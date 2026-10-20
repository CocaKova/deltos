import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from deltos import Memory, Refused
from deltos.mcp import serve


class SurfaceTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        root = Path(self.d.name)
        (root / "deltos.toml").write_text('owner = "Ada"\n[pages]\nwrite = "%s"\n' % (root / "wiki"))
        self.m = Memory(root, agent="Ava")
        self.m.call("name_thing", {"thing": "Tamsin", "other_names": ["TS-2"], "kind": "machine"})

    def test_turn_block_is_only_what_memory_is_sure_of(self):
        self.m.call("remember", {"thing": "Tamsin", "attribute": "host", "value": "the Annex"})
        self.m.call("page_write", {"content": "Rain notes\nA haiku about rain on the Annex roof."})
        self.m.call("page_write", {"content": "Thanks log\nthanks for the GPU, thanks again."})
        self.assertEqual(self.m.context("thanks!"), "")                     # one loose word: nothing
        self.assertIn("haiku about rain", self.m.context("write a haiku about rain"))   # two words meet in a line
        self.assertIn("the Annex", self.m.context("where is Tamsin?"))      # a named thing: its facts
        self.assertIn("Thanks log", self.m.call("memory_recall", {"query": "thanks"}))  # a search shows more

    def test_recent_activity(self):
        import datetime
        self.m.call("remember", {"thing": "Tamsin", "attribute": "host", "value": "the Annex"})
        self.m.call("page_write", {"content": "Decision\nWe keep vm-6 for builds."})
        block = self.m.context("What did we work on recently?")
        self.assertIn("decision", block.lower())           # the page, with its date
        self.assertIn(datetime.date.today().isoformat(), block)
        self.assertIn("Tamsin", block)                      # the thing that changed
        self.assertNotIn("Things whose", self.m.context("where is Tamsin?"))   # a named thing: its facts

    def test_tools_and_context(self):
        self.assertIn("since", self.m.call("remember", {"thing": "TS-2", "attribute": "host", "value": "the Annex"}))
        self.assertIn("source: Ava", self.m.call("memory_show", {"thing": "Tamsin"}))
        self.assertIn("the Annex", self.m.context("Where is Tamsin now?"))
        self.assertIn("Ada's world", self.m.instructions())
        self.assertIn("not recorded", self.m.call("remember", {"thing": "Nobody", "attribute": "host", "value": "x"}))
        with self.assertRaises(Refused):
            self.m.call("page_edit", {"ref": "missing", "new_text": "x"})
        self.assertEqual(self.m.call("page_search", {"text": "anything"}), "(no matches)")

    def test_remember_another_keeps_both_current(self):
        self.m.call("name_thing", {"thing": "Sam", "kind": "person"})
        self.m.call("remember", {"thing": "Sam", "attribute": "client", "value": "Acme"})
        r = self.m.call("remember", {"thing": "Sam", "attribute": "client", "value": "Brightside", "another": True})
        self.assertIn("another one beside Acme", r)
        a = self.m.store().find("Sam").attrs["client"]
        self.assertEqual((a.value, [x.value for x in a.also], a.trail), ("Brightside", ["Acme"], []))
        self.m.call("remember", {"thing": "Sam", "attribute": "city", "value": "Chicago"})
        self.m.call("remember", {"thing": "Sam", "attribute": "city", "value": "Austin"})   # no another: replaces
        a = self.m.store().find("Sam").attrs["city"]
        self.assertEqual((a.value, a.also, [p.value for p in a.trail]), ("Austin", [], ["Chicago"]))

    def test_mcp_handshake_list_call(self):
        reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "remember", "arguments": {"thing": "Tamsin", "attribute": "port", "value": "22"}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "page_read", "arguments": {"ref": "nope"}}},
                {"jsonrpc": "2.0", "id": 5, "method": "nope"}]
        out = io.StringIO()
        serve(self.m, io.StringIO("\n".join(json.dumps(r) for r in reqs) + "\n"), out)
        res = {d["id"]: d for d in map(json.loads, out.getvalue().splitlines())}
        self.assertEqual(sorted(res), [1, 2, 3, 4, 5])          # no reply to the notification
        self.assertIn("deltos", res[1]["result"]["instructions"])
        self.assertEqual(len(res[2]["result"]["tools"]), len(self.m.tools))
        self.assertFalse(res[3]["result"]["isError"])
        self.assertTrue(res[4]["result"]["isError"])
        self.assertEqual(res[5]["error"]["code"], -32601)

    def test_claude_code_hook(self):
        from deltos.cli import main
        self.m.call("remember", {"thing": "Tamsin", "attribute": "host", "value": "Bay 3"})
        stdin, stdout = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = io.StringIO(json.dumps({"prompt": "where is TS-2?"})), io.StringIO()
        try:
            main(["--root", self.d.name, "hook", "claude-code"])
            out = json.loads(sys.stdout.getvalue())
        finally:
            sys.stdin, sys.stdout = stdin, stdout
        self.assertIn("Bay 3", out["hookSpecificOutput"]["additionalContext"])


if __name__ == "__main__":
    unittest.main()


class DoctorTest(unittest.TestCase):
    def test_big_profile_keeps_its_core(self):
        import tempfile
        from pathlib import Path
        from deltos.doctor import CORE, tidy
        from deltos.store import Store
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d), today="2026-06-01")
            s.name("Notely", [], "app")
            s.remember("Notely", "version", "2.18.0")
            for i in range(CORE + 10):
                s.remember("Notely", f"setting key{chr(97 + i // 26)}{chr(97 + i % 26)}", "on")
            s.today = "2026-10-08"
            s.remember("Notely", "setting fresh", "on")
            tidy(Path(d), apply=True)
            t = Store(Path(d)).find("Notely")
            self.assertEqual(len(t.attrs), CORE)
            self.assertIn("version", t.attrs)            # not a setting: stays
            self.assertIn("setting fresh", t.attrs)      # touched recently: stays
            self.assertIn("keyaa", (Path(d) / "notes/notely-extra.md").read_text())   # oldest left, kept
