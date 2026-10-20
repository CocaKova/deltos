"""The agent's own save tool: does remember's `another` option keep a second
item of the same kind (another client, trip, pet) without keeping values that
really changed? Each case is a small store with one current entry and a user
message with news; the model gets deltos's instructions, the memory block and
all of deltos's tools, and acts for up to MAX_ROUNDS rounds. The outcome is
read from the store afterwards, so it doesn't matter which calls got it there.

  python3 -m bench.savetool [--schema new|old|both] [--runs N]   # model from DELTOS_LLM_URL/MODEL

Scored like the import's items check (10-14): real updates wrongly kept
as another one must stay at ~0; then as many second items kept as possible.
`old` is the tool without `another` (the default until both models pass), the baseline.
"""
from __future__ import annotations

import argparse
import copy
import json
import tempfile
from collections import Counter
from pathlib import Path

import deltos.llm as llm
from deltos import Memory
from deltos.memory import remember_tool
from deltos.config import Config
from deltos.store import Store

MAX_ROUNDS = 4
TODAY = "2026-10-16"
# (gold, thing, kind, label, old value, user message, old words, new words)
CASES = [
    ("another", "Sam", "person", "client", "Acme Dental",
     "I just signed a second client, Brightside Pediatrics! Acme Dental is still with us.", "acme", "brightside"),
    ("another", "Sam", "person", "upcoming trip", "Denver (Nov 3)",
     "Booked another trip: Austin on Dec 12. Still going to Denver in November too.", "denver", "austin"),
    ("another", "Sam", "person", "pet", "Milo (cat)",
     "We adopted a dog named Biscuit! Milo is slowly getting used to him.", "milo", "biscuit"),
    ("another", "Sam", "person", "course", "Spanish B1",
     "I also enrolled in a pottery course this fall, on top of Spanish.", "spanish", "pottery"),
    ("another", "Sam", "person", "subscription", "Netflix", "Signed up for Spotify too.", "netflix", "spotify"),
    ("another", "Home lab", "system", "vm", "pihole",
     "Spun up another VM on the home lab for Nextcloud; pihole keeps running.", "pihole", "nextcloud"),
    ("another", "Sam", "person", "conference talk", "PyCon (May)",
     "Got accepted to speak at SciPy in July as well!", "pycon", "scipy"),
    ("another", "Sam", "person", "side project", "recipe app",
     "Started a budgeting CLI as a new side project. Still working on the recipe app.", "recipe", "budget"),
    ("another", "Sam", "person", "doctor appointment", "dentist on Oct 20",
     "Also booked an eye doctor appointment for Oct 28.", "dentist", "eye"),
    ("another", "Sam", "person", "fundraiser", "bake sale ($500)",
     "We ran a charity 5K last weekend and raised $1,200.", "bake", "5k"),
    ("another", "Book club", "group", "member", "Lena", "Priya joined our book club this week.", "lena", "priya"),
    ("another", "Sam", "person", "bike", "road bike",
     "I bought a gravel bike. Keeping the road bike for weekends.", "road", "gravel"),
    ("same", "Sam", "person", "city", "Chicago", "We moved to Austin last week!", "chicago", "austin"),
    ("same", "Sam", "person", "employer", "Initech", "Started at Globex today. Friday was my last day at Initech.",
     "initech", "globex"),
    ("same", "Sam", "person", "phone", "Pixel 7", "Upgraded to a Pixel 9 and sold the 7.", "pixel 7", "pixel 9"),
    ("same", "Nextcloud", "service", "version", "28.0.4", "Updated Nextcloud to 29.0.1 this morning.", "28.0.4",
     "29.0.1"),
    ("same", "Backup job", "service", "host", "nas", "Moved the backup job over to the new server, atlas.", "nas",
     "atlas"),
    ("same", "Sam", "person", "bike count", "2", "Just bought my third bike!", "2", "3"),
    ("same", "Sam", "person", "car", "Honda Civic", "Traded in the Civic for a Model 3.", "civic", "model 3"),
    ("same", "Sam", "person", "gym membership", "active", "I cancelled my gym membership.", "active", "cancel"),
    ("same", "Sam", "person", "team size", "5", "We hired two more people, so the team is 7 now.", "5", "7"),
    ("same", "Sam", "person", "manager", "Dana", "My new manager is Luis; Dana moved to another team.", "dana", "luis"),
    ("correction", "Router", "device", "admin port", "8080",
     "Oops, the router's admin port was always 8443, never 8080.", "8080", "8443"),
    ("correction", "Sam", "person", "birthday", "March 3", "Small fix: my birthday is March 13, not March 3.",
     "march 3", "march 13"),
]
SYSTEM = "You are Sam's assistant. Keep memory up to date as Sam tells you things, then reply briefly."


def _has(text: str, words: str) -> bool:
    t = f" {text.lower()} "
    return words in t if not words.isdigit() else f" {words} " in t.replace("(", " ").replace(")", " ")


def outcome(root: Path, thing: str, label: str, old_w: str, new_w: str) -> str:
    """Read from the store. Kept both = the original entry still holds the old
    value and the news is saved anywhere (this entry, another label, another
    thing): two labels for two true things is fine. Same = the original entry
    moved on. The news counts as saved wherever its words are: a thing's name,
    a label or a value."""
    s = Store(root)
    t = s.find(thing)
    a = next((v for k, v in t.attrs.items() if k == label), None) if t else None
    things = [s.find(s.label(sl).split(" (")[0]) for sl in set(s.by_name.values())]
    news = [x.name for x in things if x] + [f"{k} {v.value}" for x in things if x for k, v in x.attrs.items()] + \
           [y.value for x in things if x for v in x.attrs.values() for y in v.also]
    if not any(_has(n, new_w) for n in news):
        return "not saved"
    if a and (_has(a.value, old_w) or any(_has(x.value, old_w) for x in a.also)):
        return "another"
    return "correction" if a and any(p.wrong and _has(p.value, old_w) for p in a.trail) else "same"


def episode(case: tuple, schema: str) -> tuple[str, list]:
    gold, thing, kind, label, old, msg, old_w, new_w = case
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        s = Store(root)
        s.today = "2026-09-01"
        s.name(thing, [], kind)
        if thing != "Sam":
            s.name("Sam", [], "person")
        s.remember(thing, label, old, since="2026-09-01")
        m = Memory(config=Config(root=root, owner="Sam"), agent="assistant")
        tools = [remember_tool(schema == "new") if t["name"] == "remember" else copy.deepcopy(t) for t in m.tools]
        msgs = [{"role": "system", "content": f"{SYSTEM}\n\nToday is {TODAY}.\n\n{m.instructions()}\n\n"
                 + m.context(msg)},
                {"role": "user", "content": msg}]
        calls = []
        for _ in range(MAX_ROUNDS):
            reply, _u = llm.chat(msgs, tools=[{"type": "function", "function": t} for t in tools], max_tokens=8192)
            tc = reply.get("tool_calls") or []
            if not tc:
                break
            msgs.append({"role": "assistant", "content": reply.get("content") or "", "tool_calls": tc})
            for c in tc:
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                    if schema == "old":
                        args.pop("another", None)
                    out = m.call(c["function"]["name"], args)
                except Exception as e:
                    args, out = {}, f"error: {e}"
                calls.append(f'{c["function"]["name"]}({json.dumps(args)[:120]})')
                msgs.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": str(out)[:4000]})
        return outcome(root, thing, label, old_w, new_w), calls


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--schema", default="both", choices=("new", "old", "both"))
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    model = llm.ENDPOINT["model"]
    rows = []
    for schema in (("old", "new") if a.schema == "both" else (a.schema,)):
        c = Counter()
        for r in range(a.runs):
            for case in CASES:
                got, calls = episode(case, schema)
                c[(case[0], got)] += 1
                rows.append({"schema": schema, "run": r, "gold": case[0], "label": case[3], "got": got, "calls": calls})
                print(f"{schema} {case[0]:>10} {case[3]:<18} -> {got:<10} {'; '.join(calls)[:160]}", flush=True)
        n = lambda g: sum(v for (gg, _), v in c.items() if gg == g)
        print(f"== {model} schema={schema} runs={a.runs}: second items kept {c[('another', 'another')]}/{n('another')}, "
              f"real updates kept as another {c[('same', 'another')]}/{n('same')}, updates right "
              f"{c[('same', 'same')]}/{n('same')}, corrections right {c[('correction', 'correction')]}/{n('correction')}, "
              f"not saved {sum(v for (g, x), v in c.items() if x == 'not saved')}", flush=True)
    out = Path(a.out or f"out/savetool/{model.replace('/', '_')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=1) + "\n")


if __name__ == "__main__":
    main()
