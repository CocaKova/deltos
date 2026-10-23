"""deltos as a Hermes memory provider: the per-turn memory block, deltos's
tools, and its instructions. All of it is deltos.Memory; this file only maps
it onto Hermes' MemoryProvider hooks.

Setup: link this folder to ~/.hermes/plugins/deltos and set memory.provider:
deltos in config.yaml. Memory is $DELTOS_ROOT or ~/deltos (deltos.toml
there). The deltos library is imported from $DELTOS_SRC, else the repo this
folder sits in, so an update to it lands on the next gateway restart. The agent
recorded as each fact's source is $DELTOS_AGENT, else deltos.toml's `agent`,
else "agent". An optional extra block for the instructions comes from a Python
file ($DELTOS_EXTRA_BLOCK, else [hermes] extra_block) defining build_block().
Fail-soft: a memory error gives an empty block, never a failed turn.

The block is built in prefetch(), which Hermes runs on its own thread with a
time bound (8 s) and hands the query with skill scaffolding stripped and secrets
redacted. Each read is logged to <root>/reads/<YYYY-MM>.jsonl (sizes, timing
and the things and pages shown; never message text). When a session ends or a
gateway starts, `python3 -m deltos.learn --wait` is started in the background
if learning is on (deltos.learn decides; it waits until the agent is idle).
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

logger = logging.getLogger(__name__)

SRC = os.path.expanduser(os.environ.get("DELTOS_SRC") or str(Path(__file__).resolve().parents[3]))

if SRC not in sys.path:
    sys.path.insert(0, SRC)

PREV_CHARS = 2000      # the previous turn carried into the next turn's block, cut to this
PREV_SESSIONS = 256    # sessions whose previous turn is kept (the least recent is dropped)
PREV_GAP = 30 * 60     # seconds: a turn after a longer pause is a new topic, so nothing is carried
KICK_EVERY = 60        # seconds: at most one learn start per gateway process this often
# A profile's header in the block (bench/gate.py PROFILE), and a page's label line
# under "Pages (": "[memory/foo.md · 2026-07-10]".
# Hermes opens a cron job's prompt with its own "[IMPORTANT: …]" blocks (delivery, [SILENT],
# recursion; skills it could not find). The same words come every run, so as a query they
# matched the same six unrelated things in every cron block (10-22: "The Day", Hyperframes,
# LLM bench …). A cron query drops them; the job's own prompt starts after them.
CRON_NOTICE = re.compile(r"\A\s*\[IMPORTANT: .*?\](?:\s*\n\n|\s*\Z)", re.S)
PROFILE = re.compile(r"^Profile entries \[([^ \]·]+)")
PAGE = re.compile(r"^\[([^\]·\n]+?) · ")

_kick_lock = threading.Lock()
_last_kick: float | None = None   # time.monotonic() of this process's last learn start
_child = None                     # the learn process it started (None, or still running, or done)


def _extra_block(path: str) -> str:
    """The text of an optional extra block: a Python file whose build_block()
    returns it (build_wiki_block() is accepted too). Missing or failing: none."""
    if not path:
        return ""
    try:
        spec = importlib.util.spec_from_file_location("deltos_extra_block", os.path.expanduser(path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        build = getattr(mod, "build_block", None) or getattr(mod, "build_wiki_block")
        return build() or ""
    except Exception as exc:
        logger.debug("extra block unavailable (%s)", exc)
        return ""


def read_record(session_id: str, query: str, ms: int, block: str) -> dict:
    """One line of the read log: who (a session hash), how big, how long, and
    which things and pages the block showed. No message text: only its length."""
    things, pages, in_pages = [], [], False
    for ln in block.splitlines():
        in_pages = in_pages or ln.startswith("Pages (")   # "Other notes" lines above are tagged [note · date] too
        hit, seen = (PAGE.match(ln), pages) if in_pages else (PROFILE.match(ln), things)
        if hit and hit.group(1) not in seen:
            seen.append(hit.group(1))
    return {"at": datetime.now().isoformat(timespec="seconds"),
            "s": hashlib.sha1(session_id.encode()).hexdigest()[:8],
            "q_chars": len(query), "ms": ms, "block_chars": len(block), "things": things, "pages": pages}


class DeltosProvider(MemoryProvider):
    def __init__(self):
        self._lock = threading.Lock()
        self._memory = None
        self._takes_previous = False
        self._sid = ""
        self._cron = False   # a scheduled job's agent (set in initialize)
        self._turn = 0
        # session id -> (its last query cut to PREV_CHARS, the turn and a hash of the
        # whole query, the block given for it); the least recently used session goes first.
        self._last: OrderedDict[str, tuple[str, str, str, float]] = OrderedDict()

    @property
    def name(self) -> str:
        return "deltos"

    def _m(self):
        if self._memory is None:
            from deltos import Memory
            m = Memory(agent=os.environ.get("DELTOS_AGENT", ""))
            m.agent = m.agent or m.config.agent or "agent"
            # A deltos that reads the previous turn takes it as context(previous=...);
            # an older one gets the message alone.
            self._takes_previous = "previous" in inspect.signature(Memory.context).parameters
            self._memory = m
        return self._memory

    def is_available(self) -> bool:
        return os.path.isdir(os.path.join(SRC, "deltos"))

    def initialize(self, session_id: str, **kwargs) -> None:
        self._sid = session_id
        # Hermes builds one provider per agent and says what runs it: a scheduled job
        # comes as platform / agent_context "cron" ([hermes] cron_block decides its block).
        self._cron = "cron" in (kwargs.get("agent_context"), kwargs.get("platform"))
        logger.info("deltos memory ready (root=%s, lib=%s)", self._m().root, SRC)
        self._kick("startup")   # sessions that ended while no gateway ran

    def shutdown(self) -> None:
        pass

    # -- per-turn memory ---------------------------------------------------------

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        # Nothing is built here: this message is raw (a cron skill turn carries the whole
        # skill, 60,987 chars: 29-33 s to read on 10-19, its cleaned 7,215 chars under 4 s)
        # and the call has no time bound. prefetch() gets the cleaned query on a bounded thread.
        self._turn += 1   # a question asked again in a later turn gets a fresh block

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        pass   # a block built after a turn was always thrown away: prefetch() builds its own

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """The block for this turn, from the query Hermes cleaned. The session's
        previous query goes along (a follow-up like "I mean on the other box" names
        little itself); it is set here, so it never waits on Hermes' write-behind worker."""
        q = (query or "").strip()
        if self._cron:
            while CRON_NOTICE.match(q):
                q = CRON_NOTICE.sub("", q, count=1).strip()
        if not q:
            return ""
        key = f"{self._turn}:{hashlib.sha1(q.encode()).hexdigest()}"
        with self._lock:
            prev, last_key, last_out, at = self._last.get(session_id, ("", "", "", 0.0))
        if key == last_key:   # asked again in the same turn: the same block
            return last_out
        t = time.monotonic()
        if t - at > PREV_GAP:   # the session went quiet: its last turn isn't this one's topic
            prev = ""
        try:
            m = self._m()
            # A cron job's prompt is the same every run, so its page matches are too
            # (10-20..22: the same 5-7k chars of unrelated pages every morning).
            mode = str(m.config.hermes.get("cron_block", "full")).lower() if self._cron else "full"
            kw = {"pages": 0} if mode == "facts" else {}
            if mode == "off":
                block = ""
            else:
                block = m.context(q, previous=prev, **kw) if self._takes_previous else m.context(q, **kw)
        except Exception as exc:   # fail-soft: no memory beats a failed turn
            logger.warning("deltos recall failed: %s", exc)
            block = ""
        ms = round((time.monotonic() - t) * 1000)
        out = f"## Memory\n{block}" if block else ""
        with self._lock:
            self._last[session_id] = (q[:PREV_CHARS], key, out, t)
            self._last.move_to_end(session_id)
            while len(self._last) > PREV_SESSIONS:
                self._last.popitem(last=False)
        self._log_read(session_id, q, ms, block)
        return out

    def _log_read(self, session_id: str, query: str, ms: int, block: str) -> None:
        """Which things and pages each turn was shown: the use signal for ranking
        and ageing them later. A failed write is dropped, never the turn."""
        try:
            rec = read_record(session_id, query, ms, block)
            d = Path(self._m().root) / "reads"
            d.mkdir(exist_ok=True)
            with open(d / f"{rec['at'][:7]}.jsonl", "a") as fh:
                fh.write(json.dumps(rec) + "\n")
        except Exception as exc:
            logger.debug("deltos read log not written: %s", exc)

    def system_prompt_block(self) -> str:
        m = self._m()
        extra = _extra_block(os.environ.get("DELTOS_EXTRA_BLOCK") or m.config.hermes.get("extra_block", ""))
        return (extra + "\n\n" if extra else "") + m.instructions()

    # -- sessions ------------------------------------------------------------------

    def on_session_switch(self, new_session_id: str, *, reset: bool = False, **kwargs) -> None:
        if reset:   # a new conversation: the old one's last turn is no follow-up to it
            with self._lock:
                self._last.pop(self._sid, None)
        self._sid = new_session_id

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        self._kick("session end")   # deltos.learn reads the chat from state.db, not from these

    def _kick(self, why: str) -> None:
        """Start `python3 -m deltos.learn --wait` in the background, detached from
        the gateway: at most once a minute, and not while the last one runs (it
        reads the chats once the agent is idle, so it covers this one too). A
        deltos without learn, or with learning off, starts nothing. A run started by
        another process meanwhile is kept to one by learn's own run lock."""
        global _last_kick, _child
        try:
            with _kick_lock:
                now = time.monotonic()
                if _last_kick is not None and now - _last_kick < KICK_EVERY:
                    return
                if _child is not None and _child.poll() is None:
                    return
                _last_kick = now
            if importlib.util.find_spec("deltos.learn") is None:
                return
            m = self._m()
            learn = importlib.import_module("deltos.learn")
            if not learn.enabled(m.config):
                return
            env = {**os.environ, "DELTOS_ROOT": str(m.root),
                   "PYTHONPATH": os.pathsep.join(p for p in (SRC, os.environ.get("PYTHONPATH", "")) if p)}
            with open(Path(m.root) / "learn.log", "a") as log:
                p = subprocess.Popen([sys.executable, "-m", "deltos.learn", "--wait"], cwd=SRC, env=env,
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                     start_new_session=True)
            with _kick_lock:
                _child = p
            threading.Thread(target=p.wait, name="deltos-learn-reaper", daemon=True).start()   # no zombie
            logger.info("deltos learn started (%s)", why)
        except Exception as exc:
            logger.warning("deltos learn not started (%s): %s", why, exc)

    # -- tools ---------------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return self._m().tools

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        from deltos import Refused
        try:
            return json.dumps({"result": self._m().call(tool_name, args or {})})
        except Refused as exc:
            return tool_error(str(exc))
        except Exception as exc:
            logger.exception("deltos tool %s failed", tool_name)
            return tool_error(f"{tool_name} failed: {exc}")

    def get_config_schema(self):
        return []   # env: DELTOS_ROOT / DELTOS_SRC / DELTOS_AGENT / DELTOS_EXTRA_BLOCK; the rest is deltos.toml


def register(ctx) -> None:
    ctx.register_memory_provider(DeltosProvider())
