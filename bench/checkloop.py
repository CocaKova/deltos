"""Check loop: does an agent look at the world before trusting an old memory line, and
does what it sees make memory true again? A small made-up world (no real names, none a
dictionary word) whose memory was written two months ago; since then some values
changed in "reality". The agent has deltos's tools plus `inspect`, a simulated look
at a live system (a machine, a service, a file: what it shows now). NIGHT ONLY: it
calls the model.

  python3 -m bench.checkloop [--arms off,on] [--parts ask,sweep] [--out DIR] [--smoke]
  python3 -m bench.checkloop --parts passing --arms on,seen [--repeats 3]

Arms: off = today's live setup ([recall] doubt_days 21: old drifting lines say "not
confirmed in N weeks"; the instructions say live state can drift); on = [checks] on
(remember's check, memory_checks, a check: line under a not-confirmed line, the
CHECKING instructions); seen = on plus [seen] on (deltos/seen.py: memory reads what
the agent's tools return). Half the entries were checked once before (a check: line,
seen by the on and seen arms only).

Parts:
  ask    one question per case about one entry (changed in reality, or not); scored:
         right answer (reality's value), stale answer (memory's old one), looked
         (inspect called), wrote back (memory now holds reality's value), and for the
         unchanged ones a false change.
  sweep  one "memory check-up" turn with many rounds: stale entries fixed, unchanged
         entries confirmed or changed wrongly, looks used.
  passing  the task is about something else: the message gives a command to `run`, and
         its output shows numbers memory holds differently (on the machine the tools
         run on, which the message does not name; or on a thing it names), agrees with
         memory, or is about a thing memory does not hold. Scored: moved entries fixed
         in memory, entries changed that the output said nothing on, notes given.
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import tempfile
import time
from pathlib import Path

import deltos.llm as llm
from deltos import Memory
from deltos.config import Config
from deltos.store import Store

TODAY = "2026-10-26"
WRITTEN = "2026-08-20"
OWNER = "Sam"
SYSTEM = (f"You are the assistant for {OWNER}'s home lab, with a shell-like `inspect` tool to look at live "
          "systems. Keep memory true as you work, then reply briefly.")
# (thing, kind, [(label, value in memory, value in reality now, how one would look)])
WORLD = [
    ("Gateway", "service", [("port", "8787", "9119", "ss -ltnp on Box-7"),
                            ("version", "1.4.0", "1.6.2", "gatewayd --version")]),
    ("Lyrin", "app", [("version", "2.1.0", "2.1.0", "lyrin --version"),
                      ("host", "Box-7", "Box-7", "systemctl status lyrin on Box-7")]),
    ("Brain", "model", [("model", "Kestrel-9B", "Pell-30B", "curl Box-3:8888/v1/models"),
                        ("host", "Box-3", "Box-3", "docker ps on Box-3")]),
    ("Backup job", "service", [("host", "nas", "atlas", "crontab -l on atlas and nas"),
                               ("status", "running", "running", "systemctl status backup-job")]),
    ("Embed server", "service", [("port", "8889", "8889", "ss -ltnp on Box-3"),
                                 ("status", "running", "stopped", "systemctl status embed-server")]),
    ("Folio site", "website", [("url", "https://folio.example.net", "https://folio.example.net", "curl -sI the url"),
                               ("version", "0.9.1", "1.0.0", "the footer of the live site")]),
    ("Box-7", "machine", [("ip", "192.168.1.20", "192.168.1.44", "ip addr on Box-7")]),
    ("Mail relay", "service", [("status", "live", "live", "the relay's health page")]),
]
CHANGED = {(t, lab) for t, _, es in WORLD for lab, m, r, _ in es if m != r}
# ask: (thing, label, question)
ASK = [
    ("Gateway", "port", "What port is the Gateway on? I need to point a client at it."),
    ("Gateway", "version", "Which Gateway version are we running right now?"),
    ("Lyrin", "version", "What version of Lyrin is running?"),
    ("Brain", "model", "Which model is the Brain serving at the moment?"),
    ("Brain", "host", "Which box serves the Brain?"),
    ("Backup job", "host", "Where does the backup job run these days?"),
    ("Embed server", "status", "Is the embed server up?"),
    ("Embed server", "port", "What port does the embed server use?"),
    ("Folio site", "version", "What version is the Folio site on?"),
    ("Box-7", "ip", "What's Box-7's IP address?"),
    ("Mail relay", "status", "Is the mail relay live?"),
    ("Lyrin", "host", "Which machine runs Lyrin?"),
]
SWEEP = ("Quiet hour: please do a memory check-up. Look at what memory may have wrong about the lab's live state, "
         "check it, and fix what's out of date.")
ROUNDS = {"ask": 6, "sweep": 40, "passing": 6}
ARMS = {"off": {}, "on": {"checks": True}, "seen": {"checks": True, "seen": True}}
# passing: the machine the tools run on holds two more entries (label, in memory, in reality)
HERE = "Box-7"
HERE_MORE = [("os", "Debian 11", "Debian 12"), ("python version", "3.9.2", "3.11.4")]
RUN = {"name": "run", "description": "Run a shell command on the machine you work on and return its output.",
       "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}
# (case, message, the command it gives, what the command prints, the entries that output shows moved)
PASSING = [
    ("here", "Run `sysinfo` and paste what it prints.", "sysinfo",
     "os: Debian 12 (bookworm)\npython: 3.11.4\ncpus: 8\nuptime: 12 days",
     [("Box-7", "os"), ("Box-7", "python version")]),
    ("named", "Is the queue backing up? Run `gateway status` and tell me.", "gateway status",
     "gateway 1.6.2\nlistening on port 9119\nqueue: 3 waiting\nworkers: 2 busy, 2 idle",
     [("Gateway", "version"), ("Gateway", "port")]),
    ("agrees", "Run `lyrin status` and tell me if it looks healthy.", "lyrin status",
     "lyrin 2.1.0\nstate: running\nqueue: 0 waiting", []),
    ("other", "Run `folio-build --version` so I know what the build tool is on.", "folio-build --version",
     "folio-build 4.2.0", []),
]
INSPECT = {"name": "inspect", "description": "Look at a live system now (read-only): a machine, service, site or file "
           "and what to see, e.g. 'Box-7 ip', 'systemctl status lyrin', 'Gateway port'. Returns what it shows.",
           "parameters": {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]}}


def world(root: Path, more: bool = False) -> None:
    s = Store(root, today=WRITTEN)
    s.name(OWNER, [], "person")
    for i, (thing, kind, entries) in enumerate(WORLD):
        s.name(thing, [], kind)
        for j, (label, value, _, how) in enumerate(entries):
            s.remember(thing, label, value, since=WRITTEN)
            if (i + j) % 2 == 0:   # half were checked once, the day they were written
                s.remember(thing, label, value, source="assistant", check=how)
    if more:
        for label, value, _ in HERE_MORE:
            s.remember(HERE, label, value, since=WRITTEN)
    (root / "checks.jsonl").unlink(missing_ok=True)   # those first looks are setup, not the run's


def inspect(target: str) -> str:
    """What a look shows: reality's values for the thing named (the labels named, or all)."""
    low = target.lower()
    hits = [(t, es) for t, _, es in WORLD if re.search(rf"(?<![a-z0-9]){re.escape(t.lower())}(?![a-z0-9])", low)
            or t.lower().replace(" ", "-") in low]
    if not hits:
        return f"inspect: nothing answers to {target!r}"
    out = []
    for t, es in hits[:2]:
        named = [e for e in es if re.search(rf"\b{re.escape(e[0])}\b", low)] or es
        out += [f"{t} {lab}: {real}" for lab, _, real, _ in named]
    return "\n".join(out)


def state(root: Path, more: bool = False) -> dict:
    s = Store(root, today=TODAY)
    out = {}
    for thing, _, entries in WORLD + ([(HERE, "machine", HERE_MORE)] if more else []):
        t = s.find(thing)
        for label, *_ in entries:
            a = t.attrs.get(label) if t else None
            out[(thing, label)] = (a.value.lower(), a.confirmed) if a else ("", "")
    return out


def _has(text: str, value: str) -> bool:
    v = value.lower().lstrip("v")
    return re.search(rf"(?<![\w.]){re.escape(v)}(?![\w])", text.lower().replace(" v", " ")) is not None


def episode(arm: str, part: str, case: tuple | None = None) -> dict:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        passing = part == "passing"
        world(root, more=passing)
        before = state(root, passing)
        m = Memory(config=Config(root=root, owner=OWNER, recall={"doubt_days": 21},
                                 checks={"on": bool(ARMS[arm].get("checks"))}, seen={"on": bool(ARMS[arm].get("seen"))}),
                   agent="assistant", today=TODAY)
        msg = case[2] if part == "ask" else case[1] if passing else SWEEP
        block = m.context(msg)
        msgs = [{"role": "system", "content": f"{SYSTEM}\n\nToday is {TODAY}.\n\n{m.instructions()}\n\n"
                 + (f"## Memory\n{block}" if block else "")},
                {"role": "user", "content": msg}]
        tools = [{"type": "function", "function": t} for t in m.tools + [RUN if passing else INSPECT]]
        calls, looks, err, answer, notes, shown = [], 0, "", "", 0, set()
        for _ in range(ROUNDS[part]):
            try:
                reply, _u = llm.chat(msgs, tools=tools, max_tokens=8192)
            except Exception as e:   # a model failure is reported, not scored
                err = f"{type(e).__name__}: {e}"[:200]
                break
            tc = reply.get("tool_calls") or []
            if not tc:
                answer = reply.get("content") or ""
                break
            msgs.append({"role": "assistant", "content": reply.get("content") or "", "tool_calls": tc})
            for c in tc:
                name = c["function"]["name"]
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                    if name == "inspect":
                        looks += 1
                        out = inspect(str(args.get("target") or ""))
                    elif name == "run":
                        looks += 1
                        cmd = " ".join(str(args.get("command") or "").split())
                        out = case[3] if cmd == case[2] else f"bash: {cmd.split(' ')[0] if cmd else ''}: command not found"
                        note = m.seen(args, out, here=HERE, shown=shown)   # "" unless [seen] is on
                        notes += bool(note)
                        out += f"\n\n{note}" if note else ""
                    else:
                        out = m.call(name, args)
                except Exception as e:
                    args, out = {}, f"error: {e}"
                calls.append(f"{name}({json.dumps(args)[:100]})")
                msgs.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": str(out)[:4000]})
        after = state(root, passing)
        kept_how = sum(1 for ln in (root / "checks.jsonl").read_text().splitlines()) \
            if (root / "checks.jsonl").exists() else 0
    real = {(t, lab): r.lower() for t, _, es in WORLD for lab, _, r, _ in es}
    old = {(t, lab): v.lower() for t, _, es in WORLD for lab, v, _, _ in es}
    if passing:   # scored on what this one output showed: its entries fixed, every other entry as it was
        shows = set(case[4])
        real.update({(HERE, lab): r.lower() for lab, _, r in HERE_MORE})
        # fixed: memory now starts with reality's value ("debian 12 (bookworm)" for "Debian 12"); exact: is it
        fixed = [k for k in shows if after[k][0] != before[k][0] and after[k][0].startswith(real[k])]
        touched = [k for k in after if k not in shows and after[k][0] != before[k][0]]   # a confirmation changes no value
        return {"arm": arm, "part": part, "case": case[0], "looks": looks, "calls": calls, "error": err,
                "answer": answer[:400], "shows": len(shows), "fixed": [f"{t} {lab}" for t, lab in fixed],
                "exact": sum(after[k][0] == real[k] for k in fixed),
                "broke": [f"{t} {lab}" for t, lab in touched], "notes": notes,
                "wrote": [f"{t} {lab}: {after[(t, lab)][0]}" for t, lab in shows if after[(t, lab)][0] != before[(t, lab)][0]]}
    fixed = [k for k in CHANGED if after[k][0] == real[k] and before[k][0] != real[k]]
    broke = [k for k in real if k not in CHANGED and after[k][0] != real[k]]
    confirmed = [k for k in real if k not in CHANGED and after[k][1] == TODAY]
    row = {"arm": arm, "part": part, "looks": looks, "calls": calls, "error": err, "answer": answer[:400],
           "fixed": [f"{t} {lab}" for t, lab in fixed], "broke": [f"{t} {lab}" for t, lab in broke],
           "confirmed": [f"{t} {lab}" for t, lab in confirmed], "check_rows": kept_how,
           "block_has_check": "  - check: " in block}
    if part == "ask":
        k = (case[0], case[1])
        row.update(thing=k[0], label=k[1], changed=k in CHANGED,
                   right=bool(answer) and _has(answer, real[k]),
                   stale=bool(answer) and k in CHANGED and _has(answer, old[k]) and not _has(answer, real[k]),
                   wrote_back=after[k][0] == real[k] and (k not in CHANGED or before[k][0] != real[k]) and
                   (after[k] != before[k]))
    return row


def report(rows: list[dict], model: str) -> str:
    lines = [f"check loop · {model} · {datetime.datetime.now():%Y-%m-%d %H:%M} · {len(rows)} episodes", ""]
    for arm in ARMS:
        ask = [r for r in rows if r["arm"] == arm and r["part"] == "ask"]
        if ask:
            ch = [r for r in ask if r["changed"]]
            same = [r for r in ask if not r["changed"]]
            lines.append(f"ask   {arm:3}: right {sum(r['right'] for r in ask)}/{len(ask)}  "
                         f"(changed {sum(r['right'] for r in ch)}/{len(ch)}, unchanged {sum(r['right'] for r in same)}/"
                         f"{len(same)})  stale {sum(r['stale'] for r in ask)}  looked {sum(r['looks'] > 0 for r in ask)}"
                         f"/{len(ask)}  fixed in memory {sum(len(r['fixed']) for r in ask)}/{len(ch)}  "
                         f"wrongly changed {sum(len(r['broke']) for r in ask)}  how kept {sum(r['check_rows'] > 0 for r in ask)}"
                         f"  errors {sum(bool(r['error']) for r in ask)}")
        for r in [r for r in rows if r["arm"] == arm and r["part"] == "sweep"]:
            lines.append(f"sweep {arm:3}: fixed {len(r['fixed'])}/{len(CHANGED)}  confirmed {len(r['confirmed'])}/"
                         f"{sum(len(es) for _, _, es in WORLD) - len(CHANGED)} unchanged  wrongly changed "
                         f"{len(r['broke'])}  looks {r['looks']}  calls {len(r['calls'])}  how kept {r['check_rows']}"
                         f"{'  ERROR ' + r['error'] if r['error'] else ''}")
            lines.append(f"            fixed: {', '.join(r['fixed']) or '-'}; broke: {', '.join(r['broke']) or '-'}")
        ps = [r for r in rows if r["arm"] == arm and r["part"] == "passing"]
        for name in dict.fromkeys(r["case"] for r in ps):
            c = [r for r in ps if r["case"] == name]
            lines.append(f"passing {arm:4} {name:7}: fixed {sum(len(r['fixed']) for r in c)}/{sum(r['shows'] for r in c)} "
                         f"(the value alone {sum(r['exact'] for r in c)})  changed others {sum(len(r['broke']) for r in c)}  "
                         f"notes {sum(r['notes'] for r in c)}  calls {sum(len(r['calls']) for r in c)}  "
                         f"runs {len(c)}  errors {sum(bool(r['error']) for r in c)}")
    lines.append("")
    by = {}
    for r in rows:
        if r["part"] == "ask":
            by.setdefault((r["thing"], r["label"]), {})[r["arm"]] = r
    for (t, lab), arms in by.items():
        marks = " ".join(f"{a}={'R' if v['right'] else ('S' if v['stale'] else '-')}{'L' if v['looks'] else ''}"
                         f"{'W' if v['wrote_back'] else ''}{'!' if v['error'] else ''}" for a, v in arms.items())
        lines.append(f"{'changed  ' if (t, lab) in CHANGED else 'unchanged'} {marks:22} {t} {lab}")
    lines.append("\nR = right answer, S = stale (memory's old value), - = other; L = looked (inspect), "
                 "W = memory now holds reality's value, ! = model error")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arms", default="off,on")
    ap.add_argument("--parts", default="ask,sweep")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--out", default="")
    ap.add_argument("--smoke", action="store_true", help="one ask episode, on arm")
    a = ap.parse_args()
    cfg = Config.load()
    if cfg.llm.get("url") and not llm.ENDPOINT.get("model"):
        llm.configure(**{k: v for k, v in cfg.llm.items() if v})
    model = llm.ENDPOINT["model"]
    out = Path(a.out or f"out/check-loop/{datetime.date.today()}")
    out.mkdir(parents=True, exist_ok=True)
    jobs = [("on", "ask", ASK[0])] if a.smoke else \
        [(arm, part, case) for _ in range(a.repeats) for part in a.parts.split(",") for arm in a.arms.split(",")
         for case in (ASK if part == "ask" else PASSING if part == "passing" else [None])]
    rows = []
    with open(out / "rows.jsonl", "w") as fh:
        for arm, part, case in jobs:
            t = time.time()
            row = episode(arm, part, case)
            row["s"] = round(time.time() - t, 1)
            rows.append(row)
            fh.write(json.dumps(row) + "\n")
            fh.flush()
            print(f"{arm:3} {part:5} {row['s']:6.1f}s looks {row['looks']:2} fixed {len(row['fixed'])} "
                  f"broke {len(row['broke'])} {(case or ('', '', ''))[1 if part == 'passing' else 2][:50]:50} {row['error']}", flush=True)
    (out / "report.txt").write_text(report(rows, model))
    print(report(rows, model))


if __name__ == "__main__":
    main()
