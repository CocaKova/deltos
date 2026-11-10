"""Store.retitle: a thing takes another of its names as its own."""
import tempfile
import unittest
from pathlib import Path

from deltos.store import Store


class Retitle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        s = Store(self.root, today="2026-06-01")
        s.name("Tessix T2", ["Box-3", "T2"], "machine")
        s.remember("Tessix T2", "ip", "10.0.0.3", since="2026-01-01")
        s.name("Ledgerly", [], "service")

    def tearDown(self):
        self.tmp.cleanup()

    def test_retitle_keeps_facts_file_and_old_name(self):
        s = Store(self.root, today="2026-06-02")
        self.assertEqual(s.retitle("Tessix T2", "Box-3"), "Tessix T2 is now called Box-3 (also Tessix T2)")
        t = Store(self.root).find("Box-3")
        self.assertEqual((t.name, t.file, t.attrs["ip"].value), ("Box-3", "tessix-t2", "10.0.0.3"))
        self.assertEqual(t.aliases, ["Tessix T2", "T2"])
        self.assertIn("[Box-3](notes/tessix-t2.md) (Tessix T2, T2)", (self.root / "MEMORY.md").read_text())
        # the old name freed, then given to the product
        s = Store(self.root, today="2026-06-02")
        s.unname("Box-3", ["Tessix T2"])
        self.assertIn("created Tessix T2", s.name("Tessix T2", [], "product"))
        self.assertEqual(Store(self.root).find("Box-3").name, "Box-3")

    def test_refusals(self):
        s = Store(self.root, today="2026-06-02")
        self.assertIn("another thing's name", s.retitle("Tessix T2", "Ledgerly"))
        self.assertIn("nothing is called", s.retitle("Nope", "X-1"))
        self.assertIn("already its name", s.retitle("Tessix T2", "tessix t2"))
        self.assertTrue(s.retitle("Tessix T2", "it").startswith("not renamed"))

    def test_without_the_old_name(self):
        s = Store(self.root, today="2026-06-02")
        s.retitle("Tessix T2", "Box-3", keep_old=False)
        s = Store(self.root)
        self.assertIsNone(s.find("Tessix T2"))
        self.assertEqual(s.find("T2").name, "Box-3")


if __name__ == "__main__":
    unittest.main()
