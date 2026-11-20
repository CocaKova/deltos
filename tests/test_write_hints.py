"""What the remember receipt says about the entry just written: a new label beside one
that may hold the same fact, and a value that says what happened on a label for a state."""
import tempfile
import unittest
from pathlib import Path

from deltos.memory import Memory, near_labels


class WriteHints(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.m = Memory(Path(self.d.name), agent="Ava")
        self.m.call("name_thing", {"thing": "Box-7", "kind": "machine"})
        self.m.call("remember", {"thing": "Box-7", "attribute": "setting qsfp mtu", "value": "1500"})
        self.m.call("remember", {"thing": "Box-7", "attribute": "setting reasoning-parser", "value": "qwen3"})

    def test_near_labels(self):
        self.assertEqual(near_labels("Setting MTU", ["Setting qsfp mtu", "Host"]), ["Setting qsfp mtu"])
        self.assertEqual(near_labels("Setting tool-call-parser", ["Setting reasoning-parser"]), [])
        self.assertEqual(near_labels("Latest release", ["Release", "Version"]), ["Release"])

    def test_new_label_names_the_near_one(self):
        out = self.m.call("remember", {"thing": "Box-7", "attribute": "setting mtu", "value": "9000"})
        self.assertIn("already has Setting qsfp mtu (1500)", out)
        out = self.m.call("remember", {"thing": "Box-7", "attribute": "setting tool-call-parser", "value": "x"})
        self.assertNotIn("already has", out)
        out = self.m.call("remember", {"thing": "Box-7", "attribute": "setting qsfp mtu", "value": "9000"})
        self.assertNotIn("already has", out)   # the same label: a new value, nothing to say

    def test_event_on_a_state_label(self):
        out = self.m.call("remember", {"thing": "Box-7", "attribute": "status", "value": "restarted"})
        self.assertIn("says what happened", out)
        out = self.m.call("remember", {"thing": "Box-7", "attribute": "status", "value": "live"})
        self.assertNotIn("says what happened", out)
        out = self.m.call("remember", {"thing": "Box-7", "attribute": "owner", "value": "Ada"})
        self.assertNotIn("says what happened", out)


if __name__ == "__main__":
    unittest.main()


class Misses(unittest.TestCase):
    """A search or a name memory doesn't hold says what it does hold, nearest first."""
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        root = Path(self.d.name)
        (root / "deltos.toml").write_text('agent = "Ava"\n[pages]\nwrite = "%s"\n' % (root / "wiki"))
        self.m = Memory(root, agent="Ava")
        self.m.call("name_thing", {"thing": "Quarry Gateway", "kind": "service"})
        self.m.call("page_write", {"content": "Night shift\nAva restarted the Quarry gateway after the update."})

    def test_words_in_any_form(self):
        out = self.m.call("page_search", {"text": "restart quarry gateway"})
        self.assertIn("No page says 'restart quarry gateway' exactly", out)
        self.assertIn("restarted the Quarry gateway", out)
        self.assertNotIn("No page says", self.m.call("page_search", {"text": "Quarry gateway"}))

    def test_a_real_miss_says_so(self):
        out = self.m.call("page_search", {"text": "Halden backup"})
        self.assertIn("(no matches)", out)
        self.assertIn("no line has all of halden, backup", out)

    def test_nearest_names(self):
        self.assertIn("Nearest names in memory: Quarry Gateway", self.m.call("memory_show", {"thing": "Quary gateway"}))
        self.assertIn("No name in memory is close", self.m.call("memory_show", {"thing": "Zephyrine"}))
