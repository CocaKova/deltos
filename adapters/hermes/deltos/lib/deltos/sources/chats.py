"""Finished chats as import units: what the owner told the agent, learned once a chat ends.

  python3 -m deltos.learn [--dry-run]     (the sweep; deltos/learn.py)

A stock Hermes install has no notes to import: its write path is the agent's
own tool writes, and its conversations. A conversation becomes one unit once
it is finished (closed, or quiet QUIET_MIN minutes), read the way LongMemEval
measured it: the user's turns whole, each assistant turn's opening only (what
the user is answering). On GLM-5.3-Flash that reading scored 285 right / 17
wrong over the 332 held-out questions; the prompt is the benchmark's, with
the owner's name in it (bench/lme.py gets its own back byte for byte).

Each session keeps a watermark: the last message id learned ('upto' in its
migrate-log rows), so a chat picked up again later gives a unit of its new
turns only (a judge found 8 of 157 sessions resumed after more than 30 min).
state.db is opened read-only. Cron, kanban and tool sessions are not chats;
'Reply OK' probes (one ask, no file written, a few words back) are skipped by
shape (15 of the 100 chat sessions of two weeks, on 10-19).
"""
from __future__ import annotations

import datetime
import re
import sqlite3
from pathlib import Path

from .hermes import SECRET, SKIP_SOURCES, trivial
from .notes import MAX_CHARS

QUIET_MIN = 30          # minutes without a message: a chat with no end mark is finished
IDLE_SECS = 120         # seconds without a message in any profile: the agent is idle (deltos/learn.py)
MSG_CHARS = 2000        # a user turn is read whole up to this (a pasted log is not the owner's words)
ASSISTANT_HEAD = 200    # an assistant turn's opening: what the user is answering (LongMemEval's reading)

# The chat prompt: bench/lme.py's EXTRACT_PROMPT until 10-19, with the owner's name where
# the benchmark's stood (13 places); given the benchmark's owner it is that text byte for
# byte (sha1 445190dd, 1,667 chars). {day} and {known} are filled per session by the Migrator.
_PROMPT = """You are adding a conversation to a structured memory about the user, <OWNER>.
The conversation took place on {day}. List the facts it states about <OWNER> and the concrete
things in <OWNER>'s life (people, pets, places, possessions, work, projects, events, accounts, habits,
preferences, numbers they track).

Reply with lines only, no JSON, no commentary:
= <thing> | <other names, separated by ;> | <kind>
<thing> | <attribute> | <value> | <since YYYY-MM-DD>
<thing> | <attribute> | <value> | <since YYYY-MM-DD> | correction

- <OWNER>'s own facts go on the thing "<OWNER>". <OWNER>'s unnamed belongings, plans and routines are
  attributes of <OWNER> ("<OWNER> | car | 2018 Honda Civic", "<OWNER> | Europe trip destination | Rome"),
  never a thing called "<OWNER>'s car". A person, pet or place with a name is its own thing
  ("Max", a dog), with "relationship" saying what it is to <OWNER> (my dog, my sister).
- Attributes are short labels someone would ask about (job, location, personal best 5K time,
  favorite cuisine). One label holds one fact: two different facts get two labels, and a fact
  that changes keeps its label. Values are short: the value only.
- "since" is the date the value became true; if the conversation gives none, use {day}.
- If a value replaced an older one, give both with their dates. Use "| correction" only when
  <OWNER> says an earlier value was wrong.
- Only facts <OWNER> would expect you to remember later. Skip the assistant's general advice.

What memory already holds about the things this conversation mentions (use an entry's label
exactly when it is the same fact, so the new value replaces the old one; a new value is an
update with its date, not a correction):
{known}"""


# v2: what a working chat adds to the benchmark's reading. The benchmark's chats are
# someone telling an assistant about their life; an agent's owner mostly gives it work,
# and the first nightly (11-06) stored a test's instructions as the owner's habits ("text
# message reading habit: last 5 messages from past 2 days"). Appended, so v1 stays the
# benchmark's text byte for byte.
_WORK = """
- A request for this conversation's task ("fix this bug", "summarize my last 3 emails",
  "try sending a test") is not a fact about <OWNER> or a habit. A standing rule <OWNER> states (always,
  never, from now on, every time) is a fact: give it a label ending in "rule".
- What the assistant did or found is not <OWNER>'s word: take a fact from an assistant line
  only when <OWNER> confirms it. Lines marked "relay (<writer>)" were written by another
  agent; take no fact from them.
- A value that changed during the conversation: give where it ended."""
PROMPTS = ("v1", "v2")


def chat_prompt(owner: str, version: str = "v1") -> str:
    """The chat prompt for this owner (replace, not format: {day} and {known} stay for the
    Migrator). v1 is the benchmark's; v2 adds _WORK before the memory reference."""
    if version not in PROMPTS:
        raise ValueError(f"unknown chat prompt {version!r} (one of {', '.join(PROMPTS)})")
    text = _PROMPT
    if version == "v2":
        head, sep, tail = text.partition("\n\nWhat memory already holds")
        text = head + _WORK + sep + tail
    return text.replace("<OWNER>", owner)


def _dbs(home: Path) -> list[tuple[Path, str]]:
    """Every profile's state.db: <home>/state.db is 'default', <home>/profiles/<name>/state.db is <name>."""
    home = Path(home).expanduser()
    return [(home / "state.db", "default")] + [(p, p.parent.name) for p in sorted((home / "profiles").glob("*/state.db"))]


def _said(db: sqlite3.Connection) -> str:
    """Rows Hermes writes itself into a conversation: a compaction summary or a system notice
    filed under 'user' (a finished background process, a model switch: 57 of 2,460 user rows
    on 10-19) is the agent's text, not the owner's words, and only the owner's words may
    ground a fact (two weeks of chats: 8,712 chars in 8 units). A steer is the user typing
    mid-turn and stays.
    A turn the user took back (a rewind, an edit, a regenerate) stays on disk with active = 0
    and compacted = 0, and so does the original of a turn a compaction carried forward as a
    fresh copy; neither is read (10-19: the one in-place-compacted session held 2 rewound
    user rows and 24 such originals). A turn summarized away (compacted = 1) is still the
    owner's words and is read. Older state.dbs have no such columns: every row is a turn."""
    cols = {r[1] for r in db.execute("pragma table_info(messages)")}
    keep = []
    if "_compressed_summary" in cols:
        keep.append("coalesce(_compressed_summary, 0) = 0")
    if "display_kind" in cols:
        keep.append("(display_kind is null or display_kind = 'steer')")
    if {"active", "compacted"} <= cols:
        keep.append("(coalesce(active, 1) = 1 or compacted = 1)")
    return "".join(" and " + k for k in keep)


# Who wrote a user turn, read from the turn itself: a harness files its own notices,
# summaries and restatements as user turns, and an agent that briefs another agent
# writes in the user's seat. 11-06: of the 98 lines the first nightly learned from 12
# chats, 66 came from chats whose user turns were another agent's briefs (its
# imperatives became the owner's facts) and 5 more from a harness's restated request;
# the sessions table told them apart in nothing (same source, same user id).
#   harness  a turn that opens with a bracketed tag in capitals ("[CONTEXT COMPACTION
#            ...]", "[STILL IN PROGRESS ...]", "[SYSTEM NOTICE ...]") or with one of
#            NOTICES: the harness speaking; not read. A tag that says it carries the
#            user's message ("[OUT-OF-BAND USER MESSAGE ...]") is opened, and what it
#            carries is read by these same rules.
#   relay    a turn that names its writer, "[From <writer>...]": another agent's words,
#            shown as context (its opening, like an assistant turn) and never a ground
#            for a fact (deltos/ground.py). "[From <owner>, relayed by <agent>]" is the
#            agent's too: it writes about the owner ("Sam tapped Yes") and mixes in its
#            own orders for the task; only what the owner typed is the owner's.
#   owner    everything else.
# Two or more words in capitals, or one with a dash and its explanation: "[TODO] buy milk"
# and "[URGENT]" are someone typing.
HARNESS_TAG = re.compile(r"^\s*\[/?(?:[A-Z][A-Z0-9/'&-]+(?: [A-Z0-9/'&-]+)+(?=\s*[\]—–:(])|[A-Z][A-Z0-9/'&-]{2,}\s+[—–]\s)")
CARRIER = re.compile(r"\bUSER MESSAGE\b")
NOTICES = ("You've reached the maximum number of tool-calling iterations",)
RELAY = re.compile(r"^\s*\[(?:From|Message from|Sent by|Written by)\s+(?P<who>[^,.\]—–]+?)\s*(?=[,.\]—–])[^\]]*\]\s*",
                   re.I)


def _carried(content: str) -> str:
    """What a carrier tag holds: the text after its opening line, up to its closing tag."""
    body = content.split("\n", 1)[1] if "\n" in content else ""
    return re.split(r"(?m)^\s*\[/[A-Z][^\]]*\]\s*$", body, maxsplit=1)[0].strip()


def author(content: str, owner: str = "") -> tuple[str, str, str]:
    """(kind, writer, text) of a user turn: kind 'owner', 'relay' or 'harness'
    (the rules above); text is the turn without its tags."""
    text = content or ""
    for _ in range(3):   # a carrier inside a carrier
        if HARNESS_TAG.match(text) and CARRIER.search(text.split("\n", 1)[0]):
            text = _carried(text)
        else:
            break
    if HARNESS_TAG.match(text) or text.startswith(NOTICES):
        return "harness", "", ""
    m = RELAY.match(text)
    if m:
        who = m.group("who").strip()
        by = re.search(r"\brelayed by\s+([^,.\]—–]+)", m.group(0), re.I)
        if by:
            return "relay", by.group(1).strip(), text[m.end():].strip()
        rest = text[m.end():].strip()
        mine = {"user", "the user"} | ({w.lower() for w in owner.split()[:1]} | {owner.lower()} if owner else set())
        if who.lower() in mine or who.lower().split()[0] in mine:
            return "owner", who, text   # the owner's, as typed
        return "relay", who, rest
    return "owner", owner, text


def _line(role: str, content: str, owner: str = "") -> str:
    """One turn as the unit reads it ('' when it is not read at all)."""
    if role == "user":
        kind, who, content = author(content, owner)
        if kind == "harness" or not content:
            return ""
        content = SECRET.sub("[redacted]", content)
        if kind == "owner":
            return "user: " + content[:MSG_CHARS]
        return f"relay ({who}): {content[:ASSISTANT_HEAD]}{' ...' if len(content) > ASSISTANT_HEAD else ''}"
    content = SECRET.sub("[redacted]", content)
    return f"assistant: {content[:ASSISTANT_HEAD]}{' ...' if len(content) > ASSISTANT_HEAD else ''}"


def _pack(head: str, turns: list[str], header: str) -> list[str]:
    """The unit's text in pieces of at most MAX_CHARS, cut only between turns; every piece
    after the first starts with `header`, then a turn's own 'user: ' or 'assistant: '.
    Until 10-19 a long chat was cut by notes._chunks at any line end: over all history 18
    of its 38 cuts fell inside a turn (7 inside an assistant's), so the next piece opened
    with the rest of that turn under no speaker, and a reading of whose line it is counted
    the assistant's words as the owner's. A turn is at most MSG_CHARS and its prefix, far
    under a piece; one longer (a larger MSG_CHARS) is cut, and its rest keeps its speaker."""
    text = head + "\n".join(turns)
    if len(text) <= MAX_CHARS:
        return [text]
    room = MAX_CHARS - max(len(head), len(header))
    out, cur, size = [], [], 0
    for t in turns:
        while len(t) > room:
            who = t[:t.index(": ") + 2]
            cut = t.rfind(" ", len(who), room) + 1 or room
            if cur:
                out.append(cur)
                cur, size = [], 0
            out.append([t[:cut]])
            t = who + t[cut:]
        if cur and size + len(t) > room:
            out.append(cur)
            cur, size = [], 0
        cur.append(t)
        size += len(t) + 1
    out.append(cur)
    return [head + "\n".join(out[0])] + [header + "\n".join(p) for p in out[1:]]


def units(home: Path, live_rows: list[dict], now: float, days: int = 2, skip_ids=None,
          stats: dict | None = None, owner: str = "") -> list[dict]:
    """The finished chats of every profile not yet learned, oldest first, one unit each:
    {note: 'chat:<profile>/<session id>', day, pieces, upto: the last message id, of}.
    live_rows: the store's migrate-log rows of kind 'chat' (their 'upto' is each session's
    watermark). Sessions started in the last `days` days are looked at, and older ones
    with a watermark that have new messages. skip_ids: a regex of session ids to skip
    ([hermes] skip_ids). stats, when given, counts sessions seen and why each was skipped.
    owner: the owner's name, so "[From <owner>, relayed by ...]" reads as theirs (author()).

    A chain is one conversation: when a harness compacts a chat it ends the session and
    goes on in a child (parent_session_id), and the parent's last turns are the middle of
    the work, not where it ended (11-06: a client task learned in three units, each with
    the states of that moment: "pending", "stuck on a bot challenge"). A session with a
    child is learned with it, once the last session of the chain is finished: one unit,
    the chain's unread turns in order, its row's 'chain' carrying each earlier session's
    watermark."""
    skip = (re.compile(skip_ids) if isinstance(skip_ids, str) else skip_ids) if skip_ids else None
    upto: dict[str, int] = {}
    for r in live_rows:
        if r.get("upto") is not None:
            upto[r["note"]] = max(upto.get(r["note"], 0), int(r["upto"]))
        for note, mark in (r.get("chain") or {}).items():
            upto[note] = max(upto.get(note, 0), int(mark))
    st = stats if stats is not None else {}
    out = []
    for path, profile in _dbs(home):
        if not path.is_file():
            continue
        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error:
            st["unreadable"] = st.get("unreadable", 0) + 1
            continue
        try:
            said = _said(db)
            chained = "parent_session_id" in {r[1] for r in db.execute("pragma table_info(sessions)")}
            cols = "id, source, ended_at, " + ("parent_session_id" if chained else "null")
            known = [k.split("/", 1)[1] for k in upto if k.startswith(f"chat:{profile}/")]
            rows = db.execute(f"select {cols} from sessions where started_at >= ?",
                              (now - days * 86400,)).fetchall()
            seen = {r[0] for r in rows}
            for i in range(0, len(known), 500):
                part = [s for s in known[i:i + 500] if s not in seen]
                if part:
                    rows += db.execute(f"select {cols} from sessions where id in ({','.join('?' * len(part))})",
                                       part).fetchall()
            for sid, source, ended, parent in rows:
                st["seen"] = st.get("seen", 0) + 1
                note = f"chat:{profile}/{sid}"
                if chained and db.execute("select 1 from sessions where parent_session_id = ? limit 1",
                                          (sid,)).fetchone():
                    st["continued"] = st.get("continued", 0) + 1   # learned with the chain's last session
                    continue
                why, msgs = _turns(db, said, sid, source, ended, upto.get(note, 0), now, skip, parent)
                chain = {}
                if not why or why in ("learned", "copies", "no user", "trivial"):
                    msgs, chain = _chain(db, said, profile, parent, upto, msgs if not why else [])
                    why = "" if msgs and any(r == "user" for _, r, _, _ in msgs) else why or "no user"
                if why:
                    st[why] = st.get(why, 0) + 1
                    continue
                lines = [ln for ln in (_line(r, c, owner) for _, r, c, _ in msgs) if ln]
                if not any(ln.startswith("user: ") for ln in lines):
                    st["no owner words"] = st.get("no owner words", 0) + 1
                    continue
                last_ts = msgs[-1][3] or now
                day = datetime.date.fromtimestamp(last_ts).isoformat()
                pieces = _pack(f"Conversation on {day}:\n\n", lines, f"--- chat {sid} (continued)\n")
                u = {"note": note, "day": day, "pieces": pieces, "upto": msgs[-1][0], "of": len(pieces)}
                if chain:
                    u["chain"] = chain
                    upto.update(chain)   # a session branched into several: its turns go with the first
                out.append((last_ts, u))
        except sqlite3.Error:   # a locked, broken or foreign db: the other profiles still count
            st["unreadable"] = st.get("unreadable", 0) + 1
        finally:
            db.close()
    st["units"] = st.get("units", 0) + len(out)
    return [u for _, u in sorted(out, key=lambda x: (x[0], x[1]["note"]))]


def _turns(db, said, sid, source, ended, mark, now, skip, parent=None) -> tuple[str, list]:
    """Why a session gives no unit, or ('', its new turns as (id, role, content, timestamp)).
    A turn already read is not read again when Hermes writes it anew: its compaction re-inserts
    the turns it carries forward (new ids, same role, content and timestamp), in place after
    the originals or at the start of a child session (parent_session_id). 10-19, two weeks:
    35 of the 75 turns of the 6 child units were their parent's, one child all 21 of its."""
    if source in SKIP_SOURCES:
        return "source", []
    if skip is not None and skip.search(sid):
        return "skip_ids", []
    top, last = db.execute("select max(id), max(timestamp) from messages where session_id = ?", (sid,)).fetchone()
    if top is None:
        return "empty", []
    if ended is None and (last is None or now - last < QUIET_MIN * 60):
        return "active", []
    if top <= mark:   # nothing past the watermark: no need to read the chat again
        return "learned", []
    rows = db.execute(f"select id, role, content, tool_calls, timestamp from messages where session_id = ? "
                      f"and role in ('user', 'assistant'){said} order by id", (sid,)).fetchall()
    old = set()   # (role, content, timestamp) of the turns read before: below the watermark, or the parent's
    turn = "select role, content, timestamp from messages where role in ('user', 'assistant') and session_id = ?"
    if mark:
        old.update(db.execute(turn + " and id <= ?", (sid, mark)).fetchall())
    if parent:
        old.update(db.execute(turn, (parent,)).fetchall())
    msgs = [(i, r, c, ts) for i, r, c, _, ts in rows if i > mark and c and c.strip() and (r, c, ts) not in old]
    if not msgs:
        return ("learned" if mark else "copies" if old else "no user"), []
    if not any(r == "user" for _, r, _, _ in msgs):
        return "no user", []
    if trivial([(r, c, t) for _, r, c, t, _ in rows]):
        return "trivial", []
    return "", msgs


def _chain(db, said, profile, parent, upto, msgs) -> tuple[list, dict]:
    """The turns of a finished chain not read yet, oldest session first: each earlier
    session's (above its own watermark), then the last session's own new turns (msgs,
    from _turns: its copies of earlier turns already left out). Returns (turns, the
    earlier sessions' new watermarks by note)."""
    if not parent:
        return msgs, {}
    line, seen = [], set()
    while parent and parent not in seen and len(seen) < 50:
        seen.add(parent)
        line.append(parent)
        row = db.execute("select parent_session_id from sessions where id = ?", (parent,)).fetchone()
        parent = row[0] if row else None
    turns, chain, have = [], {}, set()
    for sid in reversed(line):
        note = f"chat:{profile}/{sid}"
        mark = upto.get(note, 0)
        rows = db.execute(f"select id, role, content, timestamp from messages where session_id = ? and id > ? "
                          f"and role in ('user', 'assistant'){said} order by id", (sid, mark)).fetchall()
        rows = [(i, r, c, ts) for i, r, c, ts in rows if c and c.strip() and (r, c, ts) not in have]
        if rows:
            chain[note] = rows[-1][0]
            have.update((r, c, ts) for _, r, c, ts in rows)
            turns += rows
    return turns + [m for m in msgs if (m[1], m[2], m[3]) not in have], chain
