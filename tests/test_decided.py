"""A question about what was decided gets the whole of a small profile (recall.DECIDED)."""
import tempfile
import unittest
from pathlib import Path

from deltos.recall import Recall
from deltos.store import Store


class Decided(unittest.TestCase):
    def test_decided_shows_the_whole_small_profile(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            s = Store(root, today="2026-06-01")
            s.name("Quorvane intake app", [], "project")
            for lab, v in [("price", "$4,800 flat"), ("timeline", "three to five weeks"),
                           ("out of scope", "patient login"), ("scope doc", "v4")]:
                s.remember("Quorvane intake app", lab, v, since="2026-06-01")
            r = Recall.open(root, "Sam")
            ctx, _ = r.context("what did we decide for the Quorvane intake app scope?")
            self.assertIn("$4,800 flat", ctx)
            self.assertIn("three to five weeks", ctx)
            ctx, _ = Recall.open(root, "Sam").context("what is in the Quorvane intake app scope doc?")
            self.assertNotIn("$4,800 flat", ctx)   # any other question: the entries it names


if __name__ == "__main__":
    unittest.main()
