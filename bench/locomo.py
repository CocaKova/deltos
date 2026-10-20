"""LoCoMo (Maharana et al., ACL 2024; CC BY-NC 4.0, see THIRD-PARTY.md) as a second,
fresh benchmark: the same systems, reader, judge and doubt label as bench/lme.py.

The data is downloaded at run time into out/locomo/ (git-ignored) and never copied
into this repo. `prepare` turns it into LongMemEval's question layout, so bench/lme.py
and bench/others.py run it unchanged apart from three hooks:

- memory per conversation: the ~200 questions of a conversation share one memory
  (`memory_id`), built once, as each system would keep it;
- two people talking (`speakers`), not a user and an assistant: every turn's role is
  its speaker's name, deltos's import reads both whole, the reader is told whose
  conversations these are, and Mem0 gets one memory per speaker, as Mem0's own
  LoCoMo evaluation feeds it;
- categories: multi-hop, temporal, open-domain and single-hop are graded with
  LongMemEval's answer check (temporal with its temporal-reasoning variant); an
  adversarial question (the conversation never says it, often a fact about the
  other person) is graded as unanswerable, with LongMemEval's abstention check.

  python3 -m bench.locomo prepare              # download, convert, draw the sample
  python3 -m bench.locomo fresh                # 500 more, none of them in the sample
  BENCH_OUT=out/locomo BENCH_DATA=out/locomo/sample500.json python3 -m bench.lme run --systems deltos-tools,rag ...

The sample: 500 of the 1,986 questions, each category in its share of the whole,
drawn with a fixed seed, so every system and both brains get the same 500.

The fresh 500 (10-17): after the first run its misses were read and a change to
deltos was made from them, so sample500 became the practice set. fresh500 is drawn
the same way from the 1,486 questions left and is the test: its answers are not read
before a change is settled.
"""
from __future__ import annotations

import argparse
import json
import random
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"
OUT = Path("out/locomo")
SRC = OUT / "locomo10.json"
CATEGORY = {1: "multi-hop", 2: "temporal", 3: "open-domain", 4: "single-hop", 5: "adversarial"}
# LongMemEval answer-check variant per category (bench/lme.py anscheck_prompt); the
# adversarial ones use its abstention check (the "_abs" question ids).
JUDGE = {1: "multi-session", 2: "temporal-reasoning", 3: "multi-session", 4: "multi-session", 5: "multi-session"}
SAMPLE, SEED = 500, 17
UNANSWERABLE = "The conversations never say this, so it cannot be answered from them."


def stamp(s: str) -> str:
    """'1:56 pm on 8 May, 2023' -> LongMemEval's '2023/05/08 (Mon) 13:56'."""
    t = datetime.strptime(s.strip(), "%I:%M %p on %d %B, %Y")
    return t.strftime("%Y/%m/%d (%a) %H:%M")


def turn(t: dict) -> dict:
    text = t["text"]
    if t.get("blip_caption"):
        text += f" [shares an image: {t['blip_caption']}]"
    return {"role": t["speaker"], "content": text}


def convert(data: list[dict]) -> list[dict]:
    out = []
    for conv in data:
        c, cid = conv["conversation"], conv["sample_id"]
        keys = sorted((k for k in c if k.startswith("session_") and isinstance(c[k], list) and c[k]),
                      key=lambda k: int(k.split("_")[1]))
        ids = [f"{cid}-{k}" for k in keys]
        dates = [stamp(c[f"{k}_date_time"]) for k in keys]
        sessions = [[turn(t) for t in c[k]] for k in keys]
        last = max(dates, key=lambda d: d[:10] + d[-5:])
        for i, qa in enumerate(conv["qa"]):
            cat = qa["category"]
            qid = f"{cid}-q{i:03d}" + ("_abs" if cat == 5 else "")
            out.append({
                "question_id": qid, "question_type": CATEGORY[cat], "judge_type": JUDGE[cat], "category": cat,
                "question": qa["question"],
                "answer": UNANSWERABLE if cat == 5 else str(qa["answer"]),
                "question_date": last, "memory_id": cid, "speakers": [c["speaker_a"], c["speaker_b"]],
                "evidence": qa.get("evidence", []),
                "haystack_session_ids": ids, "haystack_dates": dates, "haystack_sessions": sessions,
            })
    return out


def sample(qs: list[dict], n: int = SAMPLE, seed: int = SEED, skip: frozenset = frozenset()) -> list[dict]:
    """n questions, each category in its share of the whole (largest remainders), drawn
    with a fixed seed; kept in file order. skip: question ids not to draw."""
    qs = [q for q in qs if q["question_id"] not in skip]
    by = Counter(q["category"] for q in qs)
    exact = {c: n * k / len(qs) for c, k in by.items()}
    take = {c: int(x) for c, x in exact.items()}
    for c in sorted(exact, key=lambda c: exact[c] - take[c], reverse=True)[:n - sum(take.values())]:
        take[c] += 1
    rng, keep = random.Random(seed), set()
    for c in sorted(by):
        keep |= set(rng.sample([q["question_id"] for q in qs if q["category"] == c], take[c]))
    return [q for q in qs if q["question_id"] in keep]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["prepare", "fresh"])
    ap.add_argument("--src", default="", help="a local copy of locomo10.json instead of the download")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if a.cmd == "fresh":
        qs = json.load(open(OUT / "locomo_as_lme.json"))
        seen = frozenset(q["question_id"] for q in json.load(open(OUT / f"sample{SAMPLE}.json")))
        s = sample(qs, seed=SEED + 1, skip=seen)
        (OUT / f"fresh{SAMPLE}.json").write_text(json.dumps(s))
        print(f"fresh {len(s)} of the {len(qs) - len(seen)} not in sample{SAMPLE}: "
              f"{dict(sorted(Counter(q['question_type'] for q in s).items()))}")
        return
    if a.src:
        SRC.write_bytes(Path(a.src).read_bytes())
    elif not SRC.exists():
        urllib.request.urlretrieve(URL, SRC)
    qs = convert(json.load(open(SRC)))
    (OUT / "locomo_as_lme.json").write_text(json.dumps(qs))
    s = sample(qs)
    (OUT / f"sample{SAMPLE}.json").write_text(json.dumps(s))
    print(f"{len(qs)} questions in {len({q['memory_id'] for q in qs})} conversations "
          f"({sum(len(q['haystack_session_ids']) for q in {q['memory_id']: q for q in qs}.values())} sessions); "
          f"sample {len(s)}: {dict(sorted(Counter(q['question_type'] for q in s).items()))}")


if __name__ == "__main__":
    main()
