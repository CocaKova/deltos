"""Run adapters against a generated world.

  python3 -m bench.run --world out/world-7 --adapters fullcontext,grep,gbrain [--limit N] [--workers 3]
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .adapters import ADAPTERS
from .score import grade, summarize
from .world import generate, paraphrase


PARA_MODE = False


def run_one(adapter, world: Path, qs: list[dict], workers: int, out: Path) -> dict:
    t0 = time.time()
    adapter.setup(world)
    setup_s = round(time.time() - t0, 1)

    def go(q):
        t = time.time()
        try:
            a = adapter.answer(q["paraphrase"] if PARA_MODE else q["question"])
        except Exception as e:  # one bad call shouldn't sink the run
            a = {"answer": "", "abstain": True, "error": repr(e)[:300]}
        return {"q": q, "a": a, "outcome": grade(q, a), "seconds": round(time.time() - t, 1)}

    with ThreadPoolExecutor(workers) as ex:
        rows = list(ex.map(go, qs))
    with open(out / f"{adapter.name}.jsonl", "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    s = summarize(rows)
    s["setup_seconds"] = setup_s
    s["errors"] = sum(1 for r in rows if "error" in r["a"])
    s["parse_errors"] = sum(1 for r in rows if r["a"].get("parse_error"))
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", default="out/world-7")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--adapters", default="fullcontext,grep,gbrain")
    ap.add_argument("--limit", type=int, default=0, help="only the first N questions (smoke test)")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--paraphrase", action="store_true", help="ask the paraphrased questions")
    args = ap.parse_args()

    world = Path(args.world)
    if not (world / "questions.jsonl").exists():
        generate(world, args.seed)
    qs = [json.loads(l) for l in open(world / "questions.jsonl")]
    global PARA_MODE
    PARA_MODE = args.paraphrase
    if PARA_MODE and "paraphrase" not in qs[0]:
        qs = paraphrase(qs, args.seed)  # worlds generated before paraphrases existed
    if args.limit:
        qs = qs[:: max(1, len(qs) // args.limit)][: args.limit]  # spread across types
    out = world / "results" / (time.strftime("%Y%m%d-%H%M%S") + ("-para" if PARA_MODE else ""))
    out.mkdir(parents=True)

    report = {}
    for name in args.adapters.split(","):
        print(f"== {name} ({len(qs)} questions)", flush=True)
        report[name] = run_one(ADAPTERS[name](), world, qs, args.workers, out)
        print(json.dumps(report[name]["overall"], indent=1), flush=True)
    (out / "report.json").write_text(json.dumps(report, indent=1))
    print(f"\nreport -> {out / 'report.json'}")


if __name__ == "__main__":
    main()
