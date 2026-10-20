"""Session digests: what each chat asked, made and ended with, brain-free.

  deltos sessions [--days 30]          (writes <root>/sessions from [sessions] hermes = "~/.hermes")

Work done in a chat only reaches memory if someone writes it down, and most
of it never is: 'the sprite sheet we were making' (a bandit warrior, made
10-04 in ~/workspace/bandit-warrior) existed only in two chat histories, so
recall had nothing and the agent guessed. One markdown file per day lists every
session of every Hermes profile (cron runs and one-line probes excluded): the
first request, the files it wrote, the last answer. The memory plugin searches
these files as pages (sessions/...). Nightly; rewriting a day is idempotent.
"""
from __future__ import annotations

import datetime
import json
import re
import sqlite3
import sys
from pathlib import Path

SKIP_SOURCES = {"cron", "kanban", "tool"}
ASK_CHARS, END_CHARS, MAX_FILES = 400, 400, 10
TRIVIAL_END = 20   # chars: a last answer shorter than this to a single ask says nothing ('OK')


# Keys and tokens pasted into a chat stay out of the digest.
SECRET = re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|"
                    r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_.-]+|[A-Fa-f0-9]{40,}|(?=[A-Za-z0-9+_-]*[A-Z])(?=[A-Za-z0-9+_-]*[0-9])[A-Za-z0-9+_-]{40,}={0,2})")


def _one_line(text: str, n: int) -> str:
    text = SECRET.sub("[redacted]", re.sub(r"\s+", " ", text or "").strip())
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + " …"


def _paths(tool_calls: str) -> list[str]:
    out = []
    try:
        for c in json.loads(tool_calls or "[]"):
            fn = c.get("function", {})
            if fn.get("name") in ("write_file", "patch", "edit_file"):
                p = json.loads(fn.get("arguments") or "{}").get("path")
                if p:
                    out.append(p)
    except (ValueError, AttributeError, TypeError):
        pass
    return out


def trivial(msgs: list[tuple]) -> bool:
    """A probe, not a chat: one ask, no file written, a few words back ('Reply OK' -> 'OK').
    msgs are (role, content, tool_calls) rows. 10-19: 15 of the 186 sessions of two weeks
    (111 of 1,450 ever), each a 'reply OK' / 'pong' check or an ask that got no answer.
    Until then the digests skipped probes by an id pattern only one install's probes had.
    An ask with no answer at all is not a probe: its only reply failed (asks of up to 2,502
    chars on 10-19), and the owner's words in it still count."""
    asks = [c for r, c, _ in msgs if r == "user" and c and c.strip()]
    if len(asks) != 1 or any(_paths(t) for r, _, t in msgs if r == "assistant"):
        return False
    answers = [c.strip() for r, c, _ in msgs if r == "assistant" and c and c.strip()]
    return bool(answers) and len(answers[-1]) < TRIVIAL_END


def digest(db_path: Path, profile: str, day: datetime.date, skip_ids: str | None = None) -> list[tuple[float, str]]:
    """skip_ids: a regex of session ids to leave out ([hermes] skip_ids, e.g. a bench's probes)."""
    skip = re.compile(skip_ids) if skip_ids else None
    start = datetime.datetime.combine(day, datetime.time()).timestamp()
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = db.execute("select id, source, title, started_at from sessions where started_at >= ? and started_at < ?",
                      (start, start + 86400)).fetchall()
    out = []
    for sid, source, title, started in rows:
        if source in SKIP_SOURCES or (skip and skip.search(sid)):
            continue
        msgs = db.execute("select role, content, tool_calls from messages where session_id=? order by id", (sid,)).fetchall()
        asks = [c for r, c, _ in msgs if r == "user" and c]
        if not asks or trivial(msgs):
            continue
        answers = [c for r, c, t in msgs if r == "assistant" and c and c.strip()]
        files = list(dict.fromkeys(p for r, _, t in msgs if r == "assistant" for p in _paths(t)))
        head = f"## {datetime.datetime.fromtimestamp(started):%H:%M} · {profile} · {source}" + (f" · {title}" if title else "")
        lines = [head, f"- asked: {_one_line(asks[0], ASK_CHARS)}"]
        if len(asks) > 1:
            lines.append(f"- then ({len(asks) - 1} more): {_one_line(asks[-1], 200)}")
        if files:
            lines.append("- wrote: " + ", ".join(files[:MAX_FILES]) + (f" (+{len(files) - MAX_FILES})" if len(files) > MAX_FILES else ""))
        if answers:
            lines.append(f"- ended: {_one_line(answers[-1], END_CHARS)}")
        lines.append(f"- session: {sid}")
        out.append((started, "\n".join(lines)))
    return out


def write_day(out_dir: Path, day: datetime.date, home: Path = Path.home() / ".hermes",
              skip_ids: str | None = None) -> int:
    """Every profile's sessions of one day: <home>/state.db (the default profile) and
    <home>/profiles/*/state.db."""
    home = Path(home).expanduser()
    dbs = [(home / "state.db", "default")] + [(p, p.parent.name) for p in sorted((home / "profiles").glob("*/state.db"))]
    entries = []
    for db, profile in dbs:
        try:
            entries += digest(db, profile, day, skip_ids)
        except sqlite3.Error:
            continue
    if not entries:
        return 0
    out_dir.mkdir(parents=True, exist_ok=True)
    body = "\n\n".join(e for _, e in sorted(entries))
    (out_dir / f"{day}.md").write_text(
        f'---\ntitle: "Chat sessions {day}"\nupdated: {day}\n---\n\n'
        f"What each chat session on {day} asked, wrote and ended with (all profiles; cron excluded). "
        f"Generated from the session histories.\n\n{body}\n")
    return len(entries)
