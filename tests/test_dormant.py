"""A thing no longer in use (its Status starts a clause with stopped, retired,
parked, ...) is found when a question names it, not through its entries' words,
and is not due for a look."""
import tempfile
import unittest
from pathlib import Path

from deltos.checks import due
from deltos.recall import Recall
from deltos.store import Store, dormant


class DormantTest(unittest.TestCase):
    def test_which_status_is_dormant(self):
        for v in ("stopped fallback", "historical brain; stopped fallback", "retired", "PARKED", "DELETED"):
            self.assertTrue(dormant(v), v)
        for v in ("kept, DeepSeek deleted", "down", "live", "renders frozen archive", "",
                  "removed personalities.nova, frontier"):
            self.assertFalse(dormant(v), v)

    def test_not_found_by_its_words_nor_due(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d), today="2026-05-01")
            s.name("Old model", [], "model")
            s.remember("Old model", "status", "stopped fallback", since="2026-01-01")
            s.remember("Old model", "restart verified", "after the patch", since="2026-01-01")
            s.remember("Old model", "port", "8000", since="2026-01-01")
            s.name("Gateway", [], "service")
            s.remember("Gateway", "port", "8642", since="2026-01-01")
            r = Recall(d)
            facts, _ = r.retrieve("was the restart verified after the patch?")
            self.assertFalse(any("after the patch" in x[2] for x in facts), facts)
            facts, _ = r.retrieve("was the Old model restart verified?")
            self.assertTrue(any("after the patch" in x[2] for x in facts), facts)
            rows = due(Path(d), today="2026-05-01")
            self.assertEqual([r["thing"] for r in rows], ["Gateway"])


if __name__ == "__main__":
    unittest.main()
