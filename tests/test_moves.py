"""When a thing moves (its host or location changes), what runs on it or is part
of it may have moved too: their drifting entries not seen since are due first."""
import tempfile
import unittest
from pathlib import Path

from deltos.checks import due, render_due
from deltos.store import Store


class MoveTest(unittest.TestCase):
    def test_dependents_of_a_move_are_due(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d), today="2026-05-01")
            s.name("Calder", [], "service")
            s.remember("Calder", "host", "Halden", since="2026-04-01")
            s.name("Gristle", [], "service")
            s.remember("Gristle", "runs on", "Calder", since="2026-04-20")
            s.remember("Gristle", "port", "8642", since="2026-04-20")
            s.name("Quill", [], "app")
            s.remember("Quill", "owner", "Calder", since="2026-04-20")   # not where it is
            s.remember("Quill", "port", "9000", since="2026-04-20")
            s.today = "2026-05-02"
            s.remember("Calder", "host", "Brisk", since="2026-05-02")
            rows = due(Path(d), today="2026-05-03")
            self.assertEqual({(r["thing"], r["attribute"]) for r in rows},
                             {("Gristle", "Runs on"), ("Gristle", "Port")})
            self.assertIn("Calder it depends on changed host on 2026-05-02", render_due(rows))
            s.today = "2026-05-03"
            s.remember("Gristle", "port", "8642", source="Ava", check="ss -ltn on Brisk")
            self.assertEqual([r["attribute"] for r in due(Path(d), today="2026-05-03")], ["Runs on"])

    def test_recall_says_the_move_on_the_entry(self):
        from deltos.recall import Recall
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d), today="2026-05-01")
            s.name("Calder", [], "service")
            s.remember("Calder", "host", "Halden", since="2026-04-01")
            s.name("Gristle", [], "service")
            s.remember("Gristle", "runs on", "Calder", since="2026-04-20")
            s.remember("Gristle", "port", "8642", since="2026-04-20")
            s.remember("Gristle", "owner", "Ada", since="2026-04-20")
            s.today = "2026-05-02"
            s.remember("Calder", "host", "Brisk", since="2026-05-02")
            r = Recall(Path(d))
            r.links_on, r.today = True, "2026-05-03"
            text, _ = r.context("what port does Gristle use?")
            self.assertIn("8642 (since 2026-04-20; Calder it depends on changed host 2026-05-02)", text)
            self.assertNotIn("Ada (since 2026-04-20; Calder", text)    # who owns it didn't move
            self.assertIn("**Runs on:** Calder (since 2026-04-20)", text)   # names the moved thing: still right
            r.links_on = False   # links off: no graph, no line
            self.assertNotIn("depends on", r.context("what port does Gristle use?")[0])
            s.today = "2026-05-03"   # looked since the move: nothing to say
            s.remember("Gristle", "port", "8642", source="Ava", check="ss -ltn on Brisk")
            r = Recall(Path(d))
            r.links_on, r.today = True, "2026-05-03"
            self.assertNotIn("Calder it depends on", r.context("what port does Gristle use?")[0])


if __name__ == "__main__":
    unittest.main()
