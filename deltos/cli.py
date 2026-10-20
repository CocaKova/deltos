"""The deltos command.

  deltos context <message>          the memory block for a message (what a harness injects per turn)
  deltos recall <query>             same, with more pages
  deltos show <thing> [--history | --as-of YYYY-MM-DD]
  deltos remember <thing> <attribute> <value> [--since YYYY-MM-DD] [--correction]   (source: hand)
  deltos name <thing> [other names...] [--kind K] [--remove NAME ...] [--merge]
  deltos search <text>              exact text across pages
  deltos read <ref>
  deltos doctor [--fix]             health numbers; --fix moves junk out of profiles
  deltos sessions [--days N]        rebuild chat digests (default: yesterday and today)
  deltos import [--limit N]         notes -> facts with the configured model ([import] repos and folders,
                                    [llm]), as one staged run: judged against this store's own runs, then
                                    promoted or held
  deltos import --drill LOG [--texts FILE]   a logged run applied again with no model (--root: a copy)
  deltos learn [--wait] [--days N] [--max N] [--dry-run] [--no-idle]   finished chats -> facts, as a
                                    staged run (deltos/learn.py); --wait is the automatic triggers' form
  deltos runs [-n N]                the latest run decisions (runs.jsonl)
  deltos run show|promote|discard|undo <id> [--no-hold]   one run: its report; promote a held one by
                                    hand; drop it; take a promoted one out of memory
  deltos status [--line]            the last run of each kind, holds, half-promoted runs
  deltos serve                      MCP server on stdio
  deltos hook claude-code           UserPromptSubmit hook: the memory block for the prompt on stdin

Memory: --root, else $DELTOS_ROOT, else ~/deltos; settings in <root>/deltos.toml.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

from .memory import Memory, Refused
from .store import HAND


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="deltos", description="Memory that knows what is true now.")
    ap.add_argument("--root", help="memory folder (default $DELTOS_ROOT or ~/deltos)")
    ap.add_argument("--agent", default="", help="who writes (recorded as each fact's source); "
                    "default: you, by hand (a person's value stands against an agent's and a note import's)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("context", "recall", "search"):
        sub.add_parser(name).add_argument("text", nargs="+")
    p = sub.add_parser("show"); p.add_argument("thing"); p.add_argument("--history", action="store_true")
    p.add_argument("--as-of", default="", help="YYYY-MM-DD: each entry's value on that day")
    p = sub.add_parser("remember")
    for a in ("thing", "attribute", "value"):
        p.add_argument(a)
    p.add_argument("--since", default=""); p.add_argument("--correction", action="store_true")
    p = sub.add_parser("name"); p.add_argument("thing"); p.add_argument("names", nargs="*")
    p.add_argument("--kind", default=""); p.add_argument("--remove", nargs="*", default=[])
    p.add_argument("--merge", action="store_true", help="the names are separate profiles of this thing: join them")
    p = sub.add_parser("forget", help="delete a thing, or one entry with --attribute (undo: restore)")
    p.add_argument("thing"); p.add_argument("--attribute", default=""); p.add_argument("--reason", default="asked at the CLI")
    sub.add_parser("restore", help="undo the latest forget of a thing").add_argument("thing")
    sub.add_parser("forgotten", help="the latest forgets").add_argument("-n", type=int, default=20)
    sub.add_parser("purge", help="delete a forgotten thing's trash for good (cannot be undone)").add_argument("thing")
    sub.add_parser("read").add_argument("ref")
    dp = sub.add_parser("doctor")
    dp.add_argument("--fix", action="store_true")
    dp.add_argument("--review", type=int, nargs="?", const=30, default=0,
                    help="the N most suspect entries with newer page evidence, tab-separated")
    sub.add_parser("sessions").add_argument("--days", type=int, default=2)
    p = sub.add_parser("import"); p.add_argument("--limit", type=int, default=0)
    p.add_argument("--full", action="store_true", help="the full prompt instead of the lean one")
    p.add_argument("--drill", default="", help="a migrate-log.jsonl: apply its rows as a staged run, no model "
                   "(only with --root, never the live store)")
    p.add_argument("--texts", default="", help="with --drill: the units' texts (migrate-texts.jsonl, or "
                   "a JSON object {\"note#chunk\": text})")
    p = sub.add_parser("learn", help="learn the finished chats not learned yet (python3 -m deltos.learn)")
    p.add_argument("--wait", action="store_true", help="what the automatic triggers pass (harness, nightly): "
                   "wait up to 20 min for the agent to go idle; runs only with [learn] auto")
    p.add_argument("--days", type=int, default=None, help="chats started in the last N days ([learn] days, 2)")
    p.add_argument("--max", type=int, default=None, help="chats per sweep ([learn] max_units, 20)")
    p.add_argument("--dry-run", action="store_true", help="list the units; no model, nothing written")
    p.add_argument("--no-idle", action="store_true", help="don't stop between chats when the agent wakes up")
    sub.add_parser("runs").add_argument("-n", type=int, default=10)
    p = sub.add_parser("run"); p.add_argument("action", choices=["show", "promote", "discard", "undo"])
    p.add_argument("id"); p.add_argument("--no-hold", action="store_true",
                                         help="undo: don't write the hold file (its units may import again)")
    sub.add_parser("status").add_argument("--line", action="store_true", help="one line (the nightly log)")
    sub.add_parser("serve")
    sub.add_parser("hook").add_argument("harness", choices=["claude-code"])
    a = ap.parse_args(argv)
    # Who writes: you at `deltos remember`; the agent behind `deltos serve` otherwise.
    m = Memory(a.root, agent=a.agent or {"remember": HAND, "serve": "agent"}.get(a.cmd, ""))

    try:
        if a.cmd == "context":
            print(m.context(" ".join(a.text)))
        elif a.cmd in ("recall", "search"):
            tool, key = ("memory_recall", "query") if a.cmd == "recall" else ("page_search", "text")
            print(m.call(tool, {key: " ".join(a.text)}))
        elif a.cmd == "show":
            print(m.call("memory_show", {"thing": a.thing, "history": a.history, "as_of": a.as_of}))
        elif a.cmd == "remember":
            print(m.call("remember", {"thing": a.thing, "attribute": a.attribute, "value": a.value,
                                      "since": a.since, "correction": a.correction}))
        elif a.cmd == "name":
            print(m.call("name_thing", {"thing": a.thing, "other_names": a.names, "kind": a.kind,
                                        "remove_names": a.remove, "merge": a.merge}))
        elif a.cmd == "forget":
            print(m.store().forget(a.thing, a.attribute, a.reason, source=a.agent))
        elif a.cmd == "restore":
            print(m.store().restore(a.thing, source=a.agent))
        elif a.cmd == "purge":
            print(m.store().purge(a.thing))
        elif a.cmd == "forgotten":
            for e in m.store().forgotten(a.n):
                what = e["thing"] + (f" / {e['attribute']}" if e.get("attribute") else "")
                print(f"{e['at']}  {what}  by {e['source']}  — {e['reason']}  [{e['trash']}]"
                      + ("  (restored)" if e["restored"] else "") + ("  (purged)" if e["purged"] else ""))
        elif a.cmd == "read":
            print(m.call("page_read", {"ref": a.ref}))
        elif a.cmd == "doctor":
            from .doctor import doctor, review, things_of
            if a.review:
                print("thing\tattribute\tvalue\tsince\tlater_changes\tnewer_evidence")
                for r in review(things_of(m.root), datetime.date.today().isoformat(), m.pages(), a.review):
                    print("\t".join(str(r[k]) for k in ("thing", "attribute", "value", "since", "later_changes",
                                                         "newer_evidence")))
            else:
                print(doctor(m.root, fix=a.fix))
        elif a.cmd == "sessions":
            from .sources import hermes
            today, n = datetime.date.today(), 0
            for kind, home in m.config.sessions.items():
                if kind == "hermes":
                    n += sum(hermes.write_day(m.root / "sessions", today - datetime.timedelta(days=i), home)
                             for i in range(a.days))
            print(f"{n} sessions over {a.days} day(s) -> {m.root / 'sessions'}")
        elif a.cmd == "import":
            _half_promoted(m.root)
            return _import(m, a)
        elif a.cmd == "learn":
            from . import learn
            argv = (["--root", a.root] if a.root else []) + [f"--{k}" for k in ("wait", "dry-run", "no-idle")
                                                             if getattr(a, k.replace("-", "_"))]
            for k in ("days", "max"):
                if getattr(a, k) is not None:
                    argv += [f"--{k}", str(getattr(a, k))]
            return learn.main(argv)
        elif a.cmd == "runs":
            _half_promoted(m.root)
            from . import runs
            for r in runs.ledger(m.root)[-a.n:]:
                why = "; ".join(r.get("reasons") or []) or r.get("error", "")
                print(f"{r.get('at', '')}  {r.get('id')}  {r.get('kind')}  {r.get('decision')}"
                      + (f" by {r['by']}" if r.get("by") else "") + (f"  facts {r['facts']}" if "facts" in r else "")
                      + (f"  {why}" if why else ""))
        elif a.cmd == "run":
            return _run(m, a)
        elif a.cmd == "status":
            from . import runs
            print(runs.status(m.root, line=a.line))
        elif a.cmd == "serve":
            from .mcp import serve
            serve(m)
        elif a.cmd == "hook":
            return _hook_claude_code(m)
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    return 0


def _say(s: str) -> None:
    print(s, flush=True)


def _half_promoted(root) -> None:
    from . import runs
    for rid in runs.promoting(root):
        print(f"run {rid} was left half-promoted (its promote stopped part way): `deltos run undo {rid}` "
              f"takes out what it wrote", file=sys.stderr)


def _import(m: Memory, a) -> int:
    """A staged run (deltos/runs.py): the import works on a copy of memory, its numbers are
    judged against this store's own accepted runs (deltos/health.py), and it is promoted onto
    memory or held. Exit 0: promoted, nothing to do, or already held; 3: held now; 1: failed
    (memory untouched)."""
    import hashlib
    from . import health, llm, runs
    from . import items as _items
    from .config import Config
    from .sources import folder, notes
    c = m.config
    if a.drill:
        live = Config.load(None).root
        if not a.root or Path(a.root).expanduser().resolve() == live.expanduser().resolve():
            print(f"refused: --drill writes into the store it is given; give --root <a copy>, never {live}",
                  file=sys.stderr)
            return 1
        rows = [json.loads(ln) for ln in open(a.drill) if ln.strip()]
        texts = runs.load_texts(a.texts) if a.texts else {}
        fp = health.fingerprint({"drill": str(Path(a.drill).resolve())})
        return runs.execute(m.root, "note", runs.replay_work(rows, texts), label="import", say=_say,
                            owner=c.owner, fingerprint=fp)
    if not c.import_repos and not c.import_folders:
        print("nothing to import: no [import] repos or folders in deltos.toml")
        return 0
    # The jobs come from live memory's log, before the stage is copied: what is done is what was promoted.
    # Git repos and note folders go into one run; each folder's snapshot advances only with it (carry).
    done = notes.done_sessions(m.root)
    jobs = notes.git_jobs(c.import_repos, c.import_skip, done=done) if c.import_repos else []
    folders = [folder.Folder(f, m.root, c.import_skip) for f in c.import_folders]
    for f in folders:
        print(f.summary())
    jobs = sorted(jobs + [j for f in folders for j in f.jobs(done)])
    jobs = jobs[:a.limit] if a.limit else jobs
    if not jobs:
        print("nothing to import")
        return 0
    llm.configure(**c.llm)

    def work(stage: Path) -> None:
        it = None
        if c.import_items:
            it = _items.Items(_items.load(stage / "items.json"), asker=lambda evs: _items.ask(evs, llm.chat))
        notes.run(stage, jobs, c.import_workers, lean=not a.full, say=_say, owner=c.owner,
                  notes_prompt=c.import_notes_prompt, items=it, said=stage / "items-said.json" if it else None,
                  temperature=c.import_temperature, missed=c.import_missed)
        for f in folders:   # the days whose pieces are all logged: their snapshot rides in stage/.carry
            f.carry(stage)
        if it is not None:
            _items.save(stage / "items.json", it.cache)
            kept = sum(e["decision"] == "another" for e in it.events)
            print(f"items: {len(it.events)} replacements seen, {it.asked} asked, {kept} kept as another one", flush=True)

    prompt = {"v1": notes.PROMPT_V1, "v4": notes.PROMPT_V4}.get(c.import_notes_prompt, notes.PROMPT)
    fp = health.fingerprint({"url": c.llm.get("url", ""), "model": c.llm.get("model", ""),
                             "temperature": c.import_temperature,
                             "prompt": hashlib.sha1(prompt.encode()).hexdigest()})
    # runs.dated: each row keeps the day its note was written, so promote dates the facts the way the stage did.
    return runs.execute(m.root, "note", runs.dated(work, jobs), label="import", say=_say, owner=c.owner,
                        fingerprint=fp)


def _run(m: Memory, a) -> int:
    """deltos run show|promote|discard|undo <id>. Promote, discard and undo wait for no run: they
    refuse while one is in progress."""
    from . import runs
    if a.action != "undo":
        _half_promoted(m.root)
    try:
        if a.action == "show":
            print(runs.show(m.root, a.id))
            return 0
        with runs.run_lock(m.root) as free:
            if not free:
                print("another run in progress", file=sys.stderr)
                return 1
            if a.action == "promote":
                j = runs.journal(m.root, a.id)
                if j.get("reasons"):
                    print(f"overriding the verdict: {'; '.join(j['reasons'])}")
                runs.promote(m.root, a.id, by="hand", say=_say)
            elif a.action == "discard":
                runs.discard(m.root, a.id, say=_say)
            else:
                runs.undo(m.root, a.id, hold=not a.no_hold, say=_say)
    except runs.RunError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    return 0


def _hook_claude_code(m: Memory) -> int:
    """Claude Code UserPromptSubmit: the prompt arrives as JSON on stdin; the
    memory block goes back as additionalContext. Never blocks the prompt."""
    try:
        prompt = json.load(sys.stdin).get("prompt", "")
        block = m.context(prompt) if prompt.strip() else ""
    except Exception as e:   # a memory failure must not stop the user's message
        print(f"deltos hook: {e!r}", file=sys.stderr)
        return 0
    if block:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                 "additionalContext": "## Memory (deltos)\n" + block}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
