"""How good is an imported memory, before any model answers from it?

For each real-waters question: is the gold value the *current* line of an entry
recall pins (best case), only somewhere in the context, only somewhere in the
store, or nowhere? And is a stale value sitting on a current line (the import
believed something old)? Seconds, no brain.

  python3 -m bench.importscore out/real/migrated-git [out/real/questions.jsonl]
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from deltos.recall import Recall


def _in(text: str, vals: list[str]) -> bool:
    t = text.lower()
    return any(v and re.search(rf"(?<![a-z0-9]){re.escape(v.lower())}(?![a-z0-9])", t) for v in vals)


def score(root: Path, qpath: Path, verbose: bool = False) -> dict:
    r = Recall(root)
    qs = [json.loads(l) for l in open(qpath)]
    out = {"current": 0, "context": 0, "store": 0, "missing": 0, "stale_current": 0, "chars": 0}
    for q in qs:
        if q["id"].startswith("abstain"):
            continue
        pinned, _ = r.retrieve(q["question"])
        ctx = r.context(q["question"])[0]
        current = "\n".join(t for _, _, t in pinned if t.startswith("- **"))
        if _in(current, q["gold"]):
            k = "current"
        elif _in(ctx, q["gold"]):
            k = "context"
        else:
            rg = subprocess.run(["rg", "-i", "-l", "-F", *sum((["-e", g] for g in q["gold"]), []), "notes"],
                                cwd=root, capture_output=True, text=True).stdout
            k = "store" if rg.strip() else "missing"
        out[k] += 1
        stale = _in(current, q.get("stale", [])) and k != "current"
        out["stale_current"] += stale
        out["chars"] += len(ctx)
        if verbose:
            print(f"{k:8} {'STALE' if stale else '     '} {q['id']:28} {current.splitlines()[0][:90] if current else ''}")
    n = sum(out[k] for k in ("current", "context", "store", "missing"))
    out["chars"] //= n
    return out


if __name__ == "__main__":
    root = Path(sys.argv[1])
    print(json.dumps(score(root, Path(sys.argv[2] if len(sys.argv) > 2 else "out/real/questions.jsonl"), True)))
