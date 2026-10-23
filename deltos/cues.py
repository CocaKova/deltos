"""Just-in-time cues: at most two lines at the end of the per-turn block, saying
which tool the turn may need at the moment it matters.

Why (10-22): from 10-08 to 10-22 an agent on deltos wrote 124 facts, then none for
three days, and missed plain news in its own chats (a site's version drift fixed and
shipped, 0 writes). The instructions say "remember it right then", but they sit in
the system prompt, far from the moment. The block is the one part every harness
carries (Hermes, MCP, `deltos hook`), so the cue goes there, made by deltos itself.

NEWS: a sentence that tells (not asks, not asks for) a change to a thing memory
knows: a change word ("moved", "updated", "shipped", "is ... now") or a new
version, port, address or url beside its name. The label comes from what changed
(a version string -> version) and from the labels the thing already has; a value
memory already holds is no news. One line, for the first such thing.

STALE: the block shows a line marked "not confirmed" ([recall] doubt_days).

Off by default ([recall] cues = true, or DELTOS_CUES=1; DELTOS_CUES=0 wins) until it
is scored (bench/writeornot.py) on two models.
"""
from __future__ import annotations

import re

from .recall import Recall, _value

# A change said in words. Past tense or a state reached; base forms ("move", "update")
# start a request, which is not news yet.
CHANGE = re.compile(
    r"\b(moved|migrated|switched|swapped|upgraded|updated|bumped|deployed|released|shipped|launched|"
    r"renamed|replaced|retired|deleted|removed|decommissioned|stopped|paused|resumed|disabled|enabled|"
    r"installed|uninstalled|wired|went live|is live|goes live|now live|changed|landed|promoted|"
    r"shut (?:\w+ ){0,3}down|turned (?:\w+ ){0,3}off|took (?:\w+ ){0,3}down|(?:got|has|gets|have) a new \w+|"
    r"new (?:address|ip|host|port|url|version|server|name|home))\b", re.I)
# "is GLM now", "runs on Box-9 now", "now points at", "now says v1.2.1"
# (a dot inside a name or version, "GLM-5.3-Flash", is not a sentence's end)
NOW = re.compile(r"\b(?:is|are|'s|runs?|uses?|lives?|sits?|says?|listens?|points?)\b(?:[^.?!\n]|\.(?=\S)){0,60}\bnow\b"
                 r"|\bnow\s+(?:is|are|runs?|uses?|lives?|says?|listens?|points?|on|at)\b", re.I)
# not inside a name ("GLM-5.3-Flash" is a model, not version 5.3)
VERSION = re.compile(r"(?<![\w.\-])v?\d+\.\d+(?:\.\d+)*(?:[-+][0-9A-Za-z.]*[0-9A-Za-z])?(?![\w\-])(?!\.\w)")
# "port 22", "box-7:8787"; a time ("01:00") is not a port
PORT = re.compile(r"\bport\b[^\d\n]{0,12}(\d{2,5})\b|(?<=[a-z0-9\]]):(\d{3,5})\b", re.I)
STATE = re.compile(r"\b(down|up|live|offline|online|broken|running|stopped|paused|dead|healthy|retired|"
                   r"gone|deleted|working)\b", re.I)
NUMBER = re.compile(r"(?<![\w.:])\d{2,5}(?![\w.])")
IP = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?!\d)(?!\.\d)")   # a sentence's own dot may follow
URL = re.compile(r"https?://[^\s,;)]+", re.I)
# A sentence that asks for something ("Restart X", "Can you move X", "Let's ship it")
# or plans it ("thinking about switching", "next month") tells nothing yet.
REQUEST = re.compile(r"^\s*(?:please\s+|pls\s+)?(?:can|could|would|will|should|shall|let'?s|lets|go|do|don't|"
                     r"restart|check|look|show|tell|find|move|deploy|update|upgrade|switch|ship|release|install|"
                     r"run|make|set|open|fix|help|write|draft|give|list|summari[sz]e|remind|explain|try|add|"
                     r"remove|delete|stop|start)\b", re.I)
FUTURE = re.compile(r"\b(?:will|going to|gonna|plan(?:ning)? to|thinking about|might|maybe|tomorrow|next "
                    r"(?:week|month|year|time)|later today|soon)\b", re.I)
HISTORY = re.compile(r"\b(?:used to|back then|last (?:year|month)|years? ago)\b", re.I)

# What each signal says changed, most specific first; a thing's own label wins.
SHAPE_LABELS = (
    (URL, ("url", "address", "dashboard url")),
    (IP, ("ip", "address", "host", "tailnet ip", "lan ip")),
    (PORT, ("port",)),
    (VERSION, ("version", "latest release", "release")),
)
WORD_LABELS = (
    (re.compile(r"\b(released|shipped|launched)\b", re.I), ("latest release", "version", "release", "status")),
    (re.compile(r"\b(moved|migrated|runs? on|lives? on)\b", re.I), ("host", "runs on", "location", "build host",
                                                                     "schedule", "city")),
    (re.compile(r"\b(switched|swapped|replaced)\b", re.I), ("model", "vendor", "provider", "host", "version")),
    (re.compile(r"\b(retired|deleted|removed|decommissioned|stopped|paused|resumed|disabled|enabled|"
                r"installed|uninstalled|went live|is live|goes live|now live|shut (?:\w+ ){0,3}down|"
                r"turned (?:\w+ ){0,3}off|took (?:\w+ ){0,3}down|wired)\b", re.I), ("status",)),
    (re.compile(r"\b(upgraded|updated|bumped|deployed)\b", re.I), ("version", "latest release", "status")),
    (re.compile(r"\brenamed\b", re.I), ("name",)),
)
STALE = ("Lines marked \"not confirmed\" may be out of date: check them if they matter, then `remember` "
         "what you find.")


def _sentences(message: str) -> list[str]:
    """Sentences with their end mark kept, so a question can be told from news.
    A dot inside a version or an address ("v1.2.1", "192.168.1.44") ends nothing."""
    out, cur = [], ""
    for i, ch in enumerate(message):
        cur += ch
        nxt = message[i + 1] if i + 1 < len(message) else " "
        if ch in "?!\n" or (ch == "." and (nxt.isspace() or nxt in "\"')")):
            if cur.strip():
                out.append(cur.strip())
            cur = ""
    if cur.strip():
        out.append(cur.strip())
    return out


def _entries(r: Recall, prof: str) -> dict[str, str]:
    """A profile's current entries: lowercased label -> value."""
    out = {}
    for p, _, line in r.facts:
        if p == prof and line.startswith("- **") and ":**" in line:
            out[line.split(":**")[0].removeprefix("- **").lower().strip()] = _value(line).lower()
    return out


def _norm(v: str) -> str:
    return v.lower().lstrip("v").rstrip(".,;")


def news_cue(r: Recall, message: str) -> str:
    """One line naming the thing and entry the message may change, or ""."""
    for sent in _sentences(message):
        if sent.endswith("?") or REQUEST.match(sent) or FUTURE.search(sent) or HISTORY.search(sent):
            continue
        said = CHANGE.search(sent) or NOW.search(sent)
        if not said:
            continue
        mine = r.profile_of.get(r.owner.lower()) if r.owner else None
        # The thing the change is about is the one named last before it ("on the site: the
        # card now says v1.2.1" is the card's news), else the first one named.
        low = sent.lower()
        anchor = min([said.start()] + [m.start() for rx, _ in SHAPE_LABELS for m in rx.finditer(sent)])
        def where(g):
            at = [low.find(n.lower()) for n in g if 0 <= low.find(n.lower()) < anchor]
            return max(at) if at else -1
        groups = r.entities(sent)
        groups = sorted(groups, key=lambda g: -where(g)) if any(where(g) >= 0 for g in groups) else groups
        for group in groups:
            prof = r.profile_of.get(group[0].lower())
            if not prof or (prof == mine and len(groups) > 1):
                continue
            have = _entries(r, prof)
            label, values = "", []
            for rx, labels in SHAPE_LABELS:
                found = [next((g for g in m.groups() if g), m.group(0)) if m.groups() else m.group(0)
                         for m in rx.finditer(sent)]
                if found:
                    label = next((x for x in labels if x in have), labels[0])
                    values = found
                    break
            if not label:
                for rx, labels in WORD_LABELS:
                    if rx.search(sent):
                        label = next((x for x in labels if x in have), "")
                        if label:
                            break
            if not label and NOW.search(sent):
                # "is GLM now", "listens on 9119 now": no change word says which entry
                nums = NUMBER.findall(sent)
                if nums and "port" in have:
                    label, values = "port", nums
                elif STATE.search(sent) and "status" in have:
                    label = "status"
                else:
                    label = next((x for x in ("model", "version", "host") if x in have), "")
            # A label the thing has, said in the sentence ("its schedule", "the vendor"), is the entry.
            named = [x for x in have if x not in ("kind", "name") and not x.startswith("setting ")
                     and re.search(rf"\b{re.escape(x)}\b", sent.lower())]
            if named and label not in named:
                label, values = max(named, key=len), []   # the values found were another entry's shape
            if not label:
                continue   # a change, but to what is unclear: no cue beats a wrong one
            if values and label in have and all(_norm(v) in have[label] or have[label] in _norm(v)
                                                for v in values):
                continue   # the value memory already has: nothing new
            return f"This message may change {group[0]} {label}: if it does, `remember` it before answering."
    return ""


def cues(r: Recall, message: str, block: str) -> list[str]:
    """The cue lines for a message and the block built for it (at most two)."""
    out = []
    news = news_cue(r, message)
    if news:
        out.append(news)
    if "; not confirmed in " in block:
        out.append(STALE)
    return out
