"""Read path: one ripgrep pass by *thing*, no embeddings, no agent loop.

1. Resolve the question to a thing using the names table (every profile's
   "X (also called A, B)" line), so "TS-2" also finds "Tamsin".
2. Pin that thing's profile entries with their "was:" trails, plus list lines
   from notes the entries link to (one hop, e.g. the cold history note).
3. Add other lines that mention any of its names: change notes before daily
   logs, newest first, capped.

The result is a short context block a model can answer from in one call.
"""
from __future__ import annotations

import datetime
import gzip
import os
import re
import subprocess
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from .store import ITEM_KINDS, kind_words, name_problem

MAX_LINES = 20  # non-profile lines
# "May be outdated": an entry set long ago, never confirmed, of a kind that
# changes often in this memory (its label words' entries have long trails),
# on a thing that changed a lot since. Learned from the store, no word list.
# Receipts ("said:" under an entry): the words a value came from. Kept in the
# notes always; shown to the reader since 10-17 (with READING_SEARCH, scored on DeepSeek
# V4.1 Flash and GLM-5.3 Flash, 332 questions, paired seeds: fixed 32 / broke 11 answers,
# 25 / 7 confident misses). DELTOS_RECEIPTS=0 hides them.
RECEIPTS = os.environ.get("DELTOS_RECEIPTS", "1") != "0"
DOUBT = os.environ.get("DELTOS_DOUBT") == "1"   # off: fires on 56/98 real questions, catches 6/33 stale lines (10-09)
DOUBT_DAYS = 30      # unconfirmed this long
DOUBT_CHURN = 0.8    # mean older values per entry for the label's most volatile word
DOUBT_SUPPORT = 5    # entries a word needs before its churn counts
DOUBT_LATER = 5      # changes on the same thing since the entry was set
# "Not confirmed in 5 weeks": a current entry of a kind that drifts in live state
# (where a thing runs, what model or version it is, its status), whose newest date
# (since, or confirmed) is at least doubt_days old ([recall] doubt_days in
# deltos.toml, or $DELTOS_DOUBT_DAYS; 0 = off, the default until it is scored on two
# models). The age is said, not judged: the reader decides whether to check. 10-22:
# "what model is the brain running?" got a five-week-old Model line, said as current.
# A setting is a chosen value, not live state; a kind doesn't drift. Not "location":
# on a live store it is mostly a home town or a file path ("Location: Austin, TX ...
# not confirmed in 4 weeks" on the owner, 10-22), not where something runs (host).
VOLATILE = {"status", "version", "latest release", "model", "host", "port", "service", "url",
            "container", "branch", "ip"}
MAX_PINNED = 80  # profile lines, trails and linked history included
MAX_OTHERS = 3   # other things' profiles opened: named in the question or pointed at
OTHER_LINES = 12 # current lines shown from each of those
ELSEWHERE = 6         # entries shown from other things when the subject lacks the asked-for entry
SMALL = 12       # a profile with this many entries or fewer is pinned whole
# What a thing *is*: the entries a question that names no attribute ("Is ComfyUI
# running?", "Tell me about Brisk") most likely needs from a big profile.
CORE = {"status", "kind", "location", "host", "owner", "model", "version", "latest release", "service",
        "container", "port", "url", "repo", "schedule", "runs on", "part of"}
# Links to another thing whose entries answer questions about this one ("Ava
# runs on Hermes": Ava's effort setting lives in Hermes' config). Always shown,
# so recall follows them one hop whatever the question asks.
LINKS = {"runs on", "part of"}
STOP = set("""a an the of on in at to is was what which who when where did does do current currently
right before it became change changed date answer yyyy mm dd now for and or by with use uses used one how has
have been being there this that from into about any all are were will would should can could our your its
not both either does run runs running configured set you your yours me my mine we us they them their he she his her""".split())
def _words() -> set[str]:
    # SCOWL's American English list (Kevin Atkinson et al.; notices in words.LICENSE and
    # THIRD-PARTY.md), shipped with deltos: until 10-17 it was read from
    # /usr/share/dict/words, and a machine without that file (most servers and
    # containers) silently took plain words for names ("port" pinned a thing named Port).
    with gzip.open(Path(__file__).with_name("words.txt.gz"), "rt") as f:
        return {w.strip() for w in f if w.strip()}


WORDS = _words()   # lowercase English words
# Words that say what kind of thing a name is, not which one ("vendor courier").
GENERIC_WORDS = {"the", "vendor", "service", "server", "project", "machine", "team", "our", "main", "box", "app"}
ALSO = re.compile(r"^(.+?) \(also called ([^)]+)\)")
DESCRIBED = 2
EXTRA_LINES = 6   # moved-out lines shown from the subject's -extra note
# "How many days did I spend camping this year?", "How much have I spent on bike
# gear in total?": a count over time. Its items are an entry's values over time
# (each new trip overwrote "Recent trip duration") and entries under several
# labels, so the block opens the history of twice as many entries. Not "how
# many bikes do I own now": that is one current value.
TALLY = re.compile(r"\bhow (many|much)\b|\bin total\b|\btotal\b|\baltogether\b|\bcombined\b|\bso far\b", re.I)
TALLY_LINES = 15   # log and note lines a count question shows (a turn shows 3)
TALLY_STOP = set("many much times total past week weeks month months year years last few couple different "
                 "altogether combined far".split())
# "How many plants did I get last month?": the plants are things of their own
# (Peace Lily, kind plant; Austin Film Festival), not entries on the asker. A
# count lists the things whose kind, or the last word of whose name, is the
# word counted, so the reader can count them.
HOW_MANY = re.compile(r"\bhow many\b", re.I)
KIND_THINGS = 12   # things of the counted kind listed
KIND_LINES = 4     # current lines shown from each
# Another current item of the same kind (deltos/items.py): "Fundraising amount:
# $2,000" with "also current: $500 / $250 / $1,000" under it. A question about
# several items ("how much did I raise in total", "how many antique items",
# "which podcasts have I listened to") needs every item; any other question
# needs the entry, not a list that crowds other entries out of the block.
ALSO_CURRENT = "  - also current: "
RECEIPT = re.compile(r"\s+- said: ")   # "  - said: ...": the words an entry's value came from
ALSO_DATE = re.compile(r" \(another one, from (\d{4}-\d\d-\d\d)\)$")
LIST = re.compile(r"\bnumber of\b|\ball (the|of)\b|\bwhich\b.*\b(have|had) i\b|\bwhat\b.*\b(have|had) i\b"
                  r"|\bhow (many|much)\b|\bin total\b|\btotal\b|\baltogether\b|\bcombined\b", re.I)
ALSO_SHOWN = 2     # also-lines shown under an entry when the question isn't about several items
COUNTED = re.compile(r"\bhow many\b|\bnumber of\b", re.I)
NOW = re.compile(r"\b(currently|current|right now|now|at the moment|still|anymore|these days)\b", re.I)
PAST = re.compile(r"\b(before|previous(ly)?|used to|history|histor|earlier|originally|first|ago|until|"
                  r"when did|what was|was it|were|changed?|change[sd]?|since when|how long|"
                  r"20\d\d-\d\d|on \w+ \d|in (jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec))", re.I)
LINK = re.compile(r"\[\[[^\]]*\]\]")
# Questions about what has been happening lately, not about one thing.
# "I", "my": the person whose memory this is (config owner).
FIRST_PERSON = re.compile(r"\b(i|i'm|i've|i'd|i'll|my|me|mine|myself)\b", re.I)
# The owner as the subject of a question: "me" is only who it is for ("show me where
# the cron jobs are", "tell me about X"). With it, the owner's profile was pinned on 21
# of 183 real per-turn blocks (10-19). "What is my port" still names the owner's entry.
OWNER_SUBJECT = re.compile(r"\b(i|i'm|i've|i'd|i'll|my|mine|myself)\b", re.I)
RECENT = re.compile(r"\b(recent(ly)?|lately|latest|these days|this (week|month)|last (few )?(days|week|month)|"
                    r"yesterday|today|so far|up to|been (doing|working|busy))\b", re.I)


@lru_cache(maxsize=None)
def _stem(w: str) -> str:
    # "es" only after the sounds that take it (boxes, matches): "bikes" is "bike", not "bik".
    if w.endswith(("ches", "shes", "sses", "xes", "zes")) and len(w) >= 5:
        return w[:-2]
    if w.endswith("ies") and len(w) >= 6:
        return w[:-3] + "y"
    for suf in ("ing", "ed", "s"):
        if w.endswith(suf) and not w.endswith("ss") and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def _value(line: str) -> str:
    """The current value of a "- **Label:** value (since ...)" line."""
    return re.sub(r" \(since .*$", "", line.split(":** ", 1)[-1])


@lru_cache(maxsize=1 << 16)
def _same_word(term: str, word: str) -> bool:
    """"released" names the "Latest release" entry; "run" doesn't name "runtime"."""
    term, word = _stem(term), _stem(word)
    if term == word or term + "e" == word or word + "e" == term:   # "baking" -> "bak", "bake"
        return True
    if len(term) >= 5 and len(word) >= 5 and (term.startswith(word) or word.startswith(term)):
        return True   # "abliterated" names the "ABLIT" entry
    return len(term) >= 5 and len(word) >= 5 and term[:5] == word[:5] and abs(len(term) - len(word)) <= 3


def _loose(term: str, word: str) -> bool:
    """A list question's word inside an entry's words: "raise" in "Fundraising",
    "EPs" for "EP", "antique" in "antique tea set"."""
    if _same_word(term, word):
        return True
    def base(x):
        y = _stem(x)
        if y == x and x.endswith("s") and not x.endswith("ss") and len(x) >= 3:
            y = x[:-1]   # a plural too short for _stem: "eps"
        return y.rstrip("e") or y
    t, w = base(term), base(word)
    return t == w or (len(t) >= 4 and t in w) or (len(w) >= 4 and w in t)


def _also(b: list[str]) -> list[str]:
    return [x for x in b if x.startswith(ALSO_CURRENT)]


def _cap_also(b: list[str], keep: int) -> list[str]:
    """At most `keep` also-lines under an entry, newest first, and how many more."""
    also = _also(b)
    if len(also) <= keep:
        return b
    date = lambda x: (ALSO_DATE.search(x) or [None, ""])[1]
    kept = sorted(also, key=date, reverse=True)[:keep]   # stable: the store's order breaks ties
    more = f"  - (+{len(also) - keep} more also current: memory_show for every item)"
    out, done = [b[0]], False
    for x in b[1:]:
        if not x.startswith(ALSO_CURRENT):
            out.append(x)
        elif not done:
            out += kept + [more]
            done = True
    return out


QUANTITY = {"count", "number", "total", "amount", "current", "my"}   # not "size": "Team size" isn't "Women on team"
# A word only one label says that makes it another fact: a plan or a past value is
# not the current one ("planned trip duration" vs "recent trip duration"), and a
# different measure is a different fact ("yoga duration" vs "yoga frequency",
# "model kit" vs "model kit scale", "home offer" vs "home offer date").
OTHER_FACT = set("plan planned planning upcoming next future previous past former prior old recent last latest new "
                 "completed finished canceled cancelled rejected goal target limit wish first initial original "
                 "duration frequency time date cost price scale size length distance speed weight height age rate "
                 "score rank percentage temperature mileage".split())
# a measure after the number: "2,000 miles" is no count of bikes
UNIT = re.compile(r"^\S*?\d[\d,.]*\s*(miles?|mi|km|kilometers?|meters?|feet|ft|inch(es)?|hours?|hrs?|h|minutes?|mins?|"
                  r"seconds?|secs?|days?|weeks?|months?|years?|lbs?|pounds?|kg|grams?|g|calories|cal|steps|percent|%|"
                  r"dollars?|usd|\$)\b", re.I)
# a count or score near the start ("4", "Episode 10", "3-2", "20 titles"), not an id ("5f2a040")
# a count near the start, not a year, an id or a kind ("2-bedroom condo")
NUMBER = re.compile(r"^(\S+ )?((?!(1[89]|20)\d\d\b)\d+(?![\d-]*-?[a-z])|(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|dozen)\b)", re.I)
HASH = re.compile(r"^[0-9a-f]{7,40}$")


def _same_slot(chosen: list[list[str]], terms: list[str], label) -> list[list[str]]:
    """One count under two labels ("Bike count: 3" 02-22, "Total bikes owned: 4"
    10-10): the importer wrote the new value under a new label, and both read as
    current. When the question names both by the same word, both values are
    counts, and the labels say mostly the same words, the older one is shown
    as the newer one's past value. Not settings: two config keys are two keys
    ("Port" and "Setting android adb port"). Not when the question names the
    older label more closely: "weekday wake-up time" stays current beside a
    newer "Saturday wake-up time" when the question asks about weekdays."""
    def words(b):
        return {_stem(w) for w in re.findall(r"[a-z]+", label(b)) if w not in STOP and w not in QUANTITY}
    since = lambda b: (re.search(r"\(since (\d{4}-\d\d-\d\d)", b[0]) or [None, ""])[1]
    asked = lambda b: {t for t in terms if any(_same_word(t, w) for w in re.findall(r"[a-z]+", label(b)))}
    cand = [b for b in chosen if since(b) and asked(b) and not label(b).startswith("setting ")
            and NUMBER.search(_value(b[0])) and not HASH.match(_value(b[0]).strip())]
    gone: dict[int, list[str]] = {}
    for a in cand:
        for b in cand:
            if since(a) >= since(b) or id(a) in gone:
                continue
            wa, wb = words(a), words(b)
            only = set(re.findall(r"[a-z]+", label(a))) ^ set(re.findall(r"[a-z]+", label(b)))
            if only & OTHER_FACT:
                continue
            if wa - wb and wb - wa:
                # Each label names something the other doesn't ("Coursera courses
                # completed" / "EdX courses completed"): two counts, not one
                # rewritten ("Bike count" / "Total bikes owned" only adds words).
                continue
            ua, ub = (UNIT.match(_value(x[0]).replace("$", "1 $")) for x in (a, b))
            if bool(ua) != bool(ub) or (ua and ub and ua.group(1).lower()[:2] != ub.group(1).lower()[:2]):
                continue   # a measure and a count, or two measures, are two facts
            if (asked(a) & asked(b) and asked(a) <= asked(b) and wa and wb
                    and len(wa & wb) / len(wa | wb) >= 0.5):
                gone[id(a)] = b
    if not gone:
        return chosen
    out = []
    for b in chosen:
        if id(b) in gone:
            continue
        old = [a for a in chosen if gone.get(id(a)) is b]
        out.append(b + [f"  - was: {_value(a[0])} (as \"{label(a)}\", {since(a)} to {since(b)})" for a in old])
    return out


def _once(hits: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """The same line under two notes (one fact copied into two pages, one
    sentence in two digests) shows once, where it ranks best: case and spacing
    aside, one text is one line (3 of 154 real blocks with other notes repeated
    one, 10-19). Not across the daily log: one line on two days ("went to the
    gym") is two events, and a count or a 'when did' needs every date."""
    seen, out = set(), []
    for h in hits:
        text = " ".join(h[2].lower().split())
        key = (h[0], text) if Path(h[0]).name.startswith("log-") else text
        if key not in seen:
            seen.add(key)
            out.append(h)
    return out


def _sentence_start(q: str, i: int) -> bool:
    """q[i] starts a sentence: nothing but spaces before it, back to the start
    of the text, a line break or a . ! ? : (where any word is capitalized)."""
    k = i - 1
    while k >= 0 and q[k] in " \t":
        k -= 1
    return k < 0 or q[k] in ".!?:\n\r"


def _signature(notes: Path) -> tuple[int, int]:
    """Newest change and file count in the notes folder (~3 ms for 2,000 files):
    any write by the store, an import or a person's editor changes it."""
    stamps = [e.stat().st_mtime_ns for e in os.scandir(notes)] if notes.is_dir() else []
    return max(stamps, default=0), len(stamps)


class Recall:
    _open: dict[str, "Recall"] = {}

    @classmethod
    def open(cls, root: Path, owner: str = "") -> "Recall":
        """The loaded memory for a folder, reloaded only when its files changed."""
        key = str(Path(root).resolve())
        r = cls._open.get(key)
        sig = _signature(Path(key) / "notes")
        if r is None or r._sig != sig or r.owner != owner:
            r = cls._open[key] = cls(root, owner)
        return r

    def __init__(self, root: Path, owner: str = ""):
        self.root = Path(root)
        self.owner = owner   # whose memory this is: "my" in a question names them
        self.today = ""      # YYYY-MM-DD for a replayed world; default the real date
        self.doubt_days = 0  # "not confirmed in N weeks" on volatile entries this old; 0 = off
        self._q, self._tally, self._kinds, self._list = "", False, [], False
        self._sig = _signature(self.root / "notes")
        self.reload()

    def reload(self):
        self.dates: dict[str, str] = {}
        self.names: list[list[str]] = []      # groups of names for the same thing
        self.profile_of: dict[str, str] = {}  # lowercased name -> profile path
        self.facts: list[tuple[str, tuple, str]] = []   # (profile, label words, line) for every entry
        self.items_of: dict[int, list[str]] = {}           # fact index -> its also-current items
        self.size: dict[str, int] = {}                     # profile -> number of entries
        self.about: dict[str, str] = {}                    # profile -> names, kind, status (lowercased)
        self.kinds: dict[str, set] = {}                    # profile -> stems of its kind and its name's last word
        self.changes: dict[str, list[str]] = {}            # profile -> dates a value became true
        self._churn_n: dict[str, int] = defaultdict(int)   # label word -> entries
        self._churn_old: dict[str, int] = defaultdict(int)  # label word -> their older values
        for p in sorted((self.root / "notes").glob("*.md")):
            text = p.read_text()
            m = (re.search(r'^\s*(?:updated|modified): "?([0-9-]+)', text, re.M)
                 or re.search(r'^date: "?([0-9-]+)', text, re.M))
            self.dates[f"notes/{p.name}"] = m.group(1) if m else ""
            found = False
            for sent in (x for line in text.splitlines() for x in re.split(r"(?<=\.) ", line)):
                a = ALSO.search(sent.strip())
                if a:
                    found = True
                    group = [a.group(1).strip()] + [x.strip() for x in a.group(2).split(",")]
                    self.names.append(group)
                    for n in group:
                        self.profile_of[n.lower()] = f"notes/{p.name}"
            if "\n## Details\n" in text:
                lab_words, old_n = (), 0
                for ln in text.splitlines() + ["- **:**"]:
                    if ln.startswith("- **") and ":**" in ln:
                        for w in set(lab_words):   # the previous entry's trail is complete
                            self._churn_n[w] += 1
                            self._churn_old[w] += old_n
                        lab = ln.split(":**")[0].removeprefix("- **").lower()
                        lab_words, old_n = (() if lab.startswith("setting ") else
                                            tuple(w for w in re.findall(r"[a-z]+", lab) if len(w) > 1)), 0
                        if not lab:
                            break
                        self.facts.append((f"notes/{p.name}", tuple(re.findall(r"[a-z]+", lab))
                                           + tuple(kind_words(_value(ln.rstrip()))), ln.rstrip()))
                        self.size[f"notes/{p.name}"] = self.size.get(f"notes/{p.name}", 0) + 1
                        d = re.search(r"\(since (\d{4}-\d\d-\d\d)", ln)
                        if d:
                            self.changes.setdefault(f"notes/{p.name}", []).append(d.group(1))
                    elif ln.startswith("  - also current: "):   # another current item (deltos/items.py)
                        self.items_of.setdefault(len(self.facts) - 1, []).append(ALSO_DATE.sub("", ln[len(ALSO_CURRENT):]))
                        if ITEM_KINDS:   # an item's kind words name its entry too
                            f = self.facts[-1]
                            self.facts[-1] = (f[0], f[1] + tuple(kind_words(ALSO_DATE.sub("", ln[len(ALSO_CURRENT):].rstrip()))), f[2])
                        d = re.search(r"\(another one, from (\d{4}-\d\d-\d\d)\)", ln)
                        if d:
                            self.changes.setdefault(f"notes/{p.name}", []).append(d.group(1))
                    elif ln.startswith("  - was: "):
                        old_n += 1
                        d = re.search(r"\((\d{4}-\d\d-\d\d) to ", ln)
                        if d:
                            self.changes.setdefault(f"notes/{p.name}", []).append(d.group(1))
                    elif m2 := re.match(r"^  - (\d+) older value", ln):
                        old_n += int(m2.group(1))
            t = re.search(r'^title: "?(.+?)"?$', text, re.M)
            if "\n## Details\n" in text:
                # What the thing is, for questions that describe it instead of
                # naming it: its names, kind, and kind/status entries.
                kind = re.search(r'^kind: "?(.+?)"?$', text, re.M)
                what = [ln for ln in text.splitlines() if re.match(r"- \*\*(Kind|Status|Role|Purpose|Relationship):\*\*", ln)]
                also = ALSO.search(text)
                self.about[f"notes/{p.name}"] = " ".join(
                    [t.group(1) if t else "", also.group(2) if also else "", kind.group(1) if kind else ""] + what).lower()
                said = " ".join([kind.group(1) if kind else ""] + [_value(x) for x in what if x.startswith("- **Kind:")])
                head = re.findall(r"[a-z]+", t.group(1).lower())[-1:] if t else []
                self.kinds[f"notes/{p.name}"] = {_stem(w) for w in re.findall(r"[a-z]+", said.lower()) + head
                                                 if w not in STOP and len(w) > 2}
            if not found and t and "\n## Details\n" in text:   # a profile with no other names
                self.names.append([t.group(1)])
                self.profile_of[t.group(1).lower()] = f"notes/{p.name}"
        self.churn = {w: self._churn_old[w] / n for w, n in self._churn_n.items() if n >= DOUBT_SUPPORT}
        self._build_index()

    def doubt(self, prof: str, line: str) -> str:
        """" [may be outdated: ...]" for a current line memory should not be sure of, else ""."""
        if not DOUBT or "confirmed " in line:
            return ""
        lab = line.split(":**")[0].removeprefix("- **").lower()
        d = re.search(r"\(since (\d{4}-\d\d-\d\d)", line)
        if not d or lab.startswith("setting ") or lab in ("kind",):
            return ""
        today = self.today or datetime.date.today().isoformat()
        try:
            age = (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(d.group(1))).days
        except ValueError:
            return ""
        churn = max([self.churn.get(w, 0) for w in re.findall(r"[a-z]+", lab) if len(w) > 1] or [0])
        later = sum(1 for x in self.changes.get(prof, []) if x > d.group(1))
        if age < DOUBT_DAYS or churn < DOUBT_CHURN or later < DOUBT_LATER:
            return ""
        return f" [may be outdated: unconfirmed {age} days; {later} later changes on this thing]"

    def unconfirmed(self, line: str) -> str:
        """The current line with "; not confirmed in N weeks" in its (since ...) when
        it is a volatile entry whose newest date is doubt_days old or more."""
        if self.doubt_days <= 0:
            return line
        lab = line.split(":**")[0].removeprefix("- **").lower().strip()
        if lab.startswith("setting ") or not (lab in VOLATILE or lab.rsplit(" ", 1)[-1] in VOLATILE):
            return line
        when = re.search(r"\(since (\d{4}-\d\d-\d\d)[^)]*\)", line)
        if not when:
            return line
        dates = [when.group(1)] + re.findall(r"confirmed (\d{4}-\d\d-\d\d)", when.group(0))
        try:
            n = (datetime.date.fromisoformat(self.today or datetime.date.today().isoformat())
                 - datetime.date.fromisoformat(max(dates))).days
        except ValueError:
            return line
        if n < self.doubt_days:
            return line
        ago = f"{n} days" if n < 14 else f"{n // 7} weeks" if n < 60 else f"{n // 30} months"
        return line[:when.end() - 1] + f"; not confirmed in {ago}" + line[when.end() - 1:]

    def _build_index(self):
        """Every name, by its lowercased key, for span lookups: one dict per
        pass (full names; then names without kind words, "our courier" for
        "vendor courier"). Built once per load instead of a regex per name per
        question (7,700 names: ~55 ms a question, now well under 1)."""
        self._index = ({}, {})
        for gi, group in enumerate(self.names):
            # A thing whose name breaks the store's name rules ("35.9%", "15 min":
            # made before the rules) names nothing in a question. Store.find,
            # memory_show and the files still have it. A thing with no entry but
            # its Kind is still named: leaving it out was measured (10-19 gate:
            # 2 fixed, 2 broken), its name is still a search word. That half of the
            # junk-thing rule is off pending the owner's decision, not tuned away.
            if name_problem(group[0]):
                continue
            for n in group:
                key = re.sub(r"^the ", "", n.lower())  # "our parts vendor" == "the parts vendor"
                # "Profile", "Run", "Verify" (one-fact things from UI notes): a thing
                # named by a plain word counts only where the question writes it as a
                # name, not at the start of a sentence, where every word is capitalized
                # (entities()). "Brain" with 11 entries still counts.
                plain = bool(re.fullmatch(r"[A-Z][a-z]+", n)) and n.lower() in WORDS and \
                    self.size.get(self.profile_of.get(n.lower()), 0) < 3
                self._index[0].setdefault(key, []).append((gi, n, plain))
                short = " ".join(w for w in key.split() if w not in GENERIC_WORDS)
                if len(short) >= 4 and short != key:
                    self._index[1].setdefault(short, []).append((gi, n, plain))
        self._longest = max((len(k) for d in self._index for k in d), default=0)
        self._seen = {}

    def entities(self, q: str) -> list[list[str]]:
        """Every thing the question names, in the order it names them. The
        subject usually comes first: in "When did BFZ move onto Wadrast?" the
        value is a thing too, and the longer name isn't the one being asked about."""
        if q in self._seen:
            return self._seen[q]
        ql = q.lower()
        edge = lambda i: i < 0 or i >= len(ql) or not (ql[i].isascii() and ql[i].isalnum())
        starts = [i for i in range(len(ql)) if edge(i - 1)]
        ends = [j for j in range(1, len(ql) + 1) if edge(j)]
        found = {}
        for index in self._index:   # full names first; short names only when none
            for i in starts:
                for j in ends:
                    if j <= i:
                        continue
                    if j - i > self._longest:
                        break
                    for gi, n, plain in index.get(ql[i:j], ()):
                        if plain and (q[i:j] != n or _sentence_start(q, i)):
                            continue   # "Run the weekly update" names nothing; "what does Run do" does
                        if gi not in found or (i, -(j - i)) < found[gi][:2]:
                            found[gi] = (i, -(j - i), j)
            if found:
                break
        spans = sorted((v[0], v[1], v[2], gi) for gi, v in found.items())
        out, owners, taken = [], [], []
        for start, _, end, gi in spans:
            # A name inside a longer name of another thing doesn't count.
            if any(s <= start and end <= e and (s, e) != (start, end) for s, e in taken):
                continue
            taken.append((start, end))
            # An owner ("Alex's dotfiles PR", "Ava's Flash-Next brain") is not
            # what is asked about when the question names something after it,
            # and every line that mentions the owner is noise.
            (owners if re.match(r"['’]s\b", ql[end:end + 2]) else out).append(self.names[gi])
        out = out or owners
        mine = self._owner_asked(q)
        if mine and mine not in out:
            out.append(mine)
        self._seen[q] = out
        return out

    def _owner_asked(self, q: str) -> list[str]:
        """"What's my 5K personal best?" names the owner's own entry: the
        owner's profile counts as named when the question speaks in the first
        person and one of its words names an entry there ("I'm tired" names none)."""
        prof = self.profile_of.get(self.owner.lower()) if self.owner else None
        if not prof or not OWNER_SUBJECT.search(q):
            return []
        terms = self._terms(q, [])
        # An entry's label says one of the words, or its value says two of them
        # ("my new role as Senior Software Engineer": the job title entry).
        many = LIST.search(q)
        def fits(i, words, line):
            if any(_same_word(t, w) for t in terms for w in words):
                return True
            vw = re.findall(r"[a-z]+", _value(line).lower())
            if sum(any(_same_word(t, w) for w in vw) for t in set(terms)) >= 2:
                return True
            # a question about several items, and an entry holding several:
            # "how much did I raise in total" names "Fundraising amount"
            return bool(many and i in self.items_of and any(
                _loose(t, w) for t in terms if t not in TALLY_STOP
                for w in list(words) + vw + re.findall(r"[a-z]+", " ".join(self.items_of[i]).lower())))
        if not any(p == prof and fits(i, words, ln) for i, (p, words, ln) in enumerate(self.facts)):
            return []
        return next((g for g in self.names if self.profile_of.get(g[0].lower()) == prof), [])

    def described(self, q: str) -> list[list[str]]:
        """Nothing named: the things whose names, kind and status say most of
        the question ("the new memory system" -> deltos, kind system core,
        status active memory provider). At most DESCRIBED, each covering 2+
        of the question's words; a one-word description ("my girlfriend")
        only when exactly one thing fits it."""
        terms = list(dict.fromkeys(self._terms(q, [])))
        if not terms:
            return []
        group_of = {self.profile_of.get(g[0].lower()): g for g in self.names}
        score = {}
        for prof, text in self.about.items():
            hit = [w for w in terms if re.search(rf"\b{re.escape(w)}", text)]
            if len(hit) >= min(2, len(terms)) and prof in group_of:
                score[prof] = (len(hit), self.dates.get(prof, ""))
        if len(terms) == 1 and len(score) > 1:
            # "my mom": the one thing whose describing entry is exactly that word,
            # not the ones that only mention it ("estranged from mom").
            exact = [p for p in score if re.search(rf":\*\* {re.escape(terms[0])} \(", self.about[p])]
            score = {p: score[p] for p in exact}
        if len(terms) == 1 and len(score) != 1:
            return []   # one word describes a thing only when nothing else fits it
        best = sorted(score, key=lambda p: score[p], reverse=True)[:DESCRIBED]
        return [group_of[p] for p in best]

    def changed(self, since: str) -> list[tuple[str, list[str]]]:
        """Things whose entries changed on or after `since`, by day, newest first:
        [(day, [thing, ...])], the busiest things first within a day."""
        title = {self.profile_of.get(g[0].lower()): g[0] for g in self.names}
        days: dict[str, dict[str, int]] = {}
        for prof, _, ln in self.facts:
            m = re.search(r"\(since (\d{4}-\d\d-\d\d)", ln)
            if m and m.group(1) >= since and prof in title:
                day = days.setdefault(m.group(1), {})
                day[title[prof]] = day.get(title[prof], 0) + 1
        return [(d, sorted(days[d], key=lambda t: -days[d][t])) for d in sorted(days, reverse=True)]

    def entity(self, q: str) -> list[str]:
        ents = self.entities(q)
        return ents[0] if ents else []

    def _terms(self, q: str, names: list[str]) -> list[str]:
        ql = q.lower()
        for n in names:
            ql = ql.replace(n.lower(), " ")
        return [w for w in re.findall(r"[a-z][a-z-]+", ql) if w not in STOP and len(w) > 2]

    def _pin(self, prof: str, terms: list[str], main: bool,
             others: tuple[str, ...] = ()) -> tuple[list[tuple[str, str, str]], list[str]]:
        """Entries of one profile worth showing for these question words, and
        the current values of those entries (they may name other things).
        others: names of the other things the question names; an entry whose
        value names one is what the two have to do with each other ("does
        Notely ship on GitHub?" -> "Distribution: GitHub releases")."""
        blocks, lines = [], (self.root / prof).read_text().splitlines()
        for i, ln in enumerate(lines):
            if ln.startswith("- **"):
                blk = [ln]
                j = i + 1
                while j < len(lines) and lines[j].startswith("  - "):
                    if RECEIPTS or not lines[j].startswith("  - said: "):
                        blk.append(lines[j])
                    j += 1
                blocks.append(blk)
        # Match question words against the attribute label only, as words:
        # "run" must not pick the Owner entry because the owner is Arun.
        label = lambda b: b[0].split(":**")[0].removeprefix("- **").lower()
        # Kind words (deltos/store.py ITEM_KINDS) name an entry like its label does: "how
        # many kitchen items" asks for "Gift received: espresso machine [kitchen appliance]".
        lab_words = lambda b: re.findall(r"[a-z]+", label(b)) + ([w for x in [_value(b[0])] + [
            ALSO_DATE.sub("", y[len(ALSO_CURRENT):]) for y in _also(b)] for w in kind_words(x)] if ITEM_KINDS else [])
        pats = [re.compile(rf"(?<![a-z0-9]){re.escape(n.lower())}(?![a-z0-9])") for n in others if len(n) > 2]
        # Settings are config keys, not what two things have to do with each other
        # ("Setting deferred: Matrix threads" is not how Notely talks to Matrix).
        names_other = lambda b: not label(b).startswith("setting ") and any(p.search(_value(b[0]).lower()) for p in pats)
        if len(blocks) <= SMALL:
            chosen = [b for b in blocks if names_other(b) or any(_same_word(t, w) for t in terms
                                                                 for w in lab_words(b))] or blocks
            deep = chosen if PAST.search(self._q) or self._tally else []   # cold history: the past, or a count over it
        else:
            # A big profile: the entries the question names, rarest word
            # first ("tokenbudget" beats "setting", which names hundreds),
            # then what the thing is (status, host, ...).
            hit = {t: [i for i, b in enumerate(blocks)
                       if any(_same_word(t, w) for w in lab_words(b))] for t in terms}
            # The value says it too: "how many engineers do I lead" is the
            # "Team size: 5 engineers" entry. Half the weight of the label.
            in_value = {t: [i for i, b in enumerate(blocks) if i not in hit[t] and
                            any(_same_word(t, w) for w in re.findall(r"[a-z]+", _value(b[0]).lower()))] for t in terms}
            score: dict[int, float] = {}
            for t, idx in in_value.items():
                if 0 < len(idx) <= max(3, len(blocks) // 10):
                    for i in idx:
                        score[i] = score.get(i, 0) + 0.5 / len(idx)
            since = lambda i: (re.search(r"\(since (\d{4}-\d\d-\d\d)", blocks[i][0]) or [None, ""])[1]
            for t, idx in hit.items():
                if 0 < len(idx) <= max(3, len(blocks) // 10):
                    for i in idx:
                        score[i] = score.get(i, 0) + 1 / len(idx)
                        if (len(re.findall(r"[a-z]+", re.sub(r"^setting ", "", label(blocks[i])))) == 1
                                and since(i) >= max(since(j) for j in idx)):
                            # "Commit" for "which commit ...": the entry named by the word
                            # alone, not one of the 17 labels that contain it; unless a
                            # newer entry says the word too ("Lens" 03-11 vs "Camera
                            # lens" 05-19: the old label is the one that drifted).
                            score[i] += 1
                else:
                    # Too common a word, but an entry named by that word alone
                    # ("Setting ABLIT" for "abliterated") is still the one asked about.
                    for i in idx:
                        if len(re.findall(r"[a-z]+", re.sub(r"^setting ", "", label(blocks[i])))) == 1:
                            score[i] = score.get(i, 0) + 1
            for i, b in enumerate(blocks):
                if pats and names_other(b):
                    score[i] = score.get(i, 0) + 1
            named = sorted(score, key=lambda i: (-score[i], i))[:SMALL * (2 if self._tally else 1)]
            core = [i for i, b in enumerate(blocks) if label(b) in CORE and i not in named]
            # What the thing is (its core entries) when the question asks about
            # something else: the current line only, unless it asks about the past.
            past = PAST.search(self._q)
            chosen = [blocks[i] for i in named] + [blocks[i] if past else blocks[i][:1] for i in core] or blocks[:SMALL]
            # Only these open their cold history, and only for a question about
            # the past: "what is X now" gets the current value, its two latest
            # old values and a count, not every value it ever had.
            deep = [blocks[i] for i in named] if PAST.search(self._q) or self._tally else []
        if self._list:
            # Every item of a kind the question counts or lists: entries holding
            # several current items whose label, value or items say a question
            # word ("raise" -> "Fundraising amount"), first in line for the budget.
            words = lambda b: re.findall(r"[a-z]+", " ".join([label(b), _value(b[0])] + [
                ALSO_DATE.sub("", x[len(ALSO_CURRENT):]) for x in _also(b)]).lower())
            want = [t for t in terms if t not in TALLY_STOP]
            many = [b for b in blocks if _also(b) and any(_loose(t, w) for t in want for w in words(b))]
            named = lambda b: any(_same_word(t, w) for t in want for w in lab_words(b))
            count = lambda v: NUMBER.search(v) and not UNIT.match(v.replace("$", "1 $"))
            if COUNTED.search(self._q) and any(named(b) and count(_value(b[0])) for b in blocks):
                # Memory keeps the count itself ("H&M tops owned: 5"): items pulled in
                # beside it ("Recent purchase: also three tops from H&M") get counted twice.
                many = []
            chosen = many + [b for b in chosen if b not in many]
        else:
            # Any other question: an entry it names keeps its items ("which
            # streaming service did I start most recently" weighs them by date);
            # an entry shown for other reasons keeps two, so a long list can't
            # eat the block.
            asked = lambda b: any(_same_word(t, w) for t in terms for w in lab_words(b))
            chosen = [_cap_also(b, ALSO_SHOWN) if _also(b) and not asked(b) else b for b in chosen]
        chosen = chosen + [b for b in blocks if label(b) in LINKS and b not in chosen]
        chosen = _same_slot(chosen, terms, label)
        d = self.dates.get(prof, "")
        out = [(prof, d, self.unconfirmed(x.rstrip()) + self.doubt(prof, x) if j == 0 else x.rstrip())
               for b in chosen for j, x in enumerate(b)]
        values = [re.sub(r" \(since .*$| / decommissioned.*$", "", b[0].split(":** ", 1)[-1]) for b in chosen]
        if not main:   # another thing the question touches: its current lines only
            return [x for x in out if x[2].startswith("- **")][:OTHER_LINES], []
        keys = {label(b) for b in chosen}
        # Entries tidy moved out of the profile (a session log written as
        # settings, events written as a status) still answer questions:
        # the lines of <thing>-extra.md that say the question's words.
        extra = self.root / prof.replace(".md", "-extra.md")
        if extra.is_file():
            # ids and numbers count here ("PR #5564"): a moved line is matched by
            # what it says, not by the thing's name.
            want = list(dict.fromkeys(terms + re.findall(r"\b\d{3,}\b", self._q)))
            out += [(f"notes/{extra.name}", d, ln.strip()) for ln in extra.read_text().splitlines()
                    if ln.startswith("- ") and want and
                    sum(any(_same_word(t, w) for w in re.findall(r"[a-z0-9]+", ln.lower())) for t in want) >= min(2, len(want))][:EXTRA_LINES]
        # Follow [[links]] out of the chosen entries (e.g. a cold history
        # note for values that fell off the trail): one hop, list lines only.
        for slug in dict.fromkeys(re.findall(r"\[\[([^\]]+)\]\]", "\n".join(x for b in deep for x in b))):
            linked = f"notes/{slug}.md"
            if (self.root / linked).is_file():
                # Cold history holds every entry's old values; keep the chosen ones'.
                out += [(linked, self.dates.get(linked, ""), ln.strip())
                        for ln in (self.root / linked).read_text().splitlines()
                        if ln.startswith("- ") and (":" not in ln or ln[2:].split(":")[0].lower() in keys)]
        return out, values

    def _elsewhere(self, terms: list[str], first: str, skip: set) -> list[tuple[str, str, str]]:
        """Entries on other things that the question's words name, when the
        subject has no such entry: a setting often sits on the config or the
        model rather than on the agent the question names ("Ava's Hermes
        compression threshold" -> config.yaml's compression.threshold_tokens).
        An entry must match two of the question's words, one the subject lacks."""
        own = {w for f in self.facts if f[0] == first for w in f[1]}
        want = {t for t in terms if not any(_same_word(t, w) for w in own)}
        if not want:
            return []
        cover: dict[int, set] = {}
        for i, f in enumerate(self.facts):
            if f[0] in skip:
                continue
            hit = {t for t in terms if any(_same_word(t, w) for w in f[1])}
            if len(hit) >= 2 and hit & want:
                cover[i] = hit
        best = sorted(cover, key=lambda i: (-len(cover[i]), -len(cover[i] & want)))[:ELSEWHERE]
        return [(self.facts[i][0], self.dates.get(self.facts[i][0], ""), self.facts[i][2]) for i in best]

    def retrieve(self, q: str) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
        self._q = q
        self._tally = bool(TALLY.search(q)) and not NOW.search(q)
        self._list = bool(LIST.search(q))   # about several items: "currently" too (they are current)
        ents = self.entities(q) or self.described(q)
        names = ents[0] if ents else []
        # The owner named by "my" is the subject, but their names appear all
        # over memory: search by the question's words, not by the owner.
        mine = self._owner_asked(q)
        every = [n for g in ents if g is not mine for n in g]
        terms = self._terms(q, every)
        pats = every or terms
        if not pats:
            return [], []
        if self._tally:
            # "bake" must find "baked" and "baking": the stems too
            pats = list(dict.fromkeys(pats + [x for t in terms for x in (_stem(t), t[:-1] if t.endswith("e") else "")
                                               if len(x) >= 3 and x != t]))
        args = ["rg", "-i", "-n", "--no-heading", "-F"]
        for p in pats:
            args += ["-e", p]
        out = subprocess.run(args + ["notes"], cwd=self.root, capture_output=True, text=True).stdout
        hits = []
        for ln in out.splitlines():
            path, _, rest = ln.partition(":")
            no, _, text = rest.partition(":")
            if not RECEIPTS and text.startswith("  - said: "):
                continue   # a receipt shows under its entry, or not at all
            hits.append((path, int(no) if no.isdigit() else 0, text))
        # rg's file order changes run to run (three runs, three orders, 10-17) and the
        # order breaks ties below, so the same question could get a different block.
        # pages.py sorts too.
        hits = [(p, t) for p, _, t in sorted(hits, key=lambda h: h[:2])]
        # A profile's detail lines don't repeat the name, so pull them (and the
        # trail under each) from the profile itself: the subject's entries the
        # question names, then the same for the other things it names and for
        # things the subject's entries point at ("Ava's brain" -> the brain's
        # own profile). No synonym lists: retrieval is by thing.
        pinned, seen_prof = [], set()
        first = self.profile_of.get(names[0].lower()) if names else None
        if first:
            lines, values = self._pin(first, terms, main=True,
                                      others=tuple(n for g in ents[1:] if g is not mine for n in g))
            pinned += lines
            seen_prof.add(first)
            others = [self.profile_of.get(g[0].lower()) for g in ents[1:]]
            others += [self.profile_of.get(v.lower()) for v in values]
            for prof in dict.fromkeys(o for o in others if o):
                if prof not in seen_prof and len(seen_prof) < 1 + MAX_OTHERS:
                    seen_prof.add(prof)
                    pinned += self._pin(prof, terms, main=False)[0]
            pinned = pinned[:MAX_PINNED] + self._elsewhere(terms, first, seen_prof)
            hits = [h for h in hits if h[0] not in seen_prof]
        self._kinds = []
        if HOW_MANY.search(q):
            counted = {_stem(t) for t in terms if t not in TALLY_STOP}
            self._kinds = sorted((p for p, ks in self.kinds.items() if ks & counted and p not in seen_prof),
                                 key=lambda p: (self.dates.get(p, ""), p), reverse=True)[:KIND_THINGS]
            for prof in self._kinds:
                pinned += self._pin(prof, terms, main=False)[0][:KIND_LINES]
            hits = [h for h in hits if h[0] not in self._kinds]
        # A word inside a [[link]] is the linked note's name, not what the line says.
        said = lambda text: LINK.sub(" ", text).lower()
        if not names and terms and not self._tally:   # a count filters by stems below
            hits = [h for h in hits if any(t in said(h[1]) for t in terms)] or hits
        # Other things' profiles: their fact lines, not the intro paragraph. Not a
        # receipt: it shows under its own entry, inside a pinned profile; alone it is
        # a quote cut off from the fact it backs (201 of 470 Other notes lines in
        # real per-turn blocks, 10-19).
        profiles = set(self.profile_of.values())
        hits = [h for h in hits if (h[0] not in profiles or h[1].lstrip().startswith("- "))
                and not RECEIPT.match(h[1])]
        seen, uniq = set(), []
        for h in hits:
            if (h[0], h[1]) not in seen and not h[1].startswith(("title:", "date:", "updated:", "---", "# ")):
                seen.add((h[0], h[1]))
                uniq.append((h[0], self.dates.get(h[0], ""), h[1].strip()))
        if self._tally and terms:
            # A count over time: its items are the lines that say what was counted
            # (events in the daily log, notes), most of the question's words first,
            # then newest; more of them than a turn's three.
            want = [t for t in terms if t not in TALLY_STOP] or terms
            words = lambda h: re.findall(r"[a-z]+", said(h[2]))
            cov = lambda h: sum(1 for t in want if any(_same_word(t, w) for w in words(h)))
            uniq = [h for h in uniq if cov(h)]
            uniq.sort(key=lambda h: h[0])
            uniq.sort(key=lambda h: (cov(h), h[1]), reverse=True)
            return pinned, _once(uniq)[:TALLY_LINES]
        if not names and terms:
            # Nothing named ("the new memory system"): the lines that say most of
            # the question's rarer words first, then newest. Recency alone put
            # whatever was edited today on top.
            has = lambda t, h: re.search(rf"\b{re.escape(t)}", said(h[2])) is not None
            df = {t: sum(1 for h in uniq if has(t, h)) or 1 for t in terms}
            cover = lambda h: (sum(1 for t in terms if has(t, h)), sum(1 / df[t] for t in terms if has(t, h)))
            uniq.sort(key=lambda h: h[0])
            uniq.sort(key=lambda h: (cover(h), h[1]), reverse=True)
            return pinned, _once(uniq)[:MAX_LINES]
        # Notes that record a change outrank daily-log chatter (the "log-" notes,
        # a memory convention); within each group, newest first. No keyword
        # filter: words from the question (often the NEW value) would push out
        # the line a history question needs.
        uniq.sort(key=lambda h: h[0])  # rg searches files in parallel; make ties stable
        uniq.sort(key=lambda h: h[1], reverse=True)
        changes = [h for h in uniq if not Path(h[0]).name.startswith("log-")]
        chatter = [h for h in uniq if Path(h[0]).name.startswith("log-")]
        return pinned, _once(changes + chatter)[:MAX_LINES]

    def context(self, q: str, loose: int = MAX_LINES) -> tuple[str, int]:
        """The context block for a question, and how many lines it holds.
        loose: lines from other notes. When the question names nothing, they
        are word matches, not an answer: a per-turn block leaves them out."""
        pinned, hits = self.retrieve(q)
        named = bool(self.entities(q) or self.described(q) or self._kinds)
        if self._tally:
            loose = max(loose, TALLY_LINES)
        hits = hits[:loose] if named or loose >= MAX_LINES else []
        if not named and hits:
            # Nothing the question names is in memory: say so up front, so the
            # reader doesn't take the nearest word match for the answer.
            note = ("Nothing in memory is named in this message: the lines below are loose word matches, "
                    "not an answer.")
            text, n = self._render(q, pinned, hits)
            return note + "\n\n" + text, n
        return self._render(q, pinned, hits)

    def _render(self, q: str, pinned, hits) -> tuple[str, int]:
        parts: list[str] = []
        groups: list[list[tuple[str, str, str]]] = []
        # One block per profile. A linked history note and the thing's moved-out lines
        # (<thing>-extra.md) stay with its profile: under a header of their own they read
        # as another thing (10-22: "Profile entries [brain-2-extra ...]" under the Brain).
        for x in pinned:
            if not groups or (x[0] != groups[-1][0][0] and not x[0].endswith(("-history.md", "-extra.md"))):
                groups.append([])
            groups[-1].append(x)
        for g in groups:
            p, d = g[0][0], g[0][1]
            mine = " · whose memory this is" if self.owner and self.profile_of.get(self.owner.lower()) == p else ""
            parts.append(f"Profile entries [{p.removeprefix('notes/').removesuffix('.md')}{mine} · last edited {d}]:\n"
                         + "\n".join(t for _, _, t in g))
        if hits:
            parts.append("Other notes (change notes first, then daily logs; newest first in each), "
                         "each tagged [note · date]:\n" + "\n".join(
                f"[{p.removeprefix('notes/').removesuffix('.md')} · {d}] {t}" for p, d, t in hits))
        # Searching by every nickname is useless if the reader can't tell the
        # hits are about the same thing, so say so up front: the names the
        # question or the lines below use. Every alias would also list what
        # once filled a role ("Brain = agent-model@qwen-27b = ...") as if it
        # were the thing now.
        said = (q + "\n" + "\n".join(parts)).lower()
        used = lambda n: re.search(rf"(?<![a-z0-9]){re.escape(n.lower())}(?![a-z0-9])", said) is not None
        head = []
        for g in self.entities(q):
            names = [g[0]] + [n for n in g[1:] if used(n)]
            if len(names) > 1:
                head.append(f"Names for the same thing: {' = '.join(names)}")
        return "\n\n".join(head + parts) or "(no matches)", len(pinned) + len(hits)
