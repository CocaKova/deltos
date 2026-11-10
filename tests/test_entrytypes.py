"""Entry types (deltos/entrytypes.py): what kind of statement an entry is, read from
its label and value; recall shows a thing's rules with it whatever the question, and
its decisions when the question asks what was decided."""
import tempfile
import unittest
from pathlib import Path

from deltos.entrytypes import entry_type
from deltos.recall import Recall
from deltos.store import Store


class TypeTest(unittest.TestCase):
    def test_types(self):
        for label, value, kind, want in (
                ("Patient data rule", "never touch real patient data", "client", "rule"),
                ("Setting rule", "never force-push public main", "app", "rule"),
                ("Reply style", "always sign it Sam", "client", "rule"),
                ("Price", "$5,000 flat", "project", "decision"),
                ("Timeline", "four to six weeks", "project", "decision"),
                ("Price", "$4,000", "machine", "identity"),
                ("Status", "shipped", "component", "event"),
                ("Launch day", "2026-10-13", "client", "event"),
                ("Host", "Halden", "service", "state"),
                ("Setting kv cache", "fp8", "model", "state"),
                ("Status", "live", "service", "state"),
                ("Owner", "Rowan", "service", "identity"),
                ("Relationship", "Ivo's dad", "person", "identity"),
                ("Kind", "machine", "", "identity"),
                ("Status", "stopped", "service", "state")):
            self.assertEqual(entry_type(label, value, kind), want, (label, value, kind))


class RecallTypeTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        s = Store(Path(self.d.name), today="2026-05-01")
        s.name("Brightside", [], "client")
        for i in range(14):   # a big profile: entries are picked by the question's words
            s.remember("Brightside", f"room {i} colour", ["teal", "amber", "slate"][i % 3])
        s.remember("Brightside", "timeline", "four to six weeks")
        s.remember("Brightside", "price", "$5,000 flat")
        s.remember("Brightside", "patient data rule", "never touch real patient data")
        self.r = Recall(self.d.name)

    def lines(self, q):
        return "\n".join(x[2] for x in self.r.retrieve(q)[0])

    def test_rules_come_with_their_thing(self):
        out = self.lines("draft the next update email for Brightside")
        self.assertIn("never touch real patient data", out)
        self.assertNotIn("$5,000", out)

    def test_decisions_when_asked_what_was_decided(self):
        out = self.lines("what did we decide for Brightside?")
        self.assertIn("$5,000 flat", out)
        self.assertIn("four to six weeks", out)


if __name__ == "__main__":
    unittest.main()
