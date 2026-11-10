"""Receipts that say none of their value (store.receipt_unsaid, Store.unsay, deltos receipts)."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from deltos import cli
from deltos.doctor import report, things_of, unsaid_receipts
from deltos.store import Store, receipt_unsaid


def world(root: Path) -> Store:
    s = Store(root, today="2026-06-01")
    s.name("Box-3", [], "machine")
    s.remember("Box-3", "host", "box-3", since="2026-01-02")
    s.receipt("Box-3", "host", "box-3", "The human check is done: agree 15 of 21 (10-16).", "2026-01-02")
    s.remember("Box-3", "port", "8443", since="2026-01-02")
    s.receipt("Box-3", "port", "8443", "Box-3 listens on 8443 now.", "2026-01-02")
    s.remember("Box-3", "status", "restarted", since="2026-01-02")
    s.receipt("Box-3", "status", "restarted", "We restart it every night.", "2026-01-02")
    return s


class Receipts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        world(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_which_say_nothing(self):
        t = Store(self.root).find("Box-3")
        self.assertTrue(receipt_unsaid(t.attrs["host"]))      # 'box-3' -> box; no word of it (only a number)
        self.assertFalse(receipt_unsaid(t.attrs["port"]))     # a number value: its number counts
        self.assertFalse(receipt_unsaid(t.attrs["status"]))   # restart ~ restarted (stems)
        self.assertEqual([a.name for _, a in unsaid_receipts(things_of(self.root))], ["Host"])
        self.assertIn("Receipts that say none of their value (deltos receipts --drop): 1", report(self.root, "2026-06-02"))

    def test_unsay_keeps_the_value_and_logs_the_words(self):
        s = Store(self.root, today="2026-06-02")
        self.assertTrue(s.unsay("Box-3", "host", "").startswith("not changed"))
        out = s.unsay("Box-3", "host", "wrong sentence")
        self.assertIn("receipt taken off", out)
        t = Store(self.root).find("Box-3")
        self.assertIsNone(t.attrs["host"].said)
        self.assertEqual(t.attrs["host"].value, "box-3")
        self.assertIsNotNone(t.attrs["port"].said)
        row = json.loads((self.root / "receipts-log.jsonl").read_text().splitlines()[0])
        self.assertEqual((row["thing"], row["attribute"], row["reason"]), ("Box-3", "Host", "wrong sentence"))
        self.assertIn("human check", row["said"])
        self.assertTrue(s.unsay("Box-3", "host", "again").startswith("not changed"))

    def test_cli_lists_then_drops(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--root", str(self.root), "receipts"])
        self.assertEqual(len(buf.getvalue().splitlines()), 1)
        with redirect_stdout(io.StringIO()):
            cli.main(["--root", str(self.root), "receipts", "--drop"])
        self.assertEqual(unsaid_receipts(things_of(self.root)), [])


if __name__ == "__main__":
    unittest.main()
