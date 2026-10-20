"""Agent-written memory: the model builds memory itself from a chat stream.

The world's timeline becomes team chat, one session per day, in the order
memory would have heard it: inventory pastes, "moved TS-2 yesterday", late
news, corrections, "still there" check-ins. A writer agent turns that into
notes. Afterwards the usual readers answer the world's questions from what
the writer left behind.

Writers:
  free       read/write/search tools over notes/, organize however it likes
  free-spec  same tools, with the trail format described in the prompt
  tool       deltos's remember / name / note / show tools, no file access

  python3 -m bench.stream --world out/s7-trail --writer tool --out out/stream-tool
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
import time
from datetime import date, timedelta
from pathlib import Path

from deltos.store import Store

from .llm import chat
from .world import label_questions

MAX_STEPS = 40
TEAM = ["Pia", "Dov", "Ilse", "Arun", "Wren", "Teodor"]
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


# ---- the chat stream ----------------------------------------------------------------

def _when(valid: str, said: str, r: random.Random) -> str:
    v, s = date.fromisoformat(valid), date.fromisoformat(said)
    if v == s and r.random() < 0.6:
        return "today"
    if (s - v).days == 1 and r.random() < 0.6:
        return "yesterday"
    if r.random() < 0.5:
        return f"on {MONTHS[v.month - 1]} {v.day}"
    return f"on {valid}"


def messages(world: dict, seed: int) -> dict[str, list[str]]:
    """Day -> chat lines, built from the timeline in world.json."""
    r = random.Random(seed + 4000)
    facts = world["facts"]
    days: dict[str, list[str]] = {}
    say = lambda d, text: days.setdefault(d, []).append(f"[{r.choice(TEAM)}] {text}")

    # Inventory: one paste per thing on the day its first value was recorded.
    by_entity: dict[str, list[dict]] = {}
    for f in facts:
        by_entity.setdefault(f["entity"], []).append(f)
    for name, fs in by_entity.items():
        d = min(f["history"][0]["recorded_at"] for f in fs)
        al = fs[0]["aliases"]
        rows = "; ".join(f"{f['attr']} {f['history'][0]['value']} (since {f['history'][0]['valid_from']})" for f in fs)
        say(d, f"Inventory entry for the {fs[0]['kind_of_entity']} {name} (we also call it "
               f"{' or '.join(al)}): {rows}.")

    for f in facts:
        hist = f["history"]
        for c in hist[1:]:
            d, x = c["recorded_at"], r.choice([f["entity"]] + f["aliases"])
            if c["write"].startswith("correction"):
                say(d, r.choice([
                    f"Correction for the inventory: {x}'s {f['attr']} was never {hist[0]['value']}. "
                    f"It has been {c['value']} all along.",
                    f"Heads up, the inventory had {x}'s {f['attr']} wrong. It's {c['value']} and always was, "
                    f"not {hist[0]['value']}.",
                ]))
            elif c["value"] == "RETIRED":
                say(d, f"{x}'s {f['attr']} got retired {_when(c['valid_from'], d, r)}. It's decommissioned now.")
            else:
                w = _when(c["valid_from"], d, r)
                say(d, r.choice([
                    f"{x} {f['attr']} changed to {c['value']} {w}.",
                    f"FYI {f['attr']} for {x} is {c['value']} now, as of {w.removeprefix('on ')}.",
                    f"Switched {x}'s {f['attr']} over to {c['value']} {w}.",
                ]))

    # Check-ins: someone mentions a value as it stood (what memory believed).
    start, end = date(2026, 1, 12), date(2026, 10, 1)
    d = start
    while d < end:
        if r.random() < 0.5:
            for f in r.sample(facts, 2):
                known = None
                for c in f["history"]:
                    if c["recorded_at"] <= d.isoformat():
                        known = c["value"]
                if known and known != "RETIRED":
                    x = r.choice([f["entity"]] + f["aliases"])
                    say(d.isoformat(), r.choice([
                        f"Checked on {x} today, {f['attr']} still {known}.",
                        f"Was looking at {x}, {f['attr']} is {known} like before.",
                    ]))
        d += timedelta(days=r.randint(3, 8))
    return dict(sorted(days.items()))


# ---- writers ------------------------------------------------------------------------

def _fn(name, desc, props, req):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": req}}}


S = {"type": "string"}
FILE_TOOLS = [
    _fn("list_notes", "List the note files in memory.", {}, []),
    _fn("read_note", "Read one note, e.g. notes/tamsin.md", {"path": S}, ["path"]),
    _fn("write_note", "Create or overwrite a note under notes/ with full markdown content.",
        {"path": S, "content": S}, ["path", "content"]),
    _fn("search", "Case-insensitive search over all notes; returns file:line:text.", {"pattern": S}, ["pattern"]),
]
STORE_TOOLS = [
    _fn("remember", "Record a fact about a thing: its attribute now has this value. Old values are kept as "
        "history automatically. Use the thing's name or any registered nickname; an unregistered name is refused.",
        {"thing": S, "attribute": S, "value": S,
         "since": {"type": "string", "description": "YYYY-MM-DD the value became true, if known"},
         "correction": {"type": "boolean", "description": "true if the previous value was a mistake and never true"}},
        ["thing", "attribute", "value"]),
    _fn("name_thing", "Register a thing and the other names people use for that same thing. What it has or uses "
        "(its vendor, host, owner, version) is not a name: record that with remember.",
        {"thing": S, "other_names": {"type": "array", "items": S}, "kind": S}, ["thing"]),
    _fn("show", "Show everything memory has on a thing, including old values.", {"thing": S}, ["thing"]),
    _fn("note", "A dated log line for anything that is not a fact about a thing.", {"text": S}, ["text"]),
]

FREE_PROMPT = """You keep the long-term memory of an engineering team as markdown notes in a folder (notes/).
Each session you get the team chat from one day. Update memory so that later someone can answer questions
like "where is X now?", "what was it before?", "when did it change?" and "what was it on some date?".
Organize the notes however you think works best. Today is {day}. When memory is up to date, reply "done"."""

SPEC = """
Use this format. One note per thing, notes/<thing-slug>.md:
  ---
  title: "Tamsin"
  updated: "YYYY-MM-DD"
  ---
  # Tamsin
  Tamsin (also called TS-2, the big GPU box).
  ## Details
  - **Location:** the Annex closet (since 2026-05-09)
    - was: Bay 3 rack 12 (2026-03-06 to 2026-05-09)
    - was recorded as Dock B by mistake (corrected 2026-02-01; never true)
Rules: the first line of each attribute is the current value with the date it became true. When it
changes, rewrite that line and move the old value to a "was:" line under it with its dates. Keep the
newest two "was:" lines there and move older ones to notes/<thing-slug>-history.md as
"- location: Bay 1 (2026-01-01 to 2026-02-01)", with a line "  - N older value(s) in [[<thing-slug>-history]]".
A value that turns out to have been wrong becomes "was recorded as X by mistake (...; never true)".
A repeat of the current value changes nothing. Keep "updated" current."""

TOOL_PROMPT = """You keep the long-term memory of an engineering team using memory tools.
Each session you get the team chat from one day. Record every fact about a thing (its attribute and value,
with the date it became true when the chat says or implies one) so that later someone can answer
"where is X now?", "what was it before?", "when did it change?" and "what was it on some date?".
Names: remember only accepts names memory already has. When the chat introduces a thing or mentions
another name for one ("we also call it ..."), register every name with name_thing before recording facts.
If an unfamiliar name is probably a known thing, add it to that thing with name_thing instead of creating
a new one. Old values are kept for you.
If the chat says a recorded value was wrong all along, use correction=true. Today is {day}.
When memory is up to date, reply "done".

Things memory knows (other names in brackets):
{known}"""


class Writer:
    def __init__(self, kind: str, root: Path):
        self.kind, self.root = kind, root
        (root / "notes").mkdir(parents=True, exist_ok=True)
        self.store = Store(root) if kind == "tool" else None

    def tools(self):
        return STORE_TOOLS if self.kind == "tool" else FILE_TOOLS

    def system(self, day: str) -> str:
        if self.kind == "tool":
            return TOOL_PROMPT.format(day=day, known=self.store.names_list())
        return FREE_PROMPT.format(day=day) + (SPEC if self.kind == "free-spec" else "")

    def call(self, fn: str, a: dict, day: str) -> str:
        if self.kind == "tool":
            s = self.store
            s.today = day
            if fn == "remember":
                return s.remember(a.get("thing", ""), a.get("attribute", ""), str(a.get("value", "")),
                                  since=a.get("since") or "", correction=bool(a.get("correction")))
            if fn == "name_thing":
                return s.name(a.get("thing", ""), a.get("other_names") or [], a.get("kind") or "")
            if fn == "show":
                return s.show(a.get("thing", ""))
            if fn == "note":
                return s.note(a.get("text", ""), day)
            return "(unknown tool)"
        notes = (self.root / "notes").resolve()
        if fn == "list_notes":
            return "\n".join(sorted(f"notes/{p.name}" for p in notes.glob("*.md"))) or "(empty)"
        if fn in ("read_note", "write_note"):
            p = (self.root / a.get("path", "")).resolve()
            if p.parent != notes or p.suffix != ".md":
                return "(error: path must be notes/<name>.md)"
            if fn == "read_note":
                return p.read_text()[:8000] if p.is_file() else "(no such note)"
            p.write_text(a.get("content", ""))
            return f"wrote {a.get('path')}"
        if fn == "search":
            r = subprocess.run(["rg", "-i", "-n", "--no-heading", "-e", a.get("pattern", ""), "notes"],
                               cwd=self.root, capture_output=True, text=True, timeout=20)
            lines = r.stdout.splitlines()
            return "\n".join(lines[:60]) + (f"\n... {len(lines) - 60} more" if len(lines) > 60 else "") \
                if lines else "(no matches)"
        return "(unknown tool)"

    def session(self, day: str, lines: list[str]) -> dict:
        msgs = [{"role": "system", "content": self.system(day)},
                {"role": "user", "content": f"Team chat, {day}:\n" + "\n".join(lines)}]
        stats = {"day": day, "prompt_tokens": 0, "completion_tokens": 0, "calls": 0, "steps": 0}
        for _ in range(MAX_STEPS):
            msg, usage = chat(msgs, tools=self.tools())
            stats["steps"] += 1
            stats["prompt_tokens"] += usage.get("prompt_tokens", 0)
            stats["completion_tokens"] += usage.get("completion_tokens", 0)
            tcs = msg.get("tool_calls") or []
            if not tcs:
                break
            msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tcs})
            for tc in tcs:
                stats["calls"] += 1
                try:
                    a = json.loads(tc["function"].get("arguments") or "{}")
                except ValueError:
                    a = {}
                try:
                    res = self.call(tc["function"]["name"], a, day)
                except Exception as e:   # a bad call is the writer's problem, not the run's
                    res = f"(error: {e!r})"[:300]
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": res})
        else:
            stats["step_limit"] = True
        return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", default="out/s7-trail")
    ap.add_argument("--writer", choices=["free", "free-spec", "tool"], required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--days", type=int, default=0, help="only the first N days (smoke test)")
    args = ap.parse_args()

    world, out = Path(args.world), Path(args.out)
    w = json.loads((world / "world.json").read_text())
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    days = messages(w, w["seed"])
    if args.days:
        days = dict(list(days.items())[: args.days])
    (out / "chat.jsonl").write_text("".join(json.dumps({"day": d, "lines": ls}) + "\n" for d, ls in days.items()))

    wr = Writer(args.writer, out)
    t0 = time.time()
    with open(out / "writer-log.jsonl", "w") as log:
        for i, (day, lines) in enumerate(days.items()):
            st = wr.session(day, lines)
            log.write(json.dumps(st) + "\n")
            log.flush()
            print(f"{i + 1}/{len(days)} {day} steps={st['steps']} calls={st['calls']}", flush=True)

    # Same world and questions, so bench.run can grade the memory this writer left.
    shutil.copy(world / "world.json", out / "world.json")
    if not (out / "MEMORY.md").exists():
        (out / "MEMORY.md").write_text("# MEMORY\n\n" + "\n".join(
            f"- notes/{p.name}" for p in sorted((out / "notes").glob("*.md"))) + "\n")
    qs = [json.loads(l) for l in open(world / "questions.jsonl")]
    qs = label_questions(out, qs)
    (out / "questions.jsonl").write_text("".join(json.dumps(q) + "\n" for q in qs))
    rows = [json.loads(l) for l in open(out / "writer-log.jsonl")]
    print(json.dumps({"writer": args.writer, "days": len(rows), "minutes": round((time.time() - t0) / 60, 1),
                      "prompt_tokens": sum(r["prompt_tokens"] for r in rows),
                      "completion_tokens": sum(r["completion_tokens"] for r in rows),
                      "tool_calls": sum(r["calls"] for r in rows),
                      "step_limits": sum(1 for r in rows if r.get("step_limit")),
                      "notes": len(list((out / "notes").glob("*.md")))}, indent=1))


if __name__ == "__main__":
    main()
