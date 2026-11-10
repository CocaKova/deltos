"""Where facts go (11-05): a product's facts and one unit of it, decisions in the
instructions, the doctor's organization numbers, names a write receipt points out."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deltos.config import Config
from deltos.doctor import dead_links, plain_names, report, things_of, unit_and_product
from deltos.links import unknown_names
from deltos.memory import Memory
from deltos.recall import Recall
from deltos.store import Store, product_fact, unit_marks, unit_product_hint


def world(root: Path) -> None:
    s = Store(root, today="2026-06-01")
    s.name("Box-3", ["box-3"], "machine")
    s.remember("Box-3", "host", "box-3", since="2026-01-02")
    s.remember("Box-3", "model", "Tessix T2", since="2026-01-02")
    s.name("Ledgerly", [], "service")
    s.remember("Ledgerly", "host", "Box-3", since="2026-01-03")
    s.remember("Ledgerly", "price", "$9/month", since="2026-01-03")
    s.name("Nodix-7", [], "machine")
    s.remember("Nodix-7", "ip", "10.0.0.7", since="2026-01-04")
    s.name("Quorvane Clinic", [], "client")
    s.remember("Quorvane Clinic", "setting glutathione price", "$375", since="2026-01-05")


class UnitAndProduct(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        world(self.root)
        self.s = Store(self.root, today="2026-06-02")

    def tearDown(self):
        self.tmp.cleanup()

    def test_what_makes_a_unit(self):
        self.assertEqual(unit_marks(self.s.find("Box-3")), ["Host box-3"])      # its own host name
        self.assertEqual(unit_marks(self.s.find("Nodix-7")), ["Ip 10.0.0.7"])
        self.assertEqual(unit_marks(self.s.find("Ledgerly")), [])               # runs on another thing
        self.assertEqual(unit_marks(self.s.find("Quorvane Clinic")), [])

    def test_product_labels(self):
        for label in ("price", "Street price", "MSRP", "release date", "cost"):
            self.assertTrue(product_fact(label), label)
        for label in ("purchase price", "monthly fee", "price paid", "host", "plan cost"):
            self.assertFalse(product_fact(label), label)

    def test_hint_names_the_product(self):
        hint = unit_product_hint(self.s.find("Box-3"), "street price")
        self.assertIn("Box-3 is one unit (Host box-3)", hint)
        self.assertIn("(Tessix T2?)", hint)
        self.assertEqual(unit_product_hint(self.s.find("Box-3"), "purchase price"), "")
        self.assertEqual(unit_product_hint(self.s.find("Ledgerly"), "price"), "")
        self.assertEqual(unit_product_hint(self.s.find("Quorvane Clinic"), "setting glutathione price"), "")

    def test_the_write_stands_and_the_receipt_says_where_it_belongs(self):
        m = Memory(config=Config(root=self.root, owner="Sam"), agent="assistant", today="2026-06-02")
        out = m.call("remember", {"thing": "Box-3", "attribute": "street price", "value": "$2,999"})
        self.assertIn("note: Box-3 is one unit", out)
        self.assertEqual(Store(self.root, today="2026-06-02").find("Box-3").attrs["street price"].value, "$2,999")
        out = m.call("remember", {"thing": "Nodix-7", "attribute": "status", "value": "live"})
        self.assertNotIn("note:", out)
        out = m.call("remember", {"thing": "Box-3", "attribute": "status", "value": "reduced from 1154 to 263 lines"})
        self.assertTrue(out.startswith("not "))   # a refusal gets no hint

    def test_doctor_lists_the_mix(self):
        Store(self.root, today="2026-06-02").remember("Box-3", "price", "$2,999", since="2026-06-02")
        mixed = unit_and_product(things_of(self.root))
        self.assertEqual([(n, u) for n, u, _ in mixed], [("Box-3", "Host box-3")])
        self.assertIn("One unit holding its product's facts (name the product, move these there): 1",
                      report(self.root, today="2026-06-02"))


class Organization(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        world(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dead_store_pointers(self):
        notes = self.root / "notes"
        (notes / "box-3-extra.md").write_text("# Box-3 extra\n\n- old: x\n  - 3 older value(s) in [[box-3-history]]\n"
                                              "- see [[some-page]]\n")
        self.assertEqual(dead_links(self.root), [("box-3-extra", "box-3-history")])   # a page link may live elsewhere
        (notes / "box-3-history.md").write_text("# Box-3 history\n")
        self.assertEqual(dead_links(self.root), [])

    def test_plain_lowercase_names(self):
        s = Store(self.root, today="2026-06-02")
        s.name("Nodix-7", ["base", "test harness", "Nodix", "nx-7"])
        self.assertEqual(plain_names(things_of(self.root)), [("Nodix-7", "base"), ("Nodix-7", "test harness")])


class Decisions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_on_by_default_and_switchable(self):
        on = Memory(config=Config(root=self.root, owner="Sam")).instructions()
        self.assertIn("A decision is a fact", on)
        self.assertIn("when Sam settles a price", on)
        self.assertIn("A product and one unit of it are two things", on)
        self.assertNotIn("what did we decide about X", on)
        off = Memory(config=Config(root=self.root, owner="Sam", tools={"decisions": False})).instructions()
        self.assertNotIn("A decision is a fact", off)
        self.assertIn('"what did we decide about X"', off)
        with mock.patch.dict(os.environ, {"DELTOS_DECISIONS": "0"}):
            self.assertNotIn("A decision is a fact", Memory(config=Config(root=self.root, owner="Sam")).instructions())
        self.assertNotIn("{", on.replace("{}", ""))   # every placeholder filled


class ReceiptNames(unittest.TestCase):
    def test_a_plain_word_in_front_is_not_part_of_the_name(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            world(root)
            r = Recall.open(root, "Sam")
            text = ("Then Zorvex asks for a code. The Bitvault entry holds it. Click Manage DNS in the Airo UI. "
                    "Re-seed the vault, then Follow-up.")
            self.assertEqual(unknown_names(r, text), ["Zorvex", "Bitvault", "Airo UI"])
            self.assertEqual(unknown_names(r, "Then Box-3 restarted."), [])   # a known name after a plain word
            self.assertEqual(unknown_names(r, "Kasimir's laptop"), ["Kasimir"])


if __name__ == "__main__":
    unittest.main()
