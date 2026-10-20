"""One fact under two labels: the model's decision folds the pair; the cache
replays it without the model."""
import tempfile
import unittest
from pathlib import Path

from deltos import slots
from deltos.store import Store


class SlotsTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        s = Store(Path(self.d.name), today="2023-10-27")
        s.name("Sam", [], "person")
        s.remember("Sam", "bike count", "3", since="2023-02-22")
        s.remember("Sam", "total bikes owned", "4", since="2023-10-10")
        s.remember("Sam", "weekday wake-up time", "7:15 am", since="2023-05-02")
        s.remember("Sam", "saturday wake-up time", "7:30 am", since="2023-05-27")
        s.remember("Sam", "setting theme", "dark", since="2023-05-01")
        s.remember("Sam", "setting theme color", "blue", since="2023-06-01")
        self.calls = []

    def judge(self, messages, **kw):
        self.calls.append(messages[0]["content"])
        lines = [ln for ln in messages[0]["content"].splitlines() if ln[:1].isdigit()]
        return {"content": "\n".join(f"{ln.split('.')[0]} {'same' if 'bike' in ln.lower() else 'different'}"
                                     for ln in lines)}, {}

    def test_judged_pair_folds_and_replays(self):
        cache = {}
        st = slots.apply(Store(Path(self.d.name)), cache, judge=self.judge)
        self.assertEqual(st["folded"], 1)
        self.assertNotIn("setting", "".join(self.calls).lower())   # config keys are never asked about
        t = Store(Path(self.d.name)).find("Sam")
        self.assertEqual(t.attrs["total bikes owned"].value, "4")
        self.assertEqual(t.attrs["total bikes owned"].trail[0].value, "3")
        self.assertNotIn("bike count", t.attrs)
        self.assertIn("weekday wake-up time", t.attrs)
        self.assertEqual(sum(cache.values()), 1)

    def test_cache_alone_needs_no_model(self):
        cache = {}
        slots.apply(Store(Path(self.d.name)), cache, judge=self.judge)
        self.setUp()   # the same store again, from scratch
        st = slots.apply(Store(Path(self.d.name)), cache)
        self.assertEqual((st["asked"], st["folded"]), (0, 1))

    def test_plans_and_other_peoples_facts_are_not_candidates(self):
        s = Store(Path(self.d.name), today="2023-10-27")
        s.remember("Sam", "trip destination", "Paris", since="2023-05-01")
        s.remember("Sam", "planned trip destination", "Iceland", since="2023-06-01")
        s.remember("Sam", "location", "Austin", since="2023-05-01")
        s.remember("Sam", "brother's location", "college dorm", since="2023-06-01")
        names = {frozenset((o.name.lower(), n.name.lower())) for o, n in slots.candidates(Store(Path(self.d.name)).find("Sam"))}
        self.assertNotIn(frozenset(("trip destination", "planned trip destination")), names)
        self.assertNotIn(frozenset(("location", "brother's location")), names)
        self.assertIn(frozenset(("bike count", "total bikes owned")), names)
