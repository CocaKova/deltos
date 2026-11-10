"""A long message (a cron prompt, a pasted log) is read by its opening: the block
arrives inside the harness's wait instead of not at all."""
import tempfile
import unittest
from pathlib import Path

from deltos import Memory
from deltos.memory import QUERY_CHARS
from deltos.store import Store


class LongMessageTest(unittest.TestCase):
    def test_the_opening_is_read(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d), today="2026-05-01")
            s.name("Halden", [], "machine")
            s.remember("Halden", "host", "rack 2")
            s.name("Quarry", [], "machine")
            s.remember("Quarry", "host", "rack 9")
            m = Memory(d)
            pad = " filler" * (QUERY_CHARS // 7)
            block = m.context("Check Halden today." + pad + " Also Quarry.")
            self.assertIn("rack 2", block)
            self.assertNotIn("rack 9", block)


if __name__ == "__main__":
    unittest.main()
