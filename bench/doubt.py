"""Phase 3, "confidently wrong" (10-14): how often a system gives a wrong answer
without saying it might be wrong. The pitch is memory that knows when it's
wrong, so this sits next to accuracy.

Each answer gets one label from the judge model (the same one that grades
correctness), cached so scoring is brain-free and repeatable:
  sure       it gives an answer as fact
  unsure     it gives an answer but says it may be wrong, incomplete or out of
             date, or it offers several answers without picking one
  no answer  it says it doesn't know or has no record, and gives no answer
The label says how the answer is put, not whether it's right; correctness is
the row's own `correct`. So:
  confidently wrong   wrong and sure                      (lower is better)
  says so when wrong  wrong and unsure / no answer, as a share of the wrong ones
  hedged when right   right but unsure (doubt nobody needed; guards against a
                      system that hedges everything)
single-session-preference is left out: its answers are suggestions, not facts.

  python3 -m bench.doubt label <results.jsonl or folder> ...   # asks about answers not in the cache
  python3 -m bench.doubt score <results.jsonl or folder> ...   # brain-free, from the cache
"""
from __future__ import annotations

import argparse
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import deltos.llm as llm

CACHE = Path("out/lme/doubt.json")
LABELS = ("sure", "unsure", "no answer")
SKIP_TYPES = {"single-session-preference"}

PROMPT = """How sure does this reply sound? Don't judge whether it is right, only how it is put.

SURE: it gives an answer as fact.
UNSURE: it gives an answer but says it may be wrong, incomplete or out of date, or that it is guessing, or it offers several answers without picking one.
NO ANSWER: it says it doesn't know or has no record, and gives no answer.

Examples:
Question: How many bikes do I own?
Reply: You have 3 bikes.
-> SURE
Question: How many bikes do I own?
Reply: Based on my notes, you have 3 bikes: a road bike, a gravel bike and a commuter.
-> SURE
Question: How many bikes do I own?
Reply: My notes show 3 bikes, but I may have missed a later purchase.
-> UNSURE
Question: How many bikes do I own?
Reply: Either 3 or 4; your notes mention both.
-> UNSURE
Question: How many bikes do I own?
Reply: I don't have a record of how many bikes you own.
-> NO ANSWER
Question: Which did I do first, paint the fence or buy the boat?
Reply: You painted the fence on May 1, but I have no record of buying a boat, so I can't say which came first.
-> NO ANSWER

Question: {question}
Reply: {reply}

Answer with one of SURE, UNSURE, NO ANSWER only."""


def key(qid: str, answer: str) -> str:
    return hashlib.sha1(f"{qid}\0{answer}".encode()).hexdigest()[:16]


def parse(text: str) -> str | None:
    t = " ".join((text or "").upper().replace("_", " ").split())
    for word, label in (("NO ANSWER", "no answer"), ("UNSURE", "unsure"), ("SURE", "sure")):
        if word in t:
            return label
    return None


def clip(reply: str, n: int = 1500) -> str:
    """Long replies keep their start and their end (where a "but I may have
    missed..." usually is)."""
    return reply if len(reply) <= n else reply[:n - 300] + "\n[...]\n" + reply[-300:]


def ask(question: str, reply: str) -> str | None:
    p = PROMPT.format(question=question, reply=clip(reply))
    for _ in range(2):
        msg, _ = llm.chat([{"role": "user", "content": p}], max_tokens=10, extra=llm.thinking(False))   # as the judge
        got = parse(msg.get("content") or "")
        if got:
            return got
    return None


def rows(paths: list[str]) -> list[tuple[Path, list[dict]]]:
    out = []
    for p in map(Path, paths):
        for f in sorted(p.rglob("*.jsonl")) if p.is_dir() else [p]:
            out.append((f, [json.loads(l) for l in open(f)]))
    return out


def scorable(r: dict) -> bool:
    return r["type"] not in SKIP_TYPES and not str(r.get("answer", "")).startswith("ERROR")


def load() -> dict:
    return json.loads(CACHE.read_text()) if CACHE.exists() else {}


def label(paths: list[str], workers: int = 3, say=print) -> None:
    """Rows answered since 10-14 carry their label (bench.lme run asks); this is
    for the older ones."""
    import bench.lme as L
    cache = load()
    qtext = {q["question_id"]: q["question"] for q in L.questions([], 0)}
    todo = {}
    for _, rs in rows(paths):
        for r in rs:
            k = key(r["qid"], r["answer"])
            if scorable(r) and not r.get("doubt") and k not in cache:
                todo[k] = r
    say(f"doubt: {len(todo)} answers to label ({len(cache)} cached)", flush=True)
    lock, done = threading.Lock(), [0]

    def go(item):
        k, r = item
        got = ask(qtext[r["qid"]], r["answer"])
        with lock:
            done[0] += 1
            if got:
                cache[k] = got
            if done[0] % 50 == 0 or done[0] == len(todo):
                tmp = CACHE.with_suffix(".tmp")
                tmp.write_text(json.dumps(cache, indent=0, sort_keys=True))
                tmp.replace(CACHE)
                say(f"doubt {done[0]}/{len(todo)}", flush=True)

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(go, todo.items()))


def score(rs: list[dict], cache: dict) -> dict:
    """One results file's numbers. Shares are of the scorable answers."""
    rs = [r for r in rs if scorable(r)]
    lab = [(r, r.get("doubt") or cache.get(key(r["qid"], r["answer"]))) for r in rs]
    wrong = [x for r, x in lab if not r["correct"]]
    right = [x for r, x in lab if r["correct"]]
    n = len(rs)
    conf = wrong.count("sure")
    return {"n": n, "right": len(right), "wrong": len(wrong),
            "confidently_wrong": conf, "confidently_wrong_rate": round(conf / n, 3) if n else None,
            "wrong_unsure": wrong.count("unsure"), "wrong_no_answer": wrong.count("no answer"),
            "says_so_when_wrong": round((len(wrong) - conf - wrong.count(None)) / len(wrong), 3) if wrong else None,
            "hedged_when_right": right.count("unsure"), "unlabeled": wrong.count(None) + right.count(None)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["label", "score"])
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args()
    if a.cmd == "label":
        label(a.paths, a.workers)
    cache = load()
    for f, rs in rows(a.paths):
        s = score(rs, cache)
        if not s["n"]:
            continue
        print(f"{f}: {s['right']}/{s['n']} right, confidently wrong {s['confidently_wrong']} "
              f"({s['confidently_wrong_rate']:.1%}), says so when wrong {s['says_so_when_wrong'] or 0:.0%} "
              f"(unsure {s['wrong_unsure']}, no answer {s['wrong_no_answer']}), hedged when right "
              f"{s['hedged_when_right']}" + (f", unlabeled {s['unlabeled']}" if s["unlabeled"] else ""))


if __name__ == "__main__":
    main()
