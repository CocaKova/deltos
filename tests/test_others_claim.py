"""Phase 5 split ingest (bench/others.py --claim): two processes share one
question list, and each question goes to exactly one of them."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bench.others as O


class ClaimTest(unittest.TestCase):
    def test_a_question_is_claimed_once(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(O, "ROOT", Path(d)):
            self.assertTrue(O.claim("graphiti", "q1"))
            self.assertFalse(O.claim("graphiti", "q1"))
            self.assertTrue(O.claim("graphiti", "q2"))
            self.assertTrue(O.claim("mem0", "q1"))


if __name__ == "__main__":
    unittest.main()
