"""Memory: the one surface every way in uses (MCP server, CLI, harness adapters).

    m = Memory()                       # ~/deltos, or $DELTOS_ROOT, configured by deltos.toml
    m.context("where does the backup job run?")   # the per-turn block, ~50 ms, no model
    m.call("remember", {"thing": "Backup job", "attribute": "host", "value": "nas"})
    m.tools, m.instructions()          # what an agent is given

Facts live in <root>/notes (store.py), chat digests in <root>/sessions, and pages
in the configured folders (pages.py). Everything is markdown on disk.
"""
from __future__ import annotations

import datetime
import os
import time
from pathlib import Path

from .config import Config
from .pages import PageError, Pages, age, query_terms
from .recall import MAX_LINES, RECENT, Recall
from .store import Store

PAGES_PER_TURN = 3
LOOSE_PER_TURN = 3         # lines from other notes about the things a message names
# A follow-up ("I mean in the app", "which disk does it write to?") names nothing of
# what it is about: the previous message does. 10-19: "I mean in <app>" after "where
# are the cron jobs?" put the right page 13th (carried: 5th), and of 14 real two-turn
# follow-ups (bench/pagecheck.py) 1 had what it was about in the block, 9 carried.
# A message this short, or one that names nothing, is read with the previous
# message's words (pages); one that names nothing of its own (the owner's "my" aside)
# also with its things (facts). A short message that names its own thing ("restart
# box-9") is about that thing: the previous message's profiles would be the wrong
# subject (of 20 carried real turns, 12 gained a profile the agent read in neither
# turn when those carried facts too, 9 when they don't).
CARRY_WORDS = 6
CARRY_WEIGHT = 0.5         # a previous message's word weighs half a word of this one
RECENT_DAYS = 7            # "what did we work on recently?": pages and changes this far back
THINGS_PER_DAY = 10
FACT_CHARS = 6000          # cap on the facts part of a turn's block
SESSIONS_EVERY = 300       # seconds between refreshes of today's chat digest


class Refused(ValueError):
    """A request the memory refuses (bad input, read-only page): say it to the agent."""


def _fn(name: str, desc: str, props: dict, req: list) -> dict:
    return {"name": name, "description": desc, "parameters": {"type": "object", "properties": props, "required": req}}


S = {"type": "string"}


def remember_tool(another: bool) -> dict:
    """The remember tool. `another` (10-16): the agent can keep a second item of
    the same kind beside the first. Its text is a prompt, so it is on only once
    scored on two models (bench/savetool.py): DELTOS_REMEMBER_ANOTHER=1."""
    text = ("Record a fact: the thing's attribute now has this value. The old value moves to the "
            "thing's history automatically. Attribute = a short label someone would ask about (host, port, "
            "version, status, model, owner, location, runs on, part of, relationship, ...); reuse the thing's "
            "existing label for the same fact. One label holds one fact: two things true at once get two labels "
            "(school, job search), or the second replaces the first. ")
    if another:
        text += ("Another one of the same kind that doesn't replace the first (a second client, trip, pet, "
                 "course): another: true keeps both current. ")
    text += ("Value = the value only, short. A config key goes under 'setting <key>'. A status is a state in "
             "a few words (live, paused, released).")
    props = {"thing": S, "attribute": S, "value": S,
             "since": {"type": "string", "description": "YYYY-MM-DD the value became true; omit for today"},
             "correction": {"type": "boolean", "description": "true only if the previous value was a mistake and never true"}}
    if another:
        props["another"] = {"type": "boolean", "description": "true only if this is another one of the same kind and "
                            "the earlier one is still true (both stay current); not for a count or a state that changed"}
    return _fn("remember", text, props, ["thing", "attribute", "value"])


REMEMBER_ANOTHER = os.environ.get("DELTOS_REMEMBER_ANOTHER", "") == "1"


def show_tool(as_of: bool) -> dict:
    """The memory_show tool. `as_of` (10-16, time travel): the thing's values on
    one day. A prompt change, so on only once scored on two models
    (bench/asof.py): DELTOS_SHOW_AS_OF=1. The CLI's show --as-of always works."""
    text = ("Everything memory has on one thing: current entries with their dates and latest old "
            "values. history=true adds every older value.")
    props = {"thing": S, "history": {"type": "boolean", "description": "include every older value"}}
    if as_of:
        text += " as_of=YYYY-MM-DD gives each entry's value on that day instead."
        props["as_of"] = {"type": "string", "description": "YYYY-MM-DD: what was true on that day"}
    return _fn("memory_show", text, props, ["thing"])


SHOW_AS_OF = os.environ.get("DELTOS_SHOW_AS_OF", "") == "1"

TOOLS = [
    _fn("memory_recall", "Search memory: facts on the things a query names, plus matching pages. Use other words "
        "or the thing's other names when the first search misses.", {"query": S}, ["query"]),
    show_tool(SHOW_AS_OF),
    remember_tool(REMEMBER_ANOTHER),
    _fn("name_thing", "Register a thing, or other names people use for that same thing. What it has or uses "
        "(its host, vendor, owner, version) or what it is to someone (girlfriend, client) is not a name: record "
        "that with remember. Two profiles that are one thing: merge=true joins the profiles of other_names into "
        "this one. A name that belongs to a different thing (a wrong merge): remove_names takes it off, then "
        "name the right thing with it.",
        {"thing": S, "other_names": {"type": "array", "items": S},
         "merge": {"type": "boolean", "description": "other_names are separate profiles of this same thing: join them"},
         "remove_names": {"type": "array", "items": S, "description": "names to take off this thing"},
         "kind": {"type": "string", "description": "machine, service, model, app, repo, person, ..."}}, ["thing"]),
    _fn("forget", "Delete what should not be in memory at all: a test, junk, a thing or entry created by "
        "mistake, or something the user asked you to forget. Not for a value that changed (remember the new one) "
        "or one that was wrong (remember with correction: true). With attribute: that one entry and its old values. "
        "Without: the whole thing with its names and history. Nothing is destroyed: memory_restore undoes it. "
        "A whole thing with many entries, or one other things point to, also needs user_asked.",
        {"thing": S, "attribute": {"type": "string", "description": "one entry to forget; omit to forget the whole thing"},
         "reason": {"type": "string", "description": "why, in a few words (kept with the receipt)"},
         "user_asked": {"type": "boolean", "description": "true only when the user asked to forget this exact thing"}},
        ["thing", "reason"]),
    _fn("memory_restore", "Undo the latest forget of a thing or of one of its entries.", {"thing": S}, ["thing"]),
    _fn("page_search", "Exact (case-insensitive) text search across all pages, chat digests included; newest "
        "first. Good for ids, ports, paths, error strings.", {"text": S}, ["text"]),
    _fn("page_read", "Read a whole page. ref = a [page] label from memory_recall or page_search, or a [[link]] "
        "from a memory result (e.g. <thing>-history: a thing's older values).", {"ref": S}, ["ref"]),
    _fn("page_write", "Write a new note page (decision, how-to, incident write-up, anything longer than a "
        "fact). First line = title. Not for correcting a page: use page_edit.", {"content": S}, ["content"]),
    _fn("page_edit", "Fix a page in place. With old_text: that exact span (must match once) becomes new_text. "
        "Without: the whole body is replaced. Read-only pages refuse.",
        {"ref": S, "new_text": S, "old_text": S}, ["ref", "new_text"]),
    _fn("page_forget", "Remove a page (duplicate, superseded, or asked to). Moved to .trash, kept in git.",
        {"ref": S}, ["ref"]),
]

# WHEN each tool is for, said in its own description (10-22). Models read a tool's
# schema at the moment they pick a tool; the instructions are far away in the system
# prompt. An agent on deltos wrote 0 facts in 3 days and missed plain news in its own
# chats (a version drift fixed and shipped). A schema is a prompt, so this is
# [tools] descriptions = "when" (or $DELTOS_TOOL_DESCRIPTIONS) until bench/writeornot.py
# scores it on two models; "plain" keeps what each tool does only.
WHEN = {
    "remember": "Use it the moment a value changes or you learn one, before you answer: a version shipped, "
                "a host or port moved, a model swapped, a status changed, a decision made. ",
    "name_thing": "Use it first when memory doesn't know the thing yet, then remember. ",
    "memory_recall": "Use it before saying something isn't known, and when the memory block doesn't cover "
                     "what is asked. ",
    "page_search": "Use it before saying something isn't known. ",
    "page_write": "Use it after finished work worth a write-up (a fix, a setup, a decision and why). ",
    "forget": "Use it when the user says to forget something, or for junk you made by mistake. ",
}


def tools_for(descriptions: str = "plain") -> list[dict]:
    """The tools an agent is given: TOOLS, with WHEN in front of their
    descriptions when descriptions == "when"."""
    if descriptions != "when":
        return TOOLS
    return [dict(t, description=WHEN.get(t["name"], "") + t["description"]) for t in TOOLS]


def _switch(env: str, value, default=False) -> bool:
    v = os.environ.get(env)
    if v not in (None, ""):
        return v not in ("0", "false", "off")
    return default if value is None else bool(value)


# How to read a memory block: the agent's instructions carry it, and a reader
# that gets only the block (a benchmark) gets it with the block.
READING = "The newest `since` wins; a `was:` line is\n  history, not the current value."
# A count from memory is a floor (10-15, Phase 3: 13 of the 21 held-out answers
# that were wrong and sure were counts that came up short). Opt-in until it's
# scored on a second model: DELTOS_READING_COUNT=1.
READING_COUNT = ("A count or total from memory can come up short (what was never noted isn't there):\n  "
                 "give it as what's on record, e.g. \"3 on record, maybe more\".")
if os.environ.get("DELTOS_READING_COUNT") == "1":
    READING += "\n  " + READING_COUNT
# 10-16: questions about all of something whose parts memory files under their own
# words (the block found one part: "sisters", not "brother"). On since 10-17 with
# receipts (recall.py RECEIPTS: scored on two models); DELTOS_READING_SEARCH=0 leaves it out.
READING_SEARCH = ("When the question is about all of something and the lines cover only part of it, search\n  "
                  "memory again with the words a note would use for the other parts (for pets: dog, cat)\n  "
                  "before answering.")
if os.environ.get("DELTOS_READING_SEARCH", "1") != "0":
    READING += "\n  " + READING_SEARCH
# 10-17, LoCoMo (DeepSeek): 63 of deltos's 97 sure-wrong answers were questions that name the
# wrong person, answered from the other person's note or chat line although the block showed
# whose it was ("[caroline] Made stained glass window"); Mem0, whose search gives each person's
# memories under their name, 49. The same slip as giving one machine's value for another.
# On since 10-18, scored on two models: the LoCoMo practice 500 on
# DeepSeek, trick questions 63 -> 47 sure-wrong (p=0.01); LongMemEval no worse on DeepSeek
# (306 -> 303 right) or GLM (290 -> 285 right, 21 -> 17 sure-wrong); the fresh LoCoMo 500,
# unread until the rule was settled: most right answers on both models. DELTOS_READING_WHOSE=0
# leaves it out.
READING_WHOSE = ("A line is about the thing whose note it is in, and a chat line is what its speaker said: check\n  "
                 "it is about who or what the question names. If memory has it only for someone or something\n  "
                 "else, say so instead of answering for them.")
if os.environ.get("DELTOS_READING_WHOSE", "1") != "0":
    READING += "\n  " + READING_WHOSE

INSTRUCTIONS = """# Memory — deltos
Your long-term memory: what is true now about the things in {whose} world, what it used to be, and the
documents beside it. Nothing is captured automatically: write it deliberately.

**Facts** — one profile per thing (machine, service, model, app, repo, person, setting...). Each entry is the
value that is true now, with the date it became true, and a "was:" trail of older values.
- Read the memory block you are given for this message first. {reading}
- `memory_recall` searches again (different words, another thing); `memory_show` lists everything on one thing.
- When a value changes or you learn one, `remember` it right then: thing, attribute, value, and the date it
  became true. Reuse the thing's existing attribute names: one fact, one entry, so the new value replaces the
  old one. Link things with the attributes `runs on` / `part of` and the other thing's name. A value that was
  wrong all along: `remember` the right one with `correction: true`.
- Something that should not be in memory at all (a test, junk, a thing made by mistake, or the user says
  forget it): `forget` it with a reason. It is undoable (`memory_restore`); a big or linked thing needs the
  user to have asked for it. Deleting for good is the user's call (`deltos purge <thing>`): never remove
  memory files yourself.
- `remember` only takes names memory knows. A new thing, or another name for a known one: `name_thing` first.

**Pages** — documents for anything longer than a fact: how-tos, decisions, write-ups.{folders}
- `page_search` = exact text across all pages; `page_read` = a whole page.
- `page_write` = a new note; `page_edit` = fix a page in place (never write a "CORRECTION" note beside the
  wrong one); `page_forget` = remove one (kept in .trash and git).{readonly}

**Past chats** — `sessions/<date>` pages: one digest per day of every chat (what was asked, files written,
how it ended), current through today. Work that happened in a chat ("the sprite we were making", "what did
we decide about X") is often only there: look before saying it isn't known. Files a session wrote are on disk.

**When memory has no clear answer, say so.** If nothing found names the thing asked about, say you don't
have it and what you searched. Never present the nearest match as the answer. When {who} corrects you,
record exactly what was said (and what you verified); don't attach it to a thing you guessed.

Tool results are the truth about writes: if one refuses or fails, say so and quote the reason. Live state
(what is running, versions) can drift: when it matters, check it."""


class Memory:
    def __init__(self, root: str | Path | None = None, config: Config | None = None, agent: str = "",
                 today: str = ""):
        """agent: who writes through this Memory (recorded as each fact's source).
        today: YYYY-MM-DD for a replayed world (benchmarks); default the real date."""
        self.config = config or Config.load(root)
        self.agent = agent
        self.today = today
        self.root = self.config.root
        (self.root / "notes").mkdir(parents=True, exist_ok=True)
        self.tools = tools_for(os.environ.get("DELTOS_TOOL_DESCRIPTIONS")
                               or str(self.config.tools.get("descriptions", "plain")))
        self.cues = _switch("DELTOS_CUES", self.config.recall.get("cues"))   # deltos/cues.py; off by default

    # -- the parts ------------------------------------------------------------

    def store(self) -> Store:
        return Store(self.root, self.today or datetime.date.today().isoformat())

    def recall(self) -> Recall:
        r = Recall.open(self.root, self.config.owner)
        r.today = self.today
        days = os.environ.get("DELTOS_DOUBT_DAYS")
        r.doubt_days = int(days if days not in (None, "") else self.config.recall.get("doubt_days", 0) or 0)
        return r

    def pages(self) -> Pages:
        c = self.config
        read = dict(c.pages_read)
        if (self.root / "sessions").is_dir():
            read["sessions"] = self.root / "sessions"
        return Pages(c.pages_write or self.root / "pages", read, {"facts": self.root / "notes"},
                     today=self.today or datetime.date.today().isoformat())

    # -- reading ----------------------------------------------------------------

    def context(self, message: str, pages: int = PAGES_PER_TURN, loose: int = LOOSE_PER_TURN,
                previous: str = "") -> str:
        """The memory block for a message: facts on the things it names, then the
        pages that match it best. No model call; ~50 ms. Only what memory is sure
        of: a message that names nothing ("thanks!", "write a haiku") gets no word
        matches, and a page only when two of its words meet in one line.
        previous: the message before this one in the same chat (a follow-up's
        subject is often only there); "" reads this message alone."""
        self.refresh()
        r = self.recall()
        named = bool(r.entities(message) or r.described(message))
        if not named and RECENT.search(message):
            return self.recent()
        # The owner's own profile is named by every "my" and "me": that alone doesn't
        # make a message name something, so it doesn't let a page in on one loose word.
        mine = r.profile_of.get(r.owner.lower()) if r.owner else None
        others = lambda groups: [g for g in groups if not mine or r.profile_of.get(g[0].lower()) != mine]
        things = others(r.entities(message)) or others(r.described(message))
        carry = bool(previous.strip()) and (not things or len(message.split()) <= CARRY_WORDS)
        ask = message
        if carry and not things and r.entities(previous):
            # ", " and not " ": after "which disk?" a word starts a sentence, where a
            # thing named by a plain word ("Ledger") doesn't count as named.
            ask = message + ", " + ", ".join(g[0] for g in r.entities(previous))
        facts, n = r.context(ask, loose=loose)
        parts = []
        if n:
            parts.append(facts if len(facts) <= FACT_CHARS else facts[:FACT_CHARS] + "\n... (more: memory_show)")
        sure = bool(things) or loose >= MAX_LINES   # a search asked for (memory_recall) shows everything
        extra = {w: CARRY_WEIGHT for w in query_terms(previous)} if carry else None
        hits = self.pages().top(message, pages, min_cover=1 if sure else 2, extra_terms=extra) if pages else []
        if hits:   # each page labelled with its age: "[ref · 2026-07-10 · 3 months old]"
            today = self.today or datetime.date.today().isoformat()
            olds = [age(d, today) for _, d, _ in hits]
            parts.append("Pages (page_read for the whole page):\n" + "\n".join(
                f"[{ref} · {d}{' · ' + old if old else ''}]\n" + "\n".join(f"  {ln}" for ln in lines)
                for (ref, d, lines), old in zip(hits, olds)))
        if self.cues and parts:   # the tool this turn may need, said where the agent is reading
            from .cues import cues
            lines = cues(r, message, "\n\n".join(parts))
            if lines:
                parts.append("\n".join(lines))
        return "\n\n".join(parts)

    def recent(self, days: int = RECENT_DAYS) -> str:
        """What has been happening: pages updated and things that changed over
        the last `days`, newest first. For "what did we work on recently?"."""
        now = datetime.date.fromisoformat(self.today) if self.today else datetime.date.today()
        since = (now - datetime.timedelta(days=days)).isoformat()
        parts = []
        pages = self.pages().recent(since, skip=("sessions/",))
        if pages:
            parts.append(f"Pages updated in the last {days} days (newest first; page_read for one):\n"
                         + "\n".join(f"[{d}] {ref} — {what}" for d, ref, what in pages))
        changed = self.recall().changed(since)
        if changed:
            parts.append("Things whose facts changed (memory_show for one):\n" + "\n".join(
                f"[{d}] {', '.join(things[:THINGS_PER_DAY])}" + (f" (+{len(things) - THINGS_PER_DAY})"
                                                                if len(things) > THINGS_PER_DAY else "")
                for d, things in changed))
        if (self.root / "sessions").is_dir():
            parts.append("Chats: one page per day, sessions/<YYYY-MM-DD> (page_read).")
        return "\n\n".join(parts)

    def refresh(self) -> None:
        """Today's chat digests, rebuilt when older than SESSIONS_EVERY (~10 ms)."""
        if not self.config.sessions:
            return
        from .sources import hermes
        today = datetime.date.today()
        f = self.root / "sessions" / f"{today}.md"
        if f.exists() and time.time() - f.stat().st_mtime <= SESSIONS_EVERY:
            return
        for kind, home in self.config.sessions.items():
            if kind == "hermes":
                hermes.write_day(self.root / "sessions", today, home)

    def instructions(self) -> str:
        c = self.config
        folders = (f" Your pages: {c.pages_write}." if c.pages_write else "")
        readonly = "".join(f"\n- `{k}/` pages are read-only: when one is stale, `remember` the right value and say "
                           f"which page is stale." for k in c.pages_read)
        return INSTRUCTIONS.format(whose=f"{c.owner}'s" if c.owner else "your user's", who=c.owner or "the user",
                                   folders=folders, readonly=readonly, reading=READING)

    # -- tools ------------------------------------------------------------------

    def call(self, tool: str, args: dict | None = None) -> str:
        """Run a tool. Raises Refused for a request to refuse (the message is
        for the agent); any other exception is a bug."""
        a = args or {}

        def need(k: str) -> str:
            v = str(a.get(k) or "").strip()
            if not v:
                raise Refused(f"Missing required parameter: {k}")
            return v

        try:
            if tool == "memory_recall":
                return self.context(need("query"), pages=5, loose=MAX_LINES) or \
                    "Nothing in memory matches. Try other words or names."
            if tool == "memory_show":
                if str(a.get("as_of") or "").strip():
                    return self.store().as_of(need("thing"), str(a["as_of"]).strip())
                return self.store().show(need("thing"), history=bool(a.get("history")))
            if tool == "remember":
                return self.store().remember(need("thing"), need("attribute"), need("value"),
                                             since=str(a.get("since") or ""), source=self.agent,
                                             correction=bool(a.get("correction")), another=bool(a.get("another")))
            if tool == "name_thing":
                names = a.get("other_names") or []
                names = [n for n in names.split(",") if n.strip()] if isinstance(names, str) else list(names)
                s, out = self.store(), []
                if a.get("merge"):
                    out += [s.merge(need("thing"), str(n)) for n in names]
                if a.get("remove_names"):
                    out.append(s.unname(need("thing"), [str(n) for n in a["remove_names"]]))
                if names or a.get("kind") or not out:
                    out.append(s.name(need("thing"), names, str(a.get("kind") or "")))
                return "; ".join(out)
            if tool == "forget":
                # A tool call is always an agent's, named or not: the agent limits apply.
                return self.store().forget(need("thing"), str(a.get("attribute") or ""), need("reason"),
                                           source=self.agent or "agent", user_asked=bool(a.get("user_asked")))
            if tool == "memory_restore":
                return self.store().restore(need("thing"), source=self.agent or "agent")
            p = self.pages()
            if tool == "page_search":
                return p.search(need("text"))
            if tool == "page_read":
                return p.read(need("ref"))
            if tool == "page_write":
                return f"Wrote {p.write(need('content'))}."
            if tool == "page_edit":
                return f"Edited {p.edit(need('ref'), need('new_text'), str(a.get('old_text') or ''))} in place."
            if tool == "page_forget":
                return f"Forgot {p.forget(need('ref'))} (moved to .trash; git keeps it)."
        except PageError as e:
            raise Refused(str(e)) from e
        raise Refused(f"Unknown tool: {tool}")
