import json
import tempfile
import unittest
from pathlib import Path

from deltos.memory import Memory, Refused
from deltos.recall import Recall
from deltos.store import BIG_THING, FORGET_PER_DAY, Store


class ForgetTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.s = Store(self.root, today="2026-06-01")
        self.s.name("Tamsin", ["TS-2"], "machine")
        self.s.remember("Tamsin", "location", "Bay 1", since="2026-01-01")
        self.s.remember("Tamsin", "location", "Bay 3", since="2026-03-01")
        self.s.remember("Tamsin", "owner", "Ops", since="2026-01-01")
        self.s.name("test word", [], "test")
        self.s.remember("test word", "current word", "pineapple")

    def tearDown(self):
        self.dir.cleanup()

    def test_forget_one_entry_and_restore_it_with_its_trail(self):
        out = self.s.forget("TS-2", "Location", "recorded on the wrong machine", source="Ava")
        self.assertIn("forgot Tamsin's Location (Bay 3, and 1 older value(s))", out)
        t = self.s.find("Tamsin")
        self.assertEqual(list(t.attrs), ["owner"])
        self.assertNotIn("Bay", (self.root / "notes/tamsin.md").read_text())
        self.assertEqual(self.s.restore("Tamsin", source="Ava"), "restored Tamsin's Location")
        a = self.s.find("Tamsin").attrs["location"]
        self.assertEqual((a.value, [p.value for p in a.trail]), ("Bay 3", ["Bay 1"]))
        self.assertIn("nothing forgotten", self.s.restore("Tamsin"))   # each forget is undone once

    def test_forget_a_whole_thing_frees_its_names_and_hides_it_from_recall(self):
        self.assertIn("pineapple", Recall.open(self.root).context("what is the test word?")[0])
        out = self.s.forget("test word", reason="a test, not a fact", source="Ava")
        self.assertTrue(out.startswith("forgot test word (1 entries)"), out)
        self.assertIsNone(self.s.find("test word"))
        self.assertFalse((self.root / "notes/test-word.md").exists())
        self.assertNotIn("test word", (self.root / "MEMORY.md").read_text())
        self.assertNotIn("pineapple", Recall.open(self.root).context("what is the test word?")[0])
        trash = next((self.root / ".trash").iterdir())
        self.assertTrue((trash / "test-word.md").exists())
        self.assertEqual(json.loads((trash / "forgotten.json").read_text())["reason"], "a test, not a fact")
        self.assertEqual(self.s.restore("test word"), "restored test word")
        self.assertEqual(self.s.find("test word").attrs["current word"].value, "pineapple")

    def test_a_set_aside_page_goes_with_its_thing_and_is_not_a_thing(self):
        self.s.aside("test word", ["- status: shown in a demo (2026-05-01)"])
        s = Store(self.root, today="2026-06-01")      # a store opened with the page on disk
        self.assertIsNone(s.find("test word extra (moved out of the profile)"))
        s.forget("test word", reason="junk")
        self.assertFalse((self.root / "notes/test-word-extra.md").exists())
        self.assertTrue(s.forget("Tamsin", reason="junk").startswith("forgot Tamsin"))   # 10-20: crashed here
        self.assertNotIn("extra", (self.root / "MEMORY.md").read_text())

    def test_restore_will_not_overwrite_a_new_thing_with_the_same_name(self):
        self.s.forget("test word", reason="junk")
        self.s.name("test word", [], "test")
        self.assertIn("not restored: test word is another thing now", self.s.restore("test word"))

    def test_an_agent_needs_user_asked_for_a_big_or_linked_thing(self):
        for i in range(BIG_THING - 2):
            self.s.remember("Tamsin", f"port {i}", str(8000 + i))
        out = self.s.forget("Tamsin", reason="cleanup", source="Ava")
        self.assertIn(f"not forgotten: Tamsin has {BIG_THING} entries", out)
        self.assertIsNotNone(self.s.find("Tamsin"))
        self.assertTrue(self.s.forget("Tamsin", reason="user said so", source="Ava", user_asked=True).startswith("forgot"))

        self.s.name("Ledger", [], "service")
        self.s.remember("Ledger", "status", "live")
        self.s.remember("test word", "runs on", "Ledger")
        self.assertIn("test word point to it", self.s.forget("Ledger", reason="cleanup", source="Ava"))
        self.assertTrue(self.s.forget("Ledger", reason="cleanup").startswith("forgot"))   # a person at the CLI

    def test_an_agent_has_a_daily_budget_and_must_give_a_reason(self):
        self.assertIn("say why", self.s.forget("test word", source="Ava"))
        for i in range(FORGET_PER_DAY):
            self.s.name(f"junk {i}", [], "test")
            self.assertTrue(self.s.forget(f"junk {i}", reason="junk", source="Ava").startswith("forgot"))
        out = self.s.forget("test word", reason="junk", source="Ava")
        self.assertIn(f"already forgotten {FORGET_PER_DAY} things today", out)
        self.assertTrue(self.s.forget("test word", reason="junk").startswith("forgot"))   # people have no budget
        self.s.today = "2026-06-02"
        self.s.name("junk next", [], "test")
        self.assertTrue(self.s.forget("junk next", reason="junk", source="Ava").startswith("forgot"))
        log = self.s.forgotten(50)
        self.assertEqual(log[0]["thing"], "junk next")
        self.assertEqual(len(log), FORGET_PER_DAY + 2)

    def test_purge_is_permanent_and_only_touches_that_things_trash(self):
        self.s.forget("test word", reason="junk")
        self.s.forget("TS-2", "owner", "wrong")
        self.assertIn("purged .trash/2026-06-01-test-word", self.s.purge("test word"))
        self.assertEqual(sorted(p.name for p in (self.root / ".trash").iterdir()), ["2026-06-01-tamsin--owner"])
        self.assertIn("was purged for good", self.s.restore("test word"))
        self.assertIn("no trash", self.s.purge("test word"))
        self.assertNotIn("purge", [t["name"] for t in Memory(self.root).tools])

    def test_the_agent_gets_the_tools_and_its_name_on_the_receipt(self):
        m = Memory(self.root, agent="Ava")
        names = [t["name"] for t in m.tools]
        self.assertIn("forget", names)
        self.assertIn("memory_restore", names)
        self.assertIn("`forget` it with a reason", m.instructions())
        self.assertTrue(m.call("forget", {"thing": "test word", "reason": "a test"}).startswith("forgot test word"))
        self.assertEqual(self.s.forgotten(1)[0]["source"], "Ava")
        with self.assertRaises(Refused):
            m.call("forget", {"thing": "Tamsin"})   # no reason
        self.assertEqual(m.call("memory_restore", {"thing": "test word"}), "restored test word")


if __name__ == "__main__":
    unittest.main()


class UnnamedAgentTest(unittest.TestCase):
    def test_a_tool_call_without_an_agent_name_still_has_the_agent_limits(self):
        import tempfile
        from pathlib import Path
        from deltos import Memory
        with tempfile.TemporaryDirectory() as d:
            m = Memory(Path(d))          # e.g. `deltos serve` with no --agent
            m.call("name_thing", {"thing": "Tamsin", "kind": "machine"})
            for k in ("host", "port", "owner", "status", "model"):
                m.call("remember", {"thing": "Tamsin", "attribute": k, "value": "x"})
            self.assertIn("not forgotten", m.call("forget", {"thing": "Tamsin", "reason": "cleanup"}))
