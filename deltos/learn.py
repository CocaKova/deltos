"""Learning as it goes: each finished chat read once, into the same staged runs as the notes.

  python3 -m deltos.learn [--root R] [--wait] [--days N] [--max N] [--dry-run] [--no-idle]

The default user has one Hermes agent and no notes, so their memory has two
writers: the agent's own tool writes (no model), and their conversations.
A conversation is learned after it ends, while the agent is idle, one model
request at a time (deltos/sources/chats.py says what a unit reads). Every
automatic trigger passes `--wait`: the harness when a session ends and when
the gateway starts (orphans: chats that ended while nothing was listening),
the nightly after its import. Only those need [learn] auto; the sweep waits
for the agent to go idle and stops between units when the agent wakes up.

Every sweep is a staged run (deltos/runs.py): the units go into a copy of the
store, a health verdict compares the run with this store's accepted chat
runs, and the rows reach live memory only when promoted (a held run waits in
learn.hold with its numbers). The watermark (each row's 'upto') advances
only with them, so a held or failed sweep reads the same turns next time.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
import urllib.error
from pathlib import Path

from . import items as _items
from . import llm
from .config import Config
from .sources import chats, notes

AUTO_DEFAULT = False  # [learn] auto unset: chats are learned by hand (`deltos learn`); auto = true starts it by itself
WAIT_POLL, WAIT_MAX = 30, 20 * 60   # --wait: look for idle every 30 s, give up after 20 min


def why_not(cfg: Config) -> str:
    """What learning still needs from deltos.toml ('' when nothing)."""
    if not cfg.llm.get("url"):
        return "learning needs a model: set [llm] url in deltos.toml"
    if not cfg.owner:
        return "set owner = <your name> in deltos.toml"
    if not cfg.sessions.get("hermes"):
        return 'learning reads Hermes chats: set [sessions] hermes = "~/.hermes" in deltos.toml'
    return ""


def enabled(cfg: Config) -> bool:
    """Whether chats are learned by themselves (the harness's trigger asks this)."""
    return bool(cfg.learn.get("auto", AUTO_DEFAULT)) and not why_not(cfg)


def _dbs(home: Path) -> list[Path]:
    home = Path(home).expanduser()
    return [home / "state.db"] + sorted((home / "profiles").glob("*/state.db"))


def idle(home: Path, secs: int = chats.IDLE_SECS) -> bool:
    """No message in any profile's state.db in the last `secs` seconds."""
    since = time.time() - secs
    for p in _dbs(home):
        if not p.is_file():
            continue
        try:
            db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            try:
                if db.execute("select 1 from messages where timestamp > ? limit 1", (since,)).fetchone():
                    return False
            finally:
                db.close()
        except sqlite3.Error:
            continue
    return True


def _append(path: Path, rows: list[dict]) -> None:
    if rows:
        with open(path, "a") as fh:
            fh.write("".join(json.dumps(r) + "\n" for r in rows))


def apply_units(stage: Path, units: list[dict], cfg: Config, say=print, busy=None) -> int:
    """The units into the store at stage, one model request at a time. A unit's rows and
    texts are logged together once all its pieces are in, so the watermark never covers
    a piece that wasn't read. A request error is logged and ends the sweep (the model is
    down); busy() true after a unit ends it too (the agent woke up). A refusal of one
    unit's request (an HTTP 4xx other than a timeout or a rate limit) is that unit's
    error only: logged, and the sweep goes on, so one chat the server always refuses
    doesn't keep every newer chat waiting behind it. Returns units done.
    No temperature is sent: 0 is what LongMemEval measured for this reading."""
    stage = Path(stage)
    llm.configure(**cfg.llm)
    it = (_items.Items(_items.load(stage / "items.json"), asker=lambda evs: _items.ask(evs, llm.chat))
          if cfg.import_items else None)
    m = notes.Migrator(stage, owner=cfg.owner, prompt=chats.chat_prompt(cfg.owner), chat=True, items=it, split=True)
    if it is not None:
        m._said = notes.load_said(stage / "items-said.json")
    done, t0 = 0, time.time()
    try:
        for u in units:
            rows, texts = [], []
            try:
                for i, piece in enumerate(u["pieces"]):
                    st = m.session(u["note"], u["day"], piece, u["day"])
                    rows.append({**st, "chunk": i, "of": u["of"], "upto": u["upto"], "kind": "chat"})
                    texts.append({"note": u["note"], "chunk": i, "text": piece, "upto": u["upto"]})
            except Exception as e:
                _append(stage / "migrate-errors.jsonl", [{"note": u["note"], "day": u["day"], "error": repr(e)[:200],
                                                          "kind": "chat", "chunk": len(rows)}])
                if _refused(e):
                    say(f"{u['day']} {u['note']} error: {repr(e)[:120]} (the server refused this chat; going on)")
                    continue
                down = isinstance(e, (urllib.error.URLError, TimeoutError, ConnectionError))
                say(f"{u['day']} {u['note']} error: {repr(e)[:120]} (stopping: "
                    + ("the model is not answering)" if down else "see migrate-errors.jsonl)"))
                break
            _append(stage / "migrate-log.jsonl", rows)
            _append(stage / "migrate-texts.jsonl", texts)
            done += 1
            say(f"{u['day']} {u['note']} facts={sum(r.get('facts', 0) for r in rows)} "
                f"refused={sum(r.get('refused', 0) for r in rows)}{''.join(notes._flags(r) for r in rows)} "
                f"{round(time.time() - t0)}s")
            if busy is not None and done < len(units) and busy():
                say(f"the agent is busy: stopping after {done} of {len(units)} chats")
                break
    finally:
        if it is not None:
            _items.save(stage / "items.json", it.cache)
            notes.save_said(stage / "items-said.json", m._said, m.store)
    return done


def _refused(e: Exception) -> bool:
    """An answer about this request, not about the server: a 4xx other than 408 or 429."""
    return isinstance(e, urllib.error.HTTPError) and 400 <= e.code < 500 and e.code not in (408, 429)


def _live_rows(root: Path) -> list[dict]:
    p = Path(root) / "migrate-log.jsonl"
    if not p.exists():
        return []
    out = []
    with open(p) as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:   # a line cut short by a crash: the rest still count
                continue
            if r.get("kind") == "chat":
                out.append(r)
    return out


def sweep(root=None, wait: bool = False, days: int | None = None, max_units: int | None = None,
          no_idle: bool = False, dry_run: bool = False, say=print) -> int:
    """Learn the finished chats not learned yet. wait: what every automatic trigger passes
    (the harness at a session end and at gateway start, the nightly after its import); it
    needs [learn] auto and waits up to 20 min for the agent to go idle. A run by hand
    needs only [llm], owner and [sessions] hermes. Returns the run's exit code
    (0 promoted, empty or nothing to do; 3 held; 1 failed).
    The units are read again inside the run, from the stage's log: another trigger's run
    may have promoted between this read and this run taking the store's lock, and its
    chats must not be learned twice."""
    cfg = Config.load(root)
    why = why_not(cfg)
    if not why and wait and not enabled(cfg):
        why = "learning is off ([learn] auto = false): `deltos learn` runs it by hand"
    if why:
        say(why)
        return 0
    home = cfg.sessions["hermes"]
    if wait:
        end = time.time() + WAIT_MAX
        while not idle(home):
            if time.time() >= end:
                say("the agent stayed busy for 20 min: no chats learned this time")
                return 0
            time.sleep(WAIT_POLL)
    stats: dict = {}
    days, cap = days or int(cfg.learn.get("days", 2)), max_units or int(cfg.learn.get("max_units", 20))
    skip = cfg.hermes.get("skip_ids") or None
    us = chats.units(home, _live_rows(cfg.root), time.time(), days=days, skip_ids=skip, stats=stats)
    n, us = len(us), us[:cap]
    seen = ", ".join(f"{k} {v}" for k, v in sorted(stats.items()) if k not in ("seen", "units"))
    say(f"{len(us)} chats to learn" + (f" ({n - len(us)} more wait for the next sweep)" if n > len(us) else "")
        + f"; {stats.get('seen', 0)} sessions seen, skipped: {seen or 'none'}")
    if not us:
        return 0
    if dry_run:
        for u in us:
            say(f"{u['note']}\t{u['day']}\t{sum(len(p) for p in u['pieces'])} chars\t{u['of']} piece(s)")
        return 0
    from . import health, runs
    prompt = hashlib.sha1(chats.chat_prompt(cfg.owner).encode()).hexdigest()

    def work(stage: Path) -> int:
        fresh = chats.units(home, _live_rows(stage), time.time(), days=days, skip_ids=skip)[:cap]
        gone = len({u["note"] for u in us} - {u["note"] for u in fresh})
        if gone:
            say(f"{gone} of these chats were learned by another run meanwhile")
        return apply_units(stage, fresh, cfg, say, busy=None if no_idle else (lambda: not idle(home)))
    return runs.execute(cfg.root, "chat", work, label="learn", say=say, owner=cfg.owner,
                        fingerprint=health.fingerprint({"prompt": prompt, "url": cfg.llm.get("url", ""),
                                                        "model": cfg.llm.get("model", "")}))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="deltos.learn", description="Learn the finished chats not learned yet.")
    ap.add_argument("--root", help="memory folder (default $DELTOS_ROOT or ~/deltos)")
    ap.add_argument("--wait", action="store_true", help="what every automatic trigger passes (harness, nightly): "
                    "wait (up to 20 min) for the agent to go idle; needs [learn] auto")
    ap.add_argument("--days", type=int, default=None, help="chats started in the last N days ([learn] days, 2)")
    ap.add_argument("--max", type=int, default=None, help="chats per sweep ([learn] max_units, 20)")
    ap.add_argument("--dry-run", action="store_true", help="list the units; no model, nothing written")
    ap.add_argument("--no-idle", action="store_true", help="don't stop between chats when the agent wakes up")
    a = ap.parse_args(argv)
    return sweep(a.root, wait=a.wait, days=a.days, max_units=a.max, no_idle=a.no_idle, dry_run=a.dry_run,
                 say=lambda s: print(s, flush=True))


if __name__ == "__main__":
    sys.exit(main())
