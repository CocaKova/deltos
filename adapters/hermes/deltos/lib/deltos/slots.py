"""One fact under two labels. The importer sometimes writes a new value under a
new label ("Bike count: 3" in February, "Total bikes owned: 4" in October), and
then both read as current: the reader has to guess which one is the fact now
(10 of LongMemEval's 78 update questions; it guessed wrong in 2). Whether two
labels are one fact takes meaning, not word rules ("league record" and "team
record" are; "weekday wake-up time" and "Saturday wake-up time" aren't), so a
model decides, one short call per batch of candidate pairs, and the decisions
are cached: replaying a store applies them again without the model.

  candidates(thing)          pairs of current entries that might be one fact
  apply(store, cache, judge) fold the pairs judged one fact; judge=None uses only
                             the cache (brain-free), a callable fills it
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .llm import thinking
from .recall import STOP, _stem

QUANTITY = {"count", "number", "total", "amount", "current", "new", "recent", "latest", "my", "now"}
MIN_OVERLAP = 1 / 3   # shared label words / all label words
MAX_PAIRS = 400       # per thing (LongMemEval: ~125 per question store, 9.7K over 78)
BATCH = 12            # pairs per model call

THINK = os.environ.get("DELTOS_SLOTS_THINK") == "1"
# v1 (night12) said yes to ~9 in 10 wrong pairs: different trips, a plan vs what
# happened, "business name" vs "business type", a brother's location vs the
# owner's (hand-labeled sample, out/lme/slots-labels.jsonl). v2 says what a
# same fact is, lists what isn't, and defaults to different.
PROMPT = """You keep a memory of facts about {thing}. Each pair below is two entries from it. Answer "same" only if both entries record ONE attribute that holds a single value at a time, so the newer value replaced the older one. Examples of same: "Bike count: 3" then "Total bikes owned: 4"; "Volleyball league record: 3-2" then "Volleyball team record: 5-2"; "Residence: Harajuku, Tokyo" then "Location: Harajuku".

Answer "different" when the two can both be true, or are about different things:
- different trips, projects, purchases, events, courses, items or people (a new trip is another trip, not the old one changed)
- a plan, wish or option vs what was done or chosen
- a part or detail vs the whole fact (a name vs a type, a date vs a place, a price vs an item, a method vs a platform)
- someone else's attribute vs {thing}'s own
- a goal or limit vs the actual value
When unsure, answer different.

{pairs}

Answer one line per pair and nothing else, like "1 same" or "2 different"."""
# A label word that only one of the two says and that makes it another fact:
# "planned trip" vs "trip", "previous best" vs "best", "brother's location".
MODAL = set("plan planned planning upcoming next future previous past former prior old completed finished canceled "
            "cancelled goal target limit wish considering consideration alternative replacement first initial "
            "original enrolled tried attempted".split())


def _words(label: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z]+", label.lower())
            if w not in STOP and w not in QUANTITY and len(w) > 2}


def _overlap(a: set[str], b: set[str]) -> float:
    # "watchlist" and "to-watch list": a word inside a longer one counts
    shared = {x for x in a if any(x == y or (len(x) >= 4 and len(y) >= 4 and (x in y or y in x)) for y in b)}
    return len(shared) / len(a | b) if shared else 0.0


def key(thing: str, a, b) -> str:
    return f"{thing}|{a.name}: {a.value} @{a.since}|{b.name}: {b.value} @{b.since}".lower()


NUMBER = re.compile(r"^(\S+ )?\d")


def candidates(t) -> list[tuple]:
    """(older Attr, newer Attr) pairs on one thing that might be one fact: both
    dated, neither a setting (two config keys are two keys), labels that share
    a third of their words, values of one kind (a count and a name aren't)."""
    attrs = [a for a in t.attrs.values() if a.since and not a.name.lower().startswith("setting")]
    out = []
    for i, a in enumerate(attrs):
        for b in attrs[i + 1:]:
            ov = _overlap(_words(a.name), _words(b.name))
            only = set(re.findall(r"[a-z']+", a.name.lower())) ^ set(re.findall(r"[a-z']+", b.name.lower()))
            if only & MODAL or any(w.endswith("'s") for w in only):
                continue
            if ov >= MIN_OVERLAP and bool(NUMBER.match(a.value)) == bool(NUMBER.match(b.value)):
                older, newer = (a, b) if (a.since, a.name) <= (b.since, b.name) else (b, a)
                out.append((ov, older, newer))
    out.sort(key=lambda x: -x[0])
    return [(o, n) for _, o, n in out[:MAX_PAIRS]]


def ask(thing: str, pairs: list[tuple], chat, think: bool | None = None) -> list[bool]:
    """One call for up to BATCH pairs; a pair the answer doesn't cover is a no."""
    think = THINK if think is None else think
    text = "\n".join(f'{i}. older "{o.name}: {o.value}" ({o.since}) | newer "{n.name}: {n.value}" ({n.since})'
                     for i, (o, n) in enumerate(pairs, 1))
    msg, _ = chat([{"role": "user", "content": PROMPT.format(thing=thing, pairs=text)}],
                  max_tokens=(8192 if think else 16 * len(pairs) + 32),
                  extra=thinking(think))
    said = {int(m.group(1)): m.group(2).lower() in ("same", "yes")
            for m in re.finditer(r"^\s*(\d+)[.):]?\s*\**(same|different|yes|no)\b", msg.get("content") or "", re.I | re.M)}
    return [said.get(i, False) for i in range(1, len(pairs) + 1)]


def apply(store, cache: dict, judge=None) -> dict:
    """Fold every pair the cache (or the judge, for pairs it lacks) says is one
    fact. Returns counts; the cache is updated in place."""
    st = {"pairs": 0, "asked": 0, "folded": 0}
    for sl in list(store.rows):
        t = store.find(store.label(sl).split(" (")[0])
        if t is None:
            continue
        pairs = candidates(t)
        st["pairs"] += len(pairs)
        todo = [p for p in pairs if key(t.name, *p) not in cache]
        if judge and todo:
            for i in range(0, len(todo), BATCH):
                part = todo[i:i + BATCH]
                try:
                    said = ask(t.name, part, judge)
                except Exception:   # a failed call stays unasked (not cached as "no")
                    st["errors"] = st.get("errors", 0) + 1
                    continue
                for p, yes in zip(part, said):
                    cache[key(t.name, *p)] = yes
                st["asked"] += len(part)
        for o, n in pairs:
            if cache.get(key(t.name, o, n)):
                t = store.find(t.name)
                if t is None or o.name.lower() not in {a.name.lower() for a in t.attrs.values()}:
                    continue
                if n.name.lower() not in {a.name.lower() for a in t.attrs.values()}:
                    continue
                if store.fold(t.name, o.name, n.name).startswith(t.name + ":"):
                    st["folded"] += 1
    return st


def load(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def save(path: Path, cache: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=0, sort_keys=True))
    tmp.replace(path)
