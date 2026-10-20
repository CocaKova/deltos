"""Re-label questions from a world's notes and re-score saved results,
without calling the model again.

  python3 -m bench.rescore out/world-7 [results-dir]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .score import grade, summarize
from .world import label_questions


def main():
    world = Path(sys.argv[1])
    res = Path(sys.argv[2]) if len(sys.argv) > 2 else sorted((world / "results").iterdir())[-1]
    report = {}
    for f in sorted(res.glob("*.jsonl")):
        rows = [json.loads(l) for l in open(f)]
        label_questions(world, [r["q"] for r in rows])
        for r in rows:
            r["outcome"] = grade(r["q"], r["a"])
        report[f.stem] = summarize(rows)
        print(f"== {f.stem}\n" + json.dumps(report[f.stem]["overall"], indent=1))
    (res / "report-rescored.json").write_text(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
