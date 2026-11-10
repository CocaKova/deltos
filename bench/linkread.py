"""Links on the reader: the same questions answered from the per-turn block with
[recall] links off and on, by the configured model. The gate grades blocks; this
grades answers, which is what the two-model rule asks for before links are on by
default. NIGHT ONLY: it calls the brain (one request at a time).

Questions: the link world (bench/linkworld.py, made-up names) and, when a frozen
live snapshot exists (out/gate/live, private), the live gate questions.

    python3 -m bench.linkread --out out/link-read/DAY [--deadline 04:30] [--wait 7200] [--smoke]

The model comes from the memory's deltos.toml [llm] ($DELTOS_ROOT or ~/deltos), the
reader keeps the server's default thinking. Grades (bench.real.grade, whole words):
correct / stale / wrong / abstained. Writes report.txt and answers.jsonl, and stops
asking at the deadline (local time) with what it has.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request
from collections import Counter
from pathlib import Path

LIVE = Path("out/gate/live")
QUESTIONS = Path("out/real/questions-night10.jsonl")
INTRO = "You answer questions using your memory. This is what memory has:\n\n"


def _reader(block: str) -> list[dict]:
    from bench.llm import ANSWER_RULES
    from deltos.memory import READING
    rules = "Reading memory: " + READING.replace("\n  ", " ")
    return [{"role": "system", "content": INTRO + (block or "(nothing)") + "\n\n" + rules + "\n\n" + ANSWER_RULES}]


def _grade(q: dict, a: dict) -> str:
    from bench.real import _has, grade
    if q.get("all") and a.get("answer") and not a.get("abstain"):
        # a list question: every gold value must be in the answer
        return "correct" if all(_has(a["answer"], [g]) for g in q["gold"]) else \
            ("stale" if _has(a["answer"], q.get("stale", [])) else "wrong")
    return grade(q, a)


def _up(url: str, key: str) -> bool:
    try:
        req = urllib.request.Request(url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"})
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception:
        return False


def sets():
    """[(set name, Memory off, Memory on, questions)]."""
    from bench.linkworld import QUESTIONS as WORLD, TODAY, build
    from deltos import Memory
    from deltos.config import Config
    tmp = Path(tempfile.mkdtemp(prefix="linkread-"))
    build(tmp)
    mk = lambda root, owner, today, extra, on: Memory(config=Config(root=root, owner=owner, recall={"links": on},
                                                                    **extra), today=today)
    w = {"pages_write": tmp / "pages"}
    out = [("world", mk(tmp, "Ivo", TODAY, w, False), mk(tmp, "Ivo", TODAY, w, True), WORLD)]
    if (LIVE / "deltos" / "notes").is_dir() and QUESTIONS.exists():
        day = (LIVE / "SNAPSHOT").read_text().split()[0]
        owner = Config.load(LIVE / "deltos").owner
        e = {"pages_write": LIVE / "brain", "pages_read": {"memory": LIVE / "memory"}}
        qs = [q for q in map(json.loads, open(QUESTIONS)) if q["gold"][0] != "unknown"]
        out.append(("live", mk(LIVE / "deltos", owner, day, e, False), mk(LIVE / "deltos", owner, day, e, True), qs))
    return out, tmp


def main(argv=None) -> int:
    global LIVE, QUESTIONS
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--deadline", default="04:30", help="local HH:MM to stop asking")
    ap.add_argument("--wait", type=int, default=0, help="seconds to wait for the model to answer /models")
    ap.add_argument("--smoke", action="store_true", help="one question, one call")
    ap.add_argument("--live", default=str(LIVE), help="the frozen live snapshot (out/gate/live)")
    ap.add_argument("--questions", default=str(QUESTIONS), help="the live gate questions")
    a = ap.parse_args(argv)
    LIVE, QUESTIONS = Path(a.live), Path(a.questions)
    from bench.llm import parse_answer
    from deltos import llm
    from deltos.config import Config
    cfg = Config.load(os.environ.get("DELTOS_ROOT") or Path.home() / "deltos")
    llm.configure(**cfg.llm)   # as deltos.learn does; chat() itself leaves thinking to the server
    url, model, key = cfg.llm["url"], cfg.llm["model"], cfg.llm.get("key", "")
    t0 = time.time()
    while not _up(url, key):
        if time.time() - t0 > a.wait:
            print(f"model not answering at {url}; nothing asked")
            return 3
        time.sleep(60)
    now = datetime.datetime.now()
    hh, mm = map(int, a.deadline.split(":"))
    stop = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if stop <= now:
        stop += datetime.timedelta(days=1)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    groups, tmp = sets()
    rows, tally = [], {}
    try:
        with open(out / "answers.jsonl", "w") as fh:
            for name, off, on, qs in groups:
                for q in qs[:1] if a.smoke else qs:
                    for arm, m in (("off", off), ("on", on)):
                        if datetime.datetime.now() >= stop:
                            break
                        block = m.context(q["question"])
                        t = time.time()
                        try:
                            msg, _ = llm.chat(_reader(block) + [{"role": "user", "content": q["question"]}],
                                              max_tokens=8192)
                            ans = parse_answer(msg.get("content") or "")
                        except Exception as exc:   # a failed call is a row, not a stopped night
                            ans = {"answer": "", "abstain": False, "error": str(exc)[:200]}
                        g = _grade(q, ans) if "error" not in ans else "error"
                        row = {"set": name, "id": q["id"], "arm": arm, "grade": g, "answer": ans.get("answer", ""),
                               "chars": len(block), "s": round(time.time() - t, 1)}
                        fh.write(json.dumps(row) + "\n")
                        fh.flush()
                        rows.append(row)
                        tally.setdefault((name, arm), Counter())[g] += 1
                    if a.smoke:
                        break
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    lines = [f"links on the reader · {model} · {datetime.datetime.now():%Y-%m-%d %H:%M} · {len(rows)} answers", ""]
    for name, _, _, qs in groups:
        for arm in ("off", "on"):
            c = tally.get((name, arm), Counter())
            n = sum(c.values())
            lines.append(f"{name:6} {arm:3}  correct {c['correct']}/{n}  stale {c['stale']}  wrong {c['wrong']}  "
                         f"abstained {c['abstained']}  errors {c['error']}")
    by = {}
    for r in rows:
        by.setdefault((r["set"], r["id"]), {})[r["arm"]] = r["grade"]
    rank = {"correct": 3, "abstained": 2, "abstained_ok": 3, "wrong": 1, "stale": 0, "error": -1}
    better = [k for k, v in by.items() if len(v) == 2 and rank[v["on"]] > rank[v["off"]]]
    worse = [k for k, v in by.items() if len(v) == 2 and rank[v["on"]] < rank[v["off"]]]
    lines += ["", f"paired: on better {len(better)}, on worse {len(worse)}"]
    lines += [f"  better {s}:{i} {by[(s, i)]['off']} -> {by[(s, i)]['on']}" for s, i in better]
    lines += [f"  worse  {s}:{i} {by[(s, i)]['off']} -> {by[(s, i)]['on']}" for s, i in worse]
    secs = [r["s"] for r in rows]
    if secs:
        lines.append(f"calls {len(secs)}, median {sorted(secs)[len(secs) // 2]} s, total {sum(secs) / 60:.1f} min")
    (out / "report.txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
