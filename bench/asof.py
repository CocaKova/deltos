"""Time travel for the agent: does memory_show's `as_of` help a model answer
"what was X on <day>?" Each case is a small store whose entries changed several
times (older values in the cold history, two in the trail); the model gets
deltos's instructions, the memory block and all of deltos's tools, and answers
in up to MAX_ROUNDS rounds. Right = the answer names the value that held on
that day and none of the others.

  python3 -m bench.asof [--schema new|old|both] [--runs N]   # model from DELTOS_LLM_URL/MODEL

`old` is memory_show without `as_of` (the default until both models pass): the
model can still read the dates with history=true.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import tempfile
from collections import Counter
from pathlib import Path

import deltos.llm as llm
from deltos import Memory
from deltos.config import Config
from deltos.memory import show_tool
from deltos.store import Store

MAX_ROUNDS = 4
TODAY = "2026-10-16"
# thing, kind, label, [(value, since), ...] oldest first; a value starting with ! was recorded by mistake
HISTORIES = [
    ("Tamsin", "machine", "location", [("the north bench", "2026-01-09"), ("Bay 3 rack 12", "2026-03-06"),
                                       ("the Annex closet", "2026-05-09"), ("Dock B", "2026-07-01"),
                                       ("Lab 2", "2026-09-10")]),
    ("Nextcloud", "service", "version", [("27.1.0", "2026-02-01"), ("28.0.1", "2026-04-10"), ("28.0.4", "2026-06-02"),
                                         ("29.0.1", "2026-08-20"), ("30.0.0", "2026-10-01")]),
    ("Sam", "person", "employer", [("Initech", "2025-03-01"), ("Globex", "2026-02-15"), ("Hooli", "2026-05-01"),
                                   ("Umbrella", "2026-08-01")]),
    ("Backup job", "service", "host", [("nas", "2026-01-01"), ("atlas", "2026-03-20"), ("vault", "2026-06-30"),
                                       ("orion", "2026-09-05")]),
    ("Router", "device", "admin port", [("8443", "2026-03-01"), ("9443", "2026-06-15")]),
    ("Tamsin", "machine", "serial number", [("SN-11542-T", "2026-01-23"), ("!SN-48213-K", "2026-01-23")]),
]
# question, right words, wrong words (any of them in the answer = wrong); "" right = memory doesn't know
CASES = [
    ("Where was Tamsin on February 2, 2026?", "north bench", "bay 3|annex|dock b|lab 2"),
    ("Where was Tamsin in April 2026?", "bay 3", "north bench|annex|dock b|lab 2"),
    ("Where was Tamsin on June 1?", "annex", "north bench|bay 3|dock b|lab 2"),
    ("Where was Tamsin in mid-August?", "dock b", "north bench|bay 3|annex|lab 2"),
    ("Where is Tamsin now?", "lab 2", "north bench|bay 3|annex|dock b"),
    ("Which Nextcloud version were we running on May 1?", "28.0.1", "27.1.0|28.0.4|29.0.1|30.0.0"),
    ("What version was Nextcloud on in early March 2026?", "27.1.0", "28.0.1|28.0.4|29.0.1|30.0.0"),
    ("What Nextcloud version did we have on September 1st?", "29.0.1", "27.1.0|28.0.1|28.0.4|30.0.0"),
    ("Where was Sam working in January 2026?", "initech", "globex|hooli|umbrella"),
    ("Who was Sam's employer in March?", "globex", "initech|hooli|umbrella"),
    ("Where did the backup job run in February?", "nas", "atlas|vault|orion"),
    ("Which host ran the backup job in April 2026?", "atlas", "nas|vault|orion"),
    ("What was the router's admin port in December 2025?", "", "8443|9443"),
    ("What was Tamsin's serial number in February?", "48213", "11542"),
]
SYSTEM = "You are Sam's assistant. Answer from memory, briefly."


def build(root: Path) -> None:
    s = Store(root)
    for thing, kind, label, values in HISTORIES:
        if s.find(thing) is None:
            s.name(thing, [], kind)
        for v, since in values:
            s.today = since
            if v.startswith("!"):
                s.today = "2026-02-14"
                s.remember(thing, label, v[1:], correction=True, source="Ava")
            else:
                s.remember(thing, label, v, since=since, source="Ava")


DONT_KNOW = re.compile(r"no (?:record|value|information|data|entry)|not (?:known|recorded|in memory)|"
                       r"(?:don't|doesn't|do not|does not) (?:have|know|show)|nothing|unknown|before .{0,40}first")


def right(answer: str, good: str, bad: str) -> bool:
    """The first value the answer names is the one that held (a good answer
    often adds what came before or after it). With no value that held, it
    says memory doesn't know before naming any value."""
    a = answer.lower()
    if not a.strip():
        return False   # no answer within MAX_ROUNDS
    m = re.search(bad, a)
    first_bad = m.start() if m else len(a) + 1
    if not good:
        k = DONT_KNOW.search(a)
        return bool(k) and k.start() < first_bad or not m
    return good in a and a.index(good) < first_bad


def episode(case: tuple, schema: str) -> tuple[bool, str, list]:
    question, good, bad = case
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        build(root)
        m = Memory(config=Config(root=root, owner="Sam"), agent="assistant", today=TODAY)
        tools = [show_tool(schema == "new") if t["name"] == "memory_show" else copy.deepcopy(t) for t in m.tools]
        msgs = [{"role": "system", "content": f"{SYSTEM}\n\nToday is {TODAY}.\n\n{m.instructions()}\n\n"
                 + m.context(question)},
                {"role": "user", "content": question}]
        calls, answer = [], ""
        for _ in range(MAX_ROUNDS):
            reply, _u = llm.chat(msgs, tools=[{"type": "function", "function": t} for t in tools], max_tokens=8192)
            tc = reply.get("tool_calls") or []
            answer = reply.get("content") or ""
            if not tc:
                break
            msgs.append({"role": "assistant", "content": answer, "tool_calls": tc})
            for c in tc:
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                    if schema == "old":
                        args.pop("as_of", None)
                    out = m.call(c["function"]["name"], args)
                except Exception as e:
                    args, out = {}, f"error: {e}"
                calls.append(f'{c["function"]["name"]}({json.dumps(args)[:100]})')
                msgs.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": str(out)[:4000]})
        return right(answer, good, bad), answer, calls


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--schema", default="both", choices=("new", "old", "both"))
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--out", default="")
    ap.add_argument("--rescore", default="", help="a saved run (json): score its answers again, no model")
    a = ap.parse_args()
    if a.rescore:
        cases = {q: (g, b) for q, g, b in CASES}
        for schema in ("old", "new"):
            rs = [r for r in json.loads(Path(a.rescore).read_text()) if r["schema"] == schema]
            ok = sum(right(r["answer"], *cases[r["question"]]) for r in rs)
            used = sum(any('"as_of"' in x for x in r["calls"]) for r in rs)
            print(f"{schema}: right {ok}/{len(rs)}, used as_of in {used}")
            for r in rs:
                if not right(r["answer"], *cases[r["question"]]):
                    print(f"   wrong: {r['question']} -> {' '.join(r['answer'].split())[:160]}")
        return
    model = llm.ENDPOINT["model"]
    rows = []
    for schema in (("old", "new") if a.schema == "both" else (a.schema,)):
        c = Counter()
        for r in range(a.runs):
            for case in CASES:
                ok, answer, calls = episode(case, schema)
                c[ok] += 1
                c["as_of"] += any('"as_of"' in x for x in calls)
                rows.append({"schema": schema, "run": r, "question": case[0], "right": ok, "answer": answer,
                             "calls": calls})
                print(f"{schema} {'RIGHT' if ok else 'wrong'} {case[0]:<55} -> {' '.join(answer.split())[:90]} "
                      f"| {'; '.join(calls)[:120]}", flush=True)
        print(f"== {model} schema={schema} runs={a.runs}: right {c[True]}/{c[True] + c[False]}, "
              f"used as_of in {c['as_of']}", flush=True)
    out = Path(a.out or f"out/asof/{model.replace('/', '_')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=1) + "\n")


if __name__ == "__main__":
    main()
