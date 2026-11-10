"""Links (deltos/links.py): an entry whose value names another thing links the two."""
import tempfile
import unittest
from pathlib import Path

from deltos.config import Config
from deltos.memory import Memory
from deltos.recall import Recall
from deltos.store import Store, name_problem, placeholder


def world(root: Path) -> None:
    s = Store(root, today="2026-06-01")
    s.name("Ava", [], "person")
    s.name("Calder", ["CD-8"], "machine")
    s.name("Brisk", [], "machine")
    s.name("Gristle", ["the ingest worker"], "service")
    s.name("Fernqueue", [], "service")
    s.name("Sable", [], "app")
    s.name("Mia", [], "person")
    s.name("Teo", [], "person")
    s.name("Kestrel-9", [], "model")
    s.remember("Calder", "owner", "Ava", since="2026-01-02")
    s.remember("Brisk", "owner", "Ava", since="2026-01-02")
    s.remember("Gristle", "host", "Brisk", since="2026-01-05")
    s.remember("Gristle", "host", "Calder", since="2026-03-01")
    s.remember("Gristle", "port", "8443", since="2026-01-05")
    s.remember("Fernqueue", "host", "Calder", since="2026-01-06")
    s.remember("Sable", "runs on", "Gristle", since="2026-02-01")
    s.remember("Sable", "model", "Kestrel-9", since="2026-02-01")
    s.remember("Kestrel-9", "status", "retired", since="2026-05-01")
    s.remember("Mia", "relationship", "Ava's sister", since="2026-01-03")
    s.remember("Mia", "location", "Leeds", since="2026-01-03")
    s.remember("Teo", "relationship", "Mia's partner", since="2026-01-04")
    s.remember("Calder", "kind", "node")   # a category, never a link
    for n in ("Arvo", "Bexley", "Corwin", "Delphine"):   # Brisk is a hub
        s.name(n, [], "service")
        s.remember(n, "host", "Brisk", since="2026-02-10")


class GraphTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        world(self.root)
        self.r = Recall(self.root)
        self.r.links_on = True
        self.g = self.r.graph

    def tearDown(self):
        self.dir.cleanup()

    def p(self, name):
        return self.r.profile_of[name.lower()]

    def test_value_naming_a_thing_is_a_link_both_ways(self):
        out = {(lk.label, self.r.title(lk.dst), lk.exact) for lk in self.g.out[self.p("Gristle")]}
        self.assertIn(("Host", "Calder", True), out)
        self.assertNotIn(("Host", "Brisk", True), out)   # an old value is history, not a link
        inc = {self.r.title(lk.src) for lk in self.g.inc[self.p("Calder")]}
        self.assertEqual(inc, {"Gristle", "Fernqueue"})

    def test_mention_and_kind(self):
        ls = self.g.out[self.p("Teo")]
        self.assertEqual([(self.r.title(lk.dst), lk.exact) for lk in ls], [("Mia", False)])
        self.assertFalse(any(lk.key == "kind" for ls in self.g.out.values() for lk in ls))

    def test_path_and_dependents(self):
        chain = self.g.path(self.p("Teo"), self.p("Ava"))
        self.assertEqual([self.r.title(lk.src) for lk in chain], ["Teo", "Mia"])
        deps = {self.r.title(p): len(c) for p, c in self.g.dependents(self.p("Calder"))}
        self.assertEqual(deps, {"Gristle": 1, "Fernqueue": 1, "Sable": 2})

    def test_hub_is_never_a_step(self):
        # Everything Ava owns points at her: two of her machines aren't related through her.
        self.assertEqual(self.g.path(self.p("Calder"), self.p("Brisk"), avoid={self.p("Ava")}), [])


class RecallLinksTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        world(self.root)
        self.m = Memory(config=Config(root=self.root, owner="Ava", recall={"links": True}), today="2026-06-01")

    def tearDown(self):
        self.dir.cleanup()

    def test_what_points_at_a_thing(self):
        ctx = self.m.context("What runs on Calder?")   # a few: the entries themselves
        self.assertIn("[Gristle] - **Host:** Calder", ctx)
        self.assertIn("[Fernqueue] - **Host:** Calder", ctx)
        self.assertIn("Sable (runs on Gristle)", ctx)   # two links away
        ctx = self.m.context("What runs on Brisk?")    # many: one line of names
        self.assertIn("- **Host is Brisk on 4 things:** ", ctx)

    def test_role_word_through_the_owners_links(self):
        ctx = self.m.context("Where does my sister live?")
        self.assertIn("Leeds", ctx)
        ctx = self.m.context("Which machines do I own?")
        self.assertIn("calder", ctx.lower())
        self.assertIn("brisk", ctx.lower())

    def test_path_to_the_owner(self):
        ctx = self.m.context("Who is Teo to me?")
        self.assertIn("Teo → Mia → Ava", ctx)

    def test_linked_thing_changed_after(self):
        ctx = self.m.context("What model does Sable use?")
        self.assertIn("linked: Kestrel-9 status retired", ctx)

    def test_asked_entry_is_not_crowded(self):
        ctx = self.m.context("What port does Gristle listen on?")
        self.assertIn("8443", ctx)
        self.assertNotIn("Profile entries [calder", ctx)

    def test_off_is_unchanged(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"DELTOS_LINKS": ""}):
            self._off()

    def _off(self):
        off = Memory(config=Config(root=self.root, owner="Ava"), today="2026-06-01")
        self.assertNotIn("Links (", off.context("What runs on Calder?"))
        self.assertIn("Link things with the attributes `runs on` / `part of`", off.instructions())
        self.assertIn("links the two", self.m.instructions())

    def test_receipts(self):
        out = self.m.call("remember", {"thing": "Teo", "attribute": "employer", "value": "Calder"})
        self.assertIn("links Teo to Calder", out)
        out = self.m.call("remember", {"thing": "Teo", "attribute": "relationship", "value": "Jesse's son"})
        self.assertIn("Jesse is not in memory", out)
        self.m.config.pages_write = self.root / "pages"
        out = self.m.call("page_write", {"content": "Family\n\n- **Kristina** is Teo's friend; Mia met Orsolya."})
        self.assertIn("Not in memory yet: Kristina, Orsolya", out)
        # month and day names are dates, not names; someone's still counts
        out = self.m.call("page_write", {"content": "Sync\n\nSince the Sep 7 install the Monday sync moved "
                                                    "to October; June's notes went to Orsolya on Thu."})
        self.assertIn("Not in memory yet: June, Orsolya.", out)


class PlaceholderTest(unittest.TestCase):
    def test_rules(self):
        self.assertTrue(placeholder("Ivo's dad"))
        self.assertTrue(name_problem("Ivo's dad"))               # not for any thing
        self.assertEqual(name_problem("Ivo's dad", person=True), "")
        self.assertTrue(name_problem("Ava's plan B notes", person=True))   # a phrase, even for a person

    def test_placeholder_links_then_takes_the_name(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d), today="2026-06-01")
            s.name("Ivo", [], "person")
            self.assertTrue(s.name("Ivo's dad", [], "person").startswith("created Ivo's dad"))
            self.assertIn("**Relationship:** Ivo's dad", s.show("Ivo's dad"))
            out = s.name("Rowan", ["Ivo's dad"], "person")
            self.assertTrue(out.startswith("renamed Ivo's dad to Rowan"), out)
            self.assertTrue((Path(d) / "notes" / "rowan.md").exists())
            self.assertFalse((Path(d) / "notes" / "ivo-s-dad.md").exists())
            self.assertEqual(s.find("Ivo's dad").name, "Rowan")
            r = Recall(Path(d))
            r.links_on = True
            self.assertEqual([r.title(lk.dst) for lk in r.graph.out[r.profile_of["rowan"]]], ["Ivo"])


if __name__ == "__main__":
    unittest.main()
