"""Another one, or the same one changed? One label, a second value: the import
used to replace the first value every time. Right for "Bike count: 3" then
4; wrong for four charity events, where each "Fundraising amount" pushed
out the last and "how much did I raise in total" saw only one of them
(10 of the 37 held-out misses, 10-13). Whether a later value is the same
thing changed or another one of its kind takes meaning, so a model decides,
with the words each value came from, in short batched calls. Decisions are
cached: rebuilding a store applies them again without the model.

  Items(cache, asker)      the decider an import consults before a value replaces
                           another; an event not in the cache is asked at once when
                           there is an asker (an import built night by night), else
                           it is "same" (today's behavior) and waits in .pending
  ask(events, chat)        the model's decisions for events, BATCH per call
  evidence(text, value)    the sentence a value came from

On in an import unless [import] items = false (deltos.sources.notes.Migrator).
Model-agnostic: the prompt teaches by examples, the answer is one word per
line, and the model reasons first, its thinking turned on the way this server
takes it (deltos.llm.thinking). Off was the same answers ~100x faster on
Qwen3.8-Flash-Next (10-14), but on GLM-5.3-Flash it read "now also runs
Pi-hole" as a replacement: of 24 written and real "another one" cases it kept
19 off, 22 on, and called no true update "another" either way (10-20, 22
cases). It runs only when a value would be replaced, so the time is small.
Any other request fields come from DELTOS_ITEMS_EXTRA (a JSON object), never
from code.
Each batch is one deltos.llm.ask from temperature 0 (a classification): an
answer that loops or holds no "N same" line is asked again one rung hotter. A
batch whose answer was cut off (finish_reason "length", or no answer after
reasoning), or that still loops or can't be read on the last rung, is asked
again in halves, never read as "unanswered".
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from . import llm
from .llm import thinking
from .recall import STOP, _stem

BATCH = 12             # events per model call
EVIDENCE_CHARS = 280   # the words a value came from, per side
# Labels that hold one value at a time by definition, whatever the thing: never asked about.
# A thing has one kind, status or version; a setting one value. Not "port" or "service":
# a service has one port, a machine many, and only the words say which (10-20: "pi-media now
# also runs Pi-hole on port 53" replaced Jellyfin and 8096). Those the check decides.
SINGLE = {"kind", "status", "version", "latest release", "relationship"}

PROMPT = """Each item is one entry in a memory: a label, an earlier value and a later value, with the words each value came from.
For each item, decide:
  same     the later value is the new state of the same thing, so the earlier value is no longer true
  another  the later value is a different one of the same kind (another event, purchase, trip, course,
           person or item), so both are true

Examples:
A. "Bike count": earlier "3" (I have 3 bikes) | later "4" (just bought my fourth bike) -> same
B. "Fundraising amount": earlier "$500" (we raised $500 at the bake sale) | later "$2,000" (the charity gala brought in $2,000) -> another
C. "Current book": earlier "Dune" (I'm reading Dune) | later "Emma" (I started Emma this week) -> same
D. "New possession": earlier "desk lamp" (got a new desk lamp) | later "coffee maker" (I bought a coffee maker) -> another
E. "Finished puzzles": earlier "4" (I've finished 4 puzzles) | later "5" (I just finished my 5th one) -> same: a running count, not another puzzle

When unsure, answer same.

{items}

Answer one line per item and nothing else, like "1 same" or "2 another"."""

ANSWER = re.compile(r"^\W*(\d+)\W*\s*(same|another|different|separate)\b", re.I | re.M)


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def key(ev: dict) -> str:
    """One decision per (thing, label, earlier value and date, later value and date)."""
    raw = "|".join(_norm(str(ev.get(k, ""))) for k in ("thing", "label", "old", "old_since", "new", "new_since"))
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _words(text: str) -> set[str]:
    """Stems of the words, and numbers without their commas ("$2,000" -> "2000")."""
    text = re.sub(r"(?<=\d),(?=\d{3})", "", text.lower())
    return {w if w[0].isdigit() else _stem(w) for w in re.findall(r"[a-z]+|\d+(?:\.\d+)?", text)
            if w[0].isdigit() or (w not in STOP and len(w) > 2)}


def evidence(text: str, value: str, label: str = "", limit: int = EVIDENCE_CHARS) -> str:
    """The user's sentence that best says this value (its words count three
    times the label's); "" when no sentence says any of it."""
    vw, lw = _words(value), _words(label)
    best, score = "", 0
    for ln in (text or "").splitlines():
        if ln.startswith("assistant:"):
            continue
        ln = ln.removeprefix("user:").strip()
        for s in re.split(r"(?<=[.!?])\s+", ln):
            sw = _words(s)
            sc = 3 * len(vw & sw) + len(lw & sw)
            if sc > score and vw & sw:
                best, score = s.strip(), sc
    return best if len(best) <= limit else best[:limit - 3].rstrip() + "..."


def single(label: str) -> bool:
    lab = _norm(label)
    return lab.startswith("setting ") or lab in SINGLE


def extra_fields() -> dict:
    """Request fields for the model call from the environment, for servers that
    need them (e.g. a switch for thinking); none by default."""
    raw = os.environ.get("DELTOS_ITEMS_EXTRA", "").strip()
    return json.loads(raw) if raw else {}


def askable(ev: dict) -> bool:
    """Only with the words behind both values: without them the model would be
    guessing, and unsure means replace (10-14: "Photography trip count 3 -> 5"
    with no words found was called another)."""
    return bool(ev.get("old_ev") and ev.get("new_ev"))


def _item(i: int, ev: dict) -> str:
    def side(v, since, ev_text):
        return f'"{v}"' + (f" ({ev_text})" if ev_text else "") + (f" [{since}]" if since else "")
    return (f'{i}. {ev["thing"]}, "{ev["label"]}": earlier {side(ev["old"], ev.get("old_since"), ev.get("old_ev"))}'
            f' | later {side(ev["new"], ev.get("new_since"), ev.get("new_ev"))}')


def ask(events: list[dict], chat, max_tokens: int = 16384, stats: dict | None = None) -> dict[str, str]:
    """{key: "same" | "another"} for the events the answers cover. A failed call
    or an unreadable line leaves its events out: they stay "same" and unasked,
    never cached as a decision the model didn't make. chat is the transport
    (deltos.llm.chat or a stand-in). An answer that loops or has no answer line
    is asked again one rung hotter; a cut-off answer, or one still looping or
    unreadable on the last rung, is asked again in halves (stats counts "cut",
    "repeat", "format", "lost" and "recovered" batches)."""
    out: dict[str, str] = {}
    stats = {} if stats is None else stats
    events = [ev for ev in events if askable(ev)]
    parts = [events[i:i + BATCH] for i in range(0, len(events), BATCH)]
    extra = {**thinking(True), **extra_fields()}   # room to reason: 8192 cut 5 batches of 12 (10-20)
    while parts:
        part = parts.pop(0)
        prompt = PROMPT.format(items="\n".join(_item(n, ev) for n, ev in enumerate(part, 1)))
        try:
            ans = llm.ask([{"role": "user", "content": prompt}], max_tokens=max_tokens,
                          valid=lambda t: bool(ANSWER.search(t)), extra=extra, transport=chat)
        except Exception:
            continue
        if ans.recovered:
            stats["recovered"] = stats.get("recovered", 0) + 1
        if ans.cut:
            why = "cut" if ans.cut in ("length", "thinking") else ans.cut
            stats[why] = stats.get(why, 0) + 1
            if len(part) > 1:
                half = len(part) // 2
                parts[:0] = [part[:half], part[half:]]
            else:
                stats["lost"] = stats.get("lost", 0) + 1
            continue
        said = {int(m.group(1)): ("same" if m.group(2).lower() == "same" else "another")
                for m in ANSWER.finditer(ans.text)}
        for n, ev in enumerate(part, 1):
            if n in said:
                out[key(ev)] = said[n]
    return out


class Items:
    """The import's decider. decide(event) -> True when the later value is
    another one (both stay current). Unknown events are "same" and wait in
    .pending; ask() fills them and the next rebuild applies them."""

    def __init__(self, cache: dict | None = None, asker=None):
        """asker(events) -> {key: decision}, e.g. lambda evs: ask(evs, llm.chat):
        a store built night by night has no later rebuild to apply .pending, so
        an unknown event is asked about before its value is filed. A failed ask
        leaves it "same", as without one."""
        self.cache = cache if cache is not None else {}
        self.asker = asker
        self.asked = 0
        self.pending: dict[str, dict] = {}
        self.events: list[dict] = []   # every event seen, with the decision applied (for listing)

    def decide(self, ev: dict) -> bool:
        k = key(ev)
        if not askable(ev):
            self.events.append({**ev, "key": k, "decision": "no evidence"})
            return False
        d = self.cache.get(k)
        if d is None and self.asker is not None:
            self.asked += 1
            d = self.asker([ev]).get(k)
            if d:
                self.cache[k] = d
        if d is None:
            self.pending.setdefault(k, ev)
        self.events.append({**ev, "key": k, "decision": d or "unasked"})
        return d == "another"


def load(path: Path | None) -> dict:
    return json.loads(path.read_text()) if path and path.exists() else {}


def save(path: Path, cache: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=0, sort_keys=True))
    tmp.replace(path)
