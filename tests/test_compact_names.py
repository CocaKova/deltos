"""A name written with or without spaces, hyphens or dots names the same thing:
"the river of glass re-design" names riverofglass, "red kite" names redkite."""
import tempfile
import unittest
from pathlib import Path

from deltos.recall import Recall
from deltos.store import Store


class CompactNameTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        s = Store(Path(self.d.name), today="2026-05-01")
        s.name("riverofglass", ["riverofglass.example"], "website")
        s.remember("riverofglass", "host", "Halden")
        s.name("Night Owl", [], "app")
        s.remember("Night Owl", "port", "8642")
        s.name("Box", [], "machine")
        s.remember("Box", "status", "up")
        self.r = Recall(self.d.name)

    def names(self, q):
        return [g[0] for g in self.r.entities(q)]

    def test_spaced_and_joined(self):
        self.assertEqual(self.names("Continuing the river of glass re-design"), ["riverofglass"])
        self.assertEqual(self.names("is nightowl up?"), ["Night Owl"])
        self.assertEqual(self.names("restart night-owl"), ["Night Owl"])

    def test_short_or_word_names_stay_exact(self):
        self.assertEqual(self.names("put the b ox away"), [])


if __name__ == "__main__":
    unittest.main()
