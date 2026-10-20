import tempfile
import unittest
from pathlib import Path

from deltos.recall import Recall
from deltos.store import Store


class RecallTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        s = Store(Path(self.dir.name), today="2026-06-01")
        s.name("Alex", ["alex k"], "person")
        s.name("dotfiles", ["dotfiles#123"], "repo")
        s.remember("dotfiles", "status", "pushed", since="2026-05-01")
        s.remember("Alex", "location", "Denver")
        s.name("Hermes", [], "agent")
        s.remember("Hermes", "port", "8080")
        s.name("config.yaml", [], "file")
        s.remember("config.yaml", "setting compression.threshold_tokens", "140000")
        self.r = Recall(Path(self.dir.name))

    def tearDown(self):
        self.dir.cleanup()

    def test_owner_is_not_the_subject(self):
        self.assertEqual([g[0] for g in self.r.entities("Has Alex's dotfiles PR been pushed?")], ["dotfiles"])
        self.assertEqual([g[0] for g in self.r.entities("Where is Alex's home?")], ["Alex"])
        self.assertEqual([g[0] for g in self.r.entities("Did Alex push dotfiles?")], ["Alex", "dotfiles"])

    def test_entry_on_another_thing(self):
        ctx = Recall(Path(self.dir.name)).context("What compression threshold_tokens is Hermes set to?")[0]
        self.assertIn("compression.threshold_tokens:** 140000", ctx)
        self.assertNotIn("140000", Recall(Path(self.dir.name)).context("What port is Hermes on?")[0])

    def test_entry_whose_value_names_another_asked_thing(self):
        # "GitHub" is a thing, so it is not a question word; the Notely entry whose
        # value names it is the answer. A setting that mentions it is not.
        s = Store(Path(self.dir.name), today="2026-06-01")
        s.name("Notely", [], "app")
        s.name("GitHub", ["GH"], "service")
        for i in range(20):   # a big profile: only chosen entries are shown
            s.remember("Notely", f"feature {i}", f"thing {i}")
        s.remember("Notely", "distribution", "GitHub releases", since="2026-05-10")
        s.remember("Notely", "setting mirror", "GitHub mirror off")
        ctx = Recall(Path(self.dir.name)).context("Does Notely ship on GitHub?")[0]
        self.assertIn("Distribution:** GitHub releases", ctx)
        self.assertNotIn("GitHub mirror off", ctx)

    def test_one_count_under_two_labels(self):
        # The importer wrote the new count under a new label: the older one is
        # shown as its past value. A setting, or an entry the question names
        # more closely than the newer one, stays current.
        s = Store(Path(self.dir.name), today="2026-06-01")
        s.name("Pat", [], "person")
        s.remember("Pat", "bike count", "3", since="2026-02-22")
        s.remember("Pat", "total bikes owned", "4", since="2026-05-10")
        s.remember("Pat", "weekday wake-up time", "7:15 am", since="2026-02-02")
        s.remember("Pat", "saturday wake-up time", "7:30 am", since="2026-05-27")
        r = Recall(Path(self.dir.name))
        ctx = r.context("How many bikes does Pat own?")[0]
        self.assertIn("**Total bikes owned:** 4", ctx)
        self.assertIn('was: 3 (as "bike count", 2026-02-22 to 2026-05-10)', ctx)
        self.assertNotIn("**Bike count:**", ctx)
        ctx = r.context("When does Pat wake up on weekdays?")[0]
        self.assertIn("**Weekday wake-up time:** 7:15 am", ctx)

    def test_a_plan_or_another_measure_is_another_fact(self):
        s = Store(Path(self.dir.name), today="2026-06-01")
        s.name("Pat", [], "person")
        s.remember("Pat", "recent trip duration", "3 days", since="2026-02-22")
        s.remember("Pat", "planned trip duration", "10 days", since="2026-05-10")
        s.remember("Pat", "model kit", "2 Spitfires", since="2026-02-22")
        s.remember("Pat", "model kit scale", "1 to 48", since="2026-05-10")
        r = Recall(Path(self.dir.name))
        self.assertIn("**Recent trip duration:** 3 days", r.context("How long was Pat's trip?")[0])
        self.assertIn("**Model kit:** 2 Spitfires", r.context("Which model kit does Pat have?")[0])

    def test_short_name_when_nothing_named_in_full(self):
        s = Store(Path(self.dir.name), today="2026-06-01")
        s.name("vendor courier", ["shipping vendor"], "vendor")
        s.remember("vendor courier", "vendor", "Pallas Vault")
        r = Recall(Path(self.dir.name))
        self.assertEqual([g[0] for g in r.entities("Who was our courier before Pallas Vault?")], ["vendor courier"])
        self.assertEqual([g[0] for g in r.entities("Is the shipping vendor late?")], ["vendor courier"])


class OpenTest(unittest.TestCase):
    def test_reloads_only_when_files_change(self):
        import tempfile
        from pathlib import Path
        from deltos.store import Store
        d = tempfile.TemporaryDirectory()
        s = Store(Path(d.name), today="2026-06-01")
        s.remember("Tamsin", "host", "Bay 3", create=True)
        a = Recall.open(d.name)
        self.assertIs(Recall.open(d.name), a)
        s.remember("Tamsin", "host", "the Annex", since="2026-06-01")
        b = Recall.open(d.name)
        self.assertIsNot(b, a)
        self.assertIn("the Annex", b.context("Where is Tamsin?")[0])


class DescribedTest(unittest.TestCase):
    def test_one_word_description_finds_the_only_fit(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d), today="2026-10-08")
            s.name("Beth", [], "person")
            s.remember("Beth", "relationship", "girlfriend")
            s.name("Theo", [], "person")
            s.remember("Theo", "relationship", "friend since first grade")
            s.name("Rosa", [], "person")
            s.remember("Rosa", "relationship", "mom")
            s.name("Natalie", [], "person")
            s.remember("Natalie", "status", "estranged from mom")
            r = Recall(Path(d))
            self.assertEqual([g[0] for g in r.described("Who is my girlfriend?")], ["Beth"])
            self.assertEqual([g[0] for g in r.described("Who is my mom?")], ["Rosa"])   # exact beats a mention
            self.assertEqual(r.described("Who is my person?"), [])   # fits both: no guess


class OwnerTest(unittest.TestCase):
    def test_my_names_the_owner_only_when_an_entry_fits(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d), today="2026-10-08")
            s.name("Sam", ["CocaSam"], "person")
            s.remember("Sam", "personal best 5K time", "27:12", since="2023-05-23")
            s.remember("Sam", "personal best 5K time", "25:50", since="2023-05-30")
            s.name("Agnes", [], "model")
            s.remember("Agnes", "repo", "CocaSam/agnes")
            r = Recall(Path(d), owner="Sam")
            self.assertEqual([g[0] for g in r.entities("What was my personal best 5K time?")], ["Sam"])
            self.assertEqual(r.entities("I'm tired"), [])                 # nothing of the owner's fits
            self.assertEqual(r.entities("What is the personal best 5K time?"), [])   # not first person
            self.assertEqual(Recall(Path(d)).entities("What was my personal best 5K time?"), [])   # no owner set
            text, _ = r.context("What was my personal best 5K time?")
            self.assertIn("25:50", text)
            self.assertIn("whose memory this is", text)
            self.assertNotIn("agnes", text)   # the owner's names are not search words

    def test_plural_names_its_entry(self):
        from deltos.recall import _same_word
        for a, b in [("bikes", "bike"), ("notes", "note"), ("boxes", "box"), ("matches", "match"),
                     ("batteries", "battery"), ("released", "release")]:
            self.assertTrue(_same_word(a, b), (a, b))
        self.assertFalse(_same_word("run", "runtime"))


class TallyTest(unittest.TestCase):
    def test_how_many_lists_the_things_of_that_kind(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        s = Store(Path(d.name), today="2023-05-30")
        s.name("Sam", [], "person")
        s.remember("Sam", "snake plant acquisition date", "last month", since="2023-04-25")
        for n in ("Peace Lily", "Succulent"):
            s.name(n, [], "plant")
            s.remember(n, "acquisition date", "2023-05-06", since="2023-05-06")
        s.name("Austin Film Festival", [], "")
        s.remember("Austin Film Festival", "attended", "yes", since="2023-05-01")
        r = Recall(Path(d.name), owner="Sam")
        ctx = r.context("How many plants did I acquire in the last month?")[0]
        for t in ("[peace-lily", "[succulent", "Snake plant"):
            self.assertIn(t, ctx)
        self.assertNotIn("Film Festival", ctx)
        self.assertIn("[austin-film-festival", r.context("How many film festivals did I attend?")[0])
        self.assertNotIn("[peace-lily", r.context("How many festivals did I attend?")[0])

    def test_a_count_over_time_shows_the_logged_events(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        s = Store(Path(d.name), today="2023-05-30")
        s.name("Sam", [], "person")
        for day, t in [("2023-05-16", "baked a chocolate cake"), ("2023-05-23", "baked sourdough bread"),
                       ("2023-05-27", "went to the farmers market"), ("2023-05-28", "baking whole wheat focaccia")]:
            s.note(t, day)
        ctx = Recall(Path(d.name), owner="Sam").context("How many times did I bake something in the past two weeks?")[0]
        for t in ("chocolate cake", "sourdough bread", "whole wheat focaccia"):
            self.assertIn(t, ctx)
        self.assertNotIn("farmers market", ctx)


class KindWordsTest(unittest.TestCase):
    """DELTOS_KINDS=1: an item's kind words ("[kitchen appliance]") name its entry."""
    def setUp(self):
        from unittest import mock
        for mod in ("deltos.store", "deltos.recall"):
            p = mock.patch(f"{mod}.ITEM_KINDS", True)
            p.start()
            self.addCleanup(p.stop)
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        s = self.s = Store(Path(self.d.name), today="2023-05-30")
        s.name("Sam", [], "person")
        for i, (lab, val) in enumerate([("job", "nurse"), ("city", "Tucson"), ("car", "2018 Honda Civic"),
                                        ("favorite cuisine", "Thai"), ("gym", "Crunch"), ("phone", "Pixel 8"),
                                        ("book club day", "Tuesday"), ("commute", "30 minutes"), ("pet", "Milo (cat)"),
                                        ("running goal", "half marathon"), ("bank", "Chase"), ("hobby", "pottery")]):
            s.remember("Sam", lab, val, since=f"2023-05-{i + 1:02d}")
        s.remember("Sam", "gift received", "stand mixer from Dana [kitchen appliance]", since="2023-05-20")

    def test_kind_words_count_as_label_words(self):
        from unittest import mock
        self.s.remember("Sam", "gift received", "pasta maker from Lee [kitchen appliance]", since="2023-05-28",
                        another=True)
        r = Recall(Path(self.d.name), owner="Sam")
        words = next(f[1] for f in r.facts if "Gift received" in f[2])
        self.assertIn("kitchen", words)
        self.assertIn("stand mixer", r.context("How many kitchen items did I get this month?")[0])
        self.assertNotIn("stand mixer", r.context("Where do I work out?")[0])
        with mock.patch("deltos.recall.ITEM_KINDS", False), mock.patch("deltos.store.ITEM_KINDS", False):
            words = next(f[1] for f in Recall(Path(self.d.name), owner="Sam").facts if "Gift received" in f[2])
            self.assertNotIn("kitchen", words)

    def test_other_kind_words_are_the_same_value(self):
        out = self.s.remember("Sam", "gift received", "Stand mixer from Dana [appliance]", since="2023-05-25")
        self.assertNotIn("was:", (Path(self.d.name) / "notes" / "sam.md").read_text(), out)

    def test_a_value_is_measured_without_its_kind(self):
        from deltos.store import value_problem
        self.assertEqual(value_problem("a very long value with exactly ten words in it [kitchen appliance]"), "")
        self.assertTrue(value_problem("[kitchen appliance]"))

    def test_off_kind_words_are_just_value_text(self):
        from unittest import mock
        with mock.patch("deltos.store.ITEM_KINDS", False), mock.patch("deltos.recall.ITEM_KINDS", False):
            from deltos.store import bare, kind_words
            self.assertEqual(kind_words("stand mixer [kitchen appliance]"), [])
            self.assertEqual(bare("[mcp-aseprite]"), "[mcp-aseprite]")
