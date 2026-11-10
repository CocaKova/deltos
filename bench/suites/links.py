"""The links suite: questions whose answers sit in links between things
(bench/linkworld.py, a made-up world built through the store the way an agent
writes). Each question is graded on the per-turn block an agent gets
(Memory.context), with bench.gate's statuses:

  current questions   the answer on a current line, the stale value not (ok > conflict >
                      buried > miss > stale-current)
  anywhere questions  the answer anywhere in the block (a sub-line under an entry, a page): ok or miss

gold "all": every gold value must be there (a list question). Brain-free, ~1 s.

    python3 -m bench.suites.links          # the suite alone, every question's status
    python3 -m bench.suites.links -v       # and each block
"""
from __future__ import annotations

import argparse
import re
import shutil
import tempfile
from pathlib import Path

NAME = "links"
SLOW = False


def _has(q: dict):
    def has(text: str, which: str) -> bool:
        vals = q.get(which) or []
        hit = [re.search(re.escape(v), text, re.I) is not None for v in vals]
        return bool(hit) and (all(hit) if which == "gold" and q.get("all") else any(hit))
    return has


def run(verbose: bool = False) -> dict:
    from bench.gate import status
    from bench.linkworld import QUESTIONS, build, memory
    tmp = Path(tempfile.mkdtemp(prefix="gate-links-"))
    try:
        build(tmp)
        m = memory(tmp)
        out = {}
        for q in QUESTIONS:
            ctx = m.context(q["question"])
            s = status(ctx, _has(q), past=q.get("where") == "anywhere", owners=True)
            out[q["id"]] = {"s": s, "c": len(ctx)}
            if verbose:
                print(f"== {q['id']} [{s}] {q['question']}\n{ctx}\n")
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    res = run(a.v)
    for k, v in res.items():
        print(f"{v['s']:14} {v['c']:6}  {k}")
    from collections import Counter
    print(dict(Counter(v["s"] for v in res.values())), "chars", sum(v["c"] for v in res.values()) // max(1, len(res)))
