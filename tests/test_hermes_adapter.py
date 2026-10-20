"""The Hermes adapter (adapters/hermes/deltos): the block is built in prefetch()
from the query Hermes cleaned, with the session's previous turn; each read is
logged without message text; a learn run is started when a session ends.
Hermes isn't installed here: its two imports are stubbed."""
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from deltos import Memory

REPO = Path(__file__).resolve().parents[1]


def _load_adapter():
    mp = types.ModuleType("agent.memory_provider")
    mp.MemoryProvider = type("MemoryProvider", (), {})
    reg = types.ModuleType("tools.registry")
    reg.tool_error = lambda msg: json.dumps({"error": msg})
    stubs = {"agent": types.ModuleType("agent"), "agent.memory_provider": mp,
             "tools": types.ModuleType("tools"), "tools.registry": reg}
    added = [k for k in stubs if k not in sys.modules]
    sys.modules.update({k: stubs[k] for k in added})
    try:
        with mock.patch.dict(os.environ, {"DELTOS_SRC": str(REPO)}):
            spec = importlib.util.spec_from_file_location("deltos_hermes_adapter",
                                                          REPO / "adapters" / "hermes" / "deltos" / "__init__.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
    finally:
        for k in added:
            sys.modules.pop(k, None)
    return mod


adapter = _load_adapter()
BLOCK = "Profile entries [box-7 · last edited 2026-10-01]:\n- **Host:** the Annex (since 2026-10-01)"


def fake_learn(on: bool) -> types.ModuleType:
    """A deltos.learn whose enabled() says `on` (the real one may not be merged yet)."""
    mod = types.ModuleType("deltos.learn")
    mod.__spec__ = importlib.machinery.ModuleSpec("deltos.learn", None)
    mod.enabled = lambda cfg: on
    return mod


class HermesAdapterTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.root = Path(self.d.name)
        (self.root / "deltos.toml").write_text('owner = "Ada"\nagent = "Ava"\n[pages]\nwrite = "%s"\n'
                                               % (self.root / "wiki"))
        m = Memory(self.root, agent="Ava")
        m.call("name_thing", {"thing": "Box-7", "kind": "machine"})
        m.call("remember", {"thing": "Box-7", "attribute": "host", "value": "the Annex"})
        m.call("page_write", {"content": "Backup job\nThe backup job for Box-7 runs nightly from the Annex."})
        for p in (mock.patch.dict(os.environ, {"DELTOS_ROOT": str(self.root), "DELTOS_AGENT": "Ava"}),
                  mock.patch.object(adapter, "_last_kick", None), mock.patch.object(adapter, "_child", None)):
            p.start()
            self.addCleanup(p.stop)
        # Never a real learn run from a test; other processes (rg for pages) run as usual.
        self.popen, real = mock.MagicMock(), subprocess.Popen
        popen = mock.patch.object(subprocess, "Popen", lambda cmd, *a, **kw: (
            self.popen(cmd, *a, **kw) if "deltos.learn" in cmd else real(cmd, *a, **kw)))
        popen.start()
        self.addCleanup(popen.stop)
        self.p = adapter.DeltosProvider()

    def reads(self) -> list[dict]:
        return [json.loads(ln) for f in sorted((self.root / "reads").glob("*.jsonl")) for ln in f.read_text().splitlines()]

    def test_prefetch_gives_the_block(self):
        out = self.p.prefetch("  where is Box-7?  ", session_id="s1")
        self.assertTrue(out.startswith("## Memory\n"))
        self.assertIn("box-7", out.lower())
        self.assertIn("the Annex", out)
        self.assertEqual(self.p.prefetch("   ", session_id="s1"), "")

    def test_nothing_named_and_no_previous_gives_nothing(self):
        # Each session here has no earlier turn, so a deltos that carries a short
        # follow-up on from the previous turn has nothing to carry.
        self.assertEqual(self.p.prefetch("thanks!", session_id="s0"), "")
        self.assertIn("the Annex", self.p.prefetch("where is Box-7?", session_id="s1"))
        self.assertEqual(self.p.prefetch("thanks!", session_id="s2"), "")   # s1's turn is not s2's

    def test_turn_start_and_queue_build_nothing(self):
        with mock.patch.object(Memory, "context", autospec=True, return_value=BLOCK) as spy:
            self.p.on_turn_start(1, "where is Box-7?")
            self.p.queue_prefetch("where is Box-7?", session_id="s1")
            spy.assert_not_called()
            self.p.prefetch("where is Box-7?", session_id="s1")
            self.p.prefetch("where is Box-7?", session_id="s1")   # asked again: the cached block
            self.assertEqual(spy.call_count, 1)
            self.p.on_turn_start(2, "where is Box-7?")
            self.p.prefetch("where is Box-7?", session_id="s1")   # a later turn: built again
            self.assertEqual(spy.call_count, 2)

    def _calls(self, takes_previous: bool, turns: list[tuple[str, str]], between=None) -> list:
        """(message, previous) per context() call; previous None when not passed."""
        calls = []

        def with_previous(self, message, pages=3, loose=3, previous=""):
            calls.append((message, previous))
            return BLOCK

        def message_only(self, message, pages=3, loose=3):
            calls.append((message, None))
            return BLOCK

        with mock.patch.object(Memory, "context", with_previous if takes_previous else message_only):
            p = adapter.DeltosProvider()
            for i, (sid, q) in enumerate(turns):
                if between and i in between:
                    between[i](p)
                p.prefetch(q, session_id=sid)
        return calls

    def test_previous_only_when_context_takes_it(self):
        turns = [("s1", "where is Box-7?"), ("s1", "and its backup?")]
        self.assertEqual(self._calls(True, turns), [("where is Box-7?", ""), ("and its backup?", "where is Box-7?")])
        self.assertEqual(self._calls(False, turns), [("where is Box-7?", None), ("and its backup?", None)])

    def test_sessions_keep_their_own_previous(self):
        calls = self._calls(True, [("s1", "where is Box-7?"), ("s2", "what is Lumen?"),
                                   ("s1", "its port?"), ("s2", "its owner?")])
        self.assertEqual([prev for _, prev in calls], ["", "", "where is Box-7?", "what is Lumen?"])
        long = "x " * 3000
        calls = self._calls(True, [("s1", long), ("s1", "and?")])
        self.assertEqual(calls[1][1], long.strip()[:adapter.PREV_CHARS])

    def test_a_long_pause_drops_the_previous(self):
        def later(p):   # the session's last turn, as if it were asked an hour ago
            p._last["s1"] = p._last["s1"][:3] + (p._last["s1"][3] - 3600,)
        calls = self._calls(True, [("s1", "where is Box-7?"), ("s1", "its port?"), ("s1", "and its backup?")],
                            between={1: later})
        self.assertEqual([prev for _, prev in calls], ["", "", "its port?"])

    def test_reset_switch_drops_the_old_previous(self):
        calls = self._calls(True, [("s1", "where is Box-7?"), ("s1", "its port?"),
                                   ("s2", "what is Lumen?"), ("s2", "its owner?")],
                            between={0: lambda p: p.initialize(session_id="s1"),
                                     1: lambda p: p.on_session_switch("s2", parent_session_id="s1", reset=True),
                                     3: lambda p: p.on_session_switch("s3", reset=False)})
        self.assertEqual([prev for _, prev in calls], ["", "", "", "what is Lumen?"])

    def test_context_error_gives_nothing(self):
        with mock.patch.object(Memory, "context", side_effect=RuntimeError("disk gone")), \
                self.assertLogs(adapter.logger, "WARNING"):
            self.assertEqual(self.p.prefetch("where is Box-7?", session_id="s1"), "")

    def test_read_log_has_stems_and_no_message_text(self):
        q = "where does the backup job for Box-7 run? my zebra password"
        out = self.p.prefetch(q, session_id="s1")
        self.assertIn("Pages (", out)
        (rec,) = self.reads()
        self.assertEqual(set(rec), {"at", "s", "q_chars", "ms", "block_chars", "things", "pages"})
        self.assertEqual((rec["s"], rec["q_chars"], rec["block_chars"]),
                         (hashlib.sha1(b"s1").hexdigest()[:8], len(q), len(out) - len("## Memory\n")))
        self.assertEqual(rec["things"], ["box-7"])
        self.assertTrue(rec["pages"] and all(f"[{ref} · " in out for ref in rec["pages"]))
        self.assertNotIn("zebra", json.dumps(rec))
        self.assertEqual(rec["at"][:7], next((self.root / "reads").glob("*.jsonl")).stem)

    def test_read_record_parses_profiles_and_pages_only(self):
        block = ("Profile entries [box-7 · whose memory this is · last edited 2026-10-01]:\n- **Host:** a\n\n"
                 "Profile entries [lumen · last edited 2026-09-01]:\n- **Port:** 1\n\n"
                 "Other notes (...), each tagged [note · date]:\n[daily-log · 2026-10-02] - box-7 rebooted\n\n"
                 "Pages (page_read for the whole page):\n[wiki/backup.md · 2026-10-03 · 2 weeks old]\n"
                 "  [not a label · x] indented page text\n[wiki/backup.md · 2026-10-03]\n[notes/lumen.md · 2026-09-01]")
        rec = adapter.read_record("s1", "q", 5, block)
        self.assertEqual(rec["things"], ["box-7", "lumen"])
        self.assertEqual(rec["pages"], ["wiki/backup.md", "notes/lumen.md"])

    def test_unwritable_root_does_not_raise(self):
        (self.root / "reads").write_text("a file where the folder would go")
        self.assertIn("the Annex", self.p.prefetch("where is Box-7?", session_id="s1"))

    def test_kick_needs_learn_and_learning_on(self):
        with mock.patch.dict(sys.modules, {"deltos.learn": fake_learn(True)}), \
                mock.patch.object(adapter.importlib.util, "find_spec", return_value=None):   # no deltos.learn
            self.p.on_session_end([])
        adapter._last_kick = None
        with mock.patch.dict(sys.modules, {"deltos.learn": fake_learn(False)}):
            self.p.on_session_end([])
        self.popen.assert_not_called()

    def test_kick_starts_learn_once_a_minute(self):
        with mock.patch.dict(sys.modules, {"deltos.learn": fake_learn(True)}):
            self.p.on_session_end([{"role": "user", "content": "hi"}])
            self.p.initialize(session_id="s2")   # within the minute: nothing more
            self.assertEqual(self.popen.call_count, 1)
            (cmd,), kw = self.popen.call_args
            self.assertEqual(cmd, [sys.executable, "-m", "deltos.learn", "--wait"])
            self.assertEqual((kw["cwd"], kw["env"]["DELTOS_ROOT"], kw["env"]["PYTHONPATH"].split(os.pathsep)[0]),
                             (adapter.SRC, str(self.root), adapter.SRC))
            self.assertEqual((kw["stdin"], kw["stderr"], kw["start_new_session"]),
                             (subprocess.DEVNULL, subprocess.STDOUT, True))
            self.assertTrue((self.root / "learn.log").exists())
            adapter._last_kick -= adapter.KICK_EVERY + 1   # a minute later
            self.popen.return_value.poll.return_value = None   # the last run still going: nothing more
            self.p.on_session_end([])
            self.assertEqual(self.popen.call_count, 1)
            self.popen.return_value.poll.return_value = 0
            self.p.on_session_end([])
            self.assertEqual(self.popen.call_count, 2)

    def test_kick_failure_is_soft(self):
        self.popen.side_effect = OSError("no exec")
        with mock.patch.dict(sys.modules, {"deltos.learn": fake_learn(True)}), \
                self.assertLogs(adapter.logger, "WARNING"):
            self.p.on_session_end([])


if __name__ == "__main__":
    unittest.main()
