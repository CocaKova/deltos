"""How right is the same-fact judge (deltos/slots.py)? Its prompt against the
hand-labeled pairs in out/lme/slots-labels.jsonl (y = one fact, n = two facts,
a = arguable, left out). Pairs the candidate filter drops count as "different".

  python3 -m bench.slotcheck              # no thinking and thinking
  python3 -m bench.slotcheck --pick       # print the variant to use (think|nothink|none)
"""
from __future__ import annotations

import argparse
import json
import re
from collections import namedtuple
from pathlib import Path

import deltos.llm as llm
from deltos import slots

LABELS = Path("out/lme/slots-labels.jsonl")
A = namedtuple("A", "name value since")
MIN_PRECISION = 0.8


def parse(key: str):
    thing, a, b = key.split("|")
    side = lambda x: A(*re.match(r"(.*?): (.*) @(\S*)$", x).groups())
    return thing, side(a), side(b)


def filtered(o, n) -> bool:
    only = set(re.findall(r"[a-z']+", o.name)) ^ set(re.findall(r"[a-z']+", n.name))
    return bool(only & slots.MODAL or any(w.endswith("'s") for w in only))


def score(think: bool) -> dict:
    rows = [json.loads(l) for l in open(LABELS) if json.loads(l)["label"] in "yn"]
    said = {}
    by_thing: dict[str, list] = {}
    for r in rows:
        thing, o, n = parse(r["key"])
        if filtered(o, n):
            said[r["key"]] = False
        else:
            by_thing.setdefault(thing, []).append((r["key"], o, n))
    for thing, items in by_thing.items():
        for i in range(0, len(items), slots.BATCH):
            part = items[i:i + slots.BATCH]
            for (k, _, _), yes in zip(part, slots.ask(thing, [(o, n) for _, o, n in part], llm.chat, think=think)):
                said[k] = yes
    tp = sum(1 for r in rows if r["label"] == "y" and said[r["key"]])
    fp = sum(1 for r in rows if r["label"] == "n" and said[r["key"]])
    pos = sum(1 for r in rows if r["label"] == "y")
    return {"think": think, "tp": tp, "fp": fp, "recall": round(tp / max(1, pos), 2),
            "precision": round(tp / max(1, tp + fp), 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pick", action="store_true")
    a = ap.parse_args()
    res = [score(False), score(True)]
    for r in res:
        print(json.dumps(r), flush=True)
    if a.pick:
        ok = [r for r in res if r["precision"] >= MIN_PRECISION and r["tp"]]
        best = max(ok, key=lambda r: (r["recall"], r["precision"], not r["think"]), default=None)
        print("PICK", "none" if best is None else ("think" if best["think"] else "nothink"))


if __name__ == "__main__":
    main()
