import tempfile
import unittest
from pathlib import Path

from deltos.store import Store


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.s = Store(Path(self.dir.name), today="2026-06-01")
        self.s.name("Tamsin", ["TS-2", "the big GPU box"], "machine")

    def tearDown(self):
        self.dir.cleanup()

    def attr(self, key="location"):
        return self.s.find("Tamsin").attrs[key]

    def test_change_moves_old_value_to_trail(self):
        self.s.remember("TS-2", "location", "Bay 3", since="2026-01-10")
        self.s.remember("big GPU box", "Location", "the Annex", since="2026-02-01")
        a = self.attr()
        self.assertEqual((a.value, a.since), ("the Annex", "2026-02-01"))
        self.assertEqual((a.trail[0].value, a.trail[0].start, a.trail[0].end), ("Bay 3", "2026-01-10", "2026-02-01"))

    def test_repeat_is_a_confirmation(self):
        self.s.remember("Tamsin", "location", "Bay 3", since="2026-01-10")
        self.s.today = "2026-03-01"
        self.assertIn("confirmed", self.s.remember("Tamsin", "location", "bay 3"))
        a = self.attr()
        self.assertEqual((a.value, a.confirmed, a.trail), ("Bay 3", "2026-03-01", []))
        self.assertIn("confirmed 2026-03-01", (Path(self.dir.name) / "notes/tamsin.md").read_text())

    def test_backdated_value_goes_into_trail(self):
        self.s.remember("Tamsin", "location", "Bay 1", since="2026-01-01")
        self.s.remember("Tamsin", "location", "Bay 3", since="2026-03-01")
        self.s.remember("Tamsin", "location", "Dock B", since="2026-02-01")   # news arrives late
        a = self.attr()
        self.assertEqual(a.value, "Bay 3")
        self.assertEqual([(p.value, p.start, p.end) for p in a.trail],
                         [("Dock B", "2026-02-01", "2026-03-01"), ("Bay 1", "2026-01-01", "2026-02-01")])

    def test_correction_marks_never_true(self):
        self.s.remember("Tamsin", "serial number", "SN-1", since="2026-01-05")
        self.s.remember("Tamsin", "serial number", "SN-2", correction=True)
        a = self.attr("serial number")
        self.assertEqual((a.value, a.since), ("SN-2", "2026-01-05"))
        self.assertTrue(a.trail[0].wrong)
        self.assertIn("never true", (Path(self.dir.name) / "notes/tamsin.md").read_text())

    def test_old_values_go_cold_and_come_back(self):
        for i, d in enumerate(["2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01", "2026-05-01"]):
            self.s.remember("Tamsin", "location", f"Spot {i}", since=d)
        notes = Path(self.dir.name) / "notes"
        self.assertIn("2 older value(s) in [[tamsin-history]]", (notes / "tamsin.md").read_text())
        self.assertIn("Spot 0", (notes / "tamsin-history.md").read_text())
        a = self.attr()   # parse round trip joins warm and cold tiers again
        self.assertEqual([p.value for p in a.trail], ["Spot 3", "Spot 2", "Spot 1", "Spot 0"])

    def test_retire(self):
        self.s.remember("Tamsin", "status", "serving traffic", since="2026-01-01")
        self.s.remember("Tamsin", "status", "decommissioned", since="2026-04-01")
        text = (Path(self.dir.name) / "notes/tamsin.md").read_text()
        self.assertIn("retired / decommissioned since 2026-04-01", text)
        self.assertEqual(self.attr("status").trail[0].value, "serving traffic")

    def test_names(self):
        self.s.name("TS-2", ["Tammy"])
        self.assertEqual(self.s.find("tammy").name, "Tamsin")
        self.s.name("Orrel", ["TS-2"])   # a name can't belong to two things
        self.assertEqual(self.s.find("TS-2").name, "Tamsin")
        self.assertIsNotNone(self.s.find("Orrel"))

    def test_find_takes_only_a_plain_file_name_as_a_stem(self):
        # A model's sentence as a thing name (10-19: a repair answer's reasoning) made os.stat raise.
        self.assertIsNone(self.s.find("The reason given was " + "too long for a value " * 20))
        (Path(self.dir.name) / "MEMORY.md").write_text("# not a thing\n")
        self.assertIsNone(self.s.find("../MEMORY"))

    def test_hand_edits_survive(self):
        self.s.remember("Tamsin", "location", "Bay 3", since="2026-01-10")
        p = Path(self.dir.name) / "notes/tamsin.md"
        p.write_text(p.read_text().replace("Tamsin (also called", "Our oldest box.\nTamsin (also called"))
        self.s.remember("Tamsin", "status", "idle", since="2026-02-01")
        self.assertIn("Our oldest box.", p.read_text())
        self.assertEqual(self.attr().value, "Bay 3")

    def test_identity_guard(self):
        self.s.name("courier", ["the shipping vendor"], "vendor")
        r = self.s.name("shipping", [], "vendor")
        self.assertIn("Possibly the same as: courier", r)

    def test_unknown_name_is_refused_not_created(self):
        self.s.name("Brisk", ["BK-5"], "machine")
        r = self.s.remember("brisk-01", "location", "Bay 2")
        self.assertIn("not recorded", r)
        self.assertIn("Brisk (BK-5)", r)
        self.assertIsNone(self.s.find("brisk-01"))
        r = self.s.remember("the edge box", "location", "Bay 2")   # no clue in the name: still refused
        self.assertIn("not recorded", r)
        self.assertIsNone(self.s.find("the edge box"))
        self.s.name("Brisk", ["the edge box"])
        self.assertIn("Bay 2", self.s.remember("the edge box", "location", "Bay 2"))
        self.assertIn("created", self.s.remember("Orrel", "status", "up", create=True))

    def test_names_that_are_values_are_refused(self):
        r = self.s.name("Tamsin", ["v1.9.1", ":8080", "1 TB", "~/x/y", "192.0.2.5", "Tam"])
        self.assertIn("added names Tam", r)
        self.assertEqual(self.s.find("Tamsin").aliases, ["TS-2", "the big GPU box", "Tam"])

    def test_numbered_siblings_are_two_things(self):
        # "Node 2" beside "Node 1", "TS-3" beside "TS-2": a series, not one thing's names.
        r = self.s.name("Tamsin", ["TS-3"])
        self.assertIn("TS-3 (differs from 'TS-2' only by its number", r)
        self.s.name("Build Node", ["Node 1"], "machine")
        r = self.s.name("Build Node", ["Build Node 2", "Node #1", "node-a3f9"])
        self.assertIn("added names Node #1, node-a3f9", r)
        self.assertIn("Build Node 2 (differs from 'Node 1'", r)
        self.assertNotIn("TS-3", self.s.find("Tamsin").aliases)
        self.assertIn("not created", self.s.remember("Notely 1.9", "status", "shipped", create=True))
        self.assertIsNone(self.s.find("Notely 1.9"))


    def test_kinds_labels_and_commits_are_not_names(self):
        self.s.name("Helsing", ["kind: company"])
        self.s.name("SambaNova", ["company", "brain@4288d32", "SambaNova Systems"])
        self.assertEqual(self.s.find("SambaNova").aliases, ["SambaNova Systems"])
        self.assertEqual(self.s.find("Helsing").aliases, [])
        self.assertIsNone(self.s.find("company"))
        self.assertIn("not created", self.s.name("notely: entity"))
        r = self.s.name("Notely", ["Notely 2.17.2 BACKUP FIX", "vc94", "Qwen 3.8 27B", "GPT-5.6 Sol"])
        self.assertEqual(self.s.find("Notely").aliases, ["Qwen 3.8 27B", "GPT-5.6 Sol"])

    def test_setting_and_separator_variants_are_one_attribute(self):
        self.s.remember("Tamsin", "setting build host", "Node 1", since="2026-01-01")
        self.s.remember("Tamsin", "Build_host", "buildbox", since="2026-03-01")
        t = self.s.find("Tamsin")
        self.assertEqual(list(t.attrs), ["setting build host"])
        self.assertEqual((t.attrs["setting build host"].value, t.attrs["setting build host"].trail[0].value),
                         ("buildbox", "Node 1"))

    def test_junk_is_refused(self):
        self.assertIn("not recorded", self.s.remember("Tamsin", "status",
                      "Does NOT supersede the 09-29 cooling-failure note in project_build_server or anything else"))
        self.assertIn("not created", self.s.remember("context_length", "size", "262144", create=True))
        self.assertIn("not created", self.s.remember("48dfa3d58d77", "status", "merged", create=True))
        self.s.name("Akamai", ["-"], "vendor")   # a placeholder must not become a shared name
        self.s.name("Netlify", ["-"], "vendor")
        self.assertIsNone(self.s.find("-"))
        self.assertEqual(self.s.find("Netlify").name, "Netlify")
        self.s.remember("Hollis &amp; Grey", "kind", "vendor", create=True)
        self.assertIsNotNone(self.s.find("Hollis & Grey"))

    # -- regressions found in the 10-07 review --

    def test_never_true_values_survive_going_cold(self):
        self.s.remember("Tamsin", "serial", "SN-1", since="2026-01-01")
        self.s.remember("Tamsin", "serial", "SN-2", correction=True)
        for i, d in enumerate(["2026-02-01", "2026-02-10", "2026-02-20"]):
            self.s.remember("Tamsin", "serial", f"SN-{i + 3}", since=d)
        trail = self.attr("serial").trail
        self.assertEqual([(p.value, p.wrong) for p in trail],
                         [("SN-4", False), ("SN-3", False), ("SN-2", False), ("SN-1", True)])

    def test_same_slug_never_overwrites_another_thing(self):
        self.s.remember("Hollis & Grey", "kind", "vendor", create=True)
        self.s.remember("Hollis Grey", "kind", "bakery", create=True)
        self.assertEqual(self.s.find("Hollis & Grey").attrs["kind"].value, "vendor")
        self.assertEqual(self.s.find("Hollis Grey").attrs["kind"].value, "bakery")
        self.s.remember("Log 2026-06-01", "status", "up", create=True)   # would be the daily log's file
        self.s.note("a log line")
        self.assertEqual(Store(Path(self.dir.name)).find("Log 2026-06-01").attrs["status"].value, "up")

    def test_odd_sources_and_hand_written_lines_are_kept(self):
        self.s.remember("Tamsin", "location", "Bay 3", since="2026-01-05", source="standup (Arun); chat")
        p = Path(self.dir.name) / "notes/tamsin.md"
        p.write_text(p.read_text() + "  - ask Arun before moving it\n- **Owner:** Arun\n\n## Notes\n\n- **Fan:** loud\n")
        self.s.remember("Tamsin", "status", "idle", since="2026-02-01")
        text = p.read_text()
        self.assertEqual(self.attr().value, "Bay 3")
        self.assertIn("source: standup [Arun], chat", text)
        self.assertIn("  - ask Arun before moving it", text)
        self.assertIn("## Notes\n\n- **Fan:** loud", text)
        self.assertNotIn("fan", self.s.find("Tamsin").attrs)
        self.assertIn("was Arun", self.s.remember("Tamsin", "owner", "Mira", since="2026-03-01"))
        self.assertEqual([p.value for p in self.attr("owner").trail], ["Arun"])
        self.assertIn("- **Owner:** Mira (since 2026-03-01)\n  - was: Arun (until 2026-03-01)", p.read_text())

    def test_dates_are_checked(self):
        self.assertIn("not a date", self.s.remember("Tamsin", "port", "1", since="2026-9-5"))
        self.assertIn("not a date", self.s.remember("Tamsin", "port", "1", since="last week"))
        self.assertIn("more than a year", self.s.remember("Tamsin", "port", "1", since="2027-07-02"))
        self.assertIn("new", self.s.remember("Tamsin", "port", "1", since="2026-07-01"))   # a dated plan

    def test_same_value_from_earlier_moves_its_start(self):
        self.s.remember("Tamsin", "owner", "Mira", since="2026-01-01")
        self.s.remember("Tamsin", "owner", "Arun", since="2026-02-01")
        self.s.remember("Tamsin", "owner", "Arun", since="2026-01-15")
        a = self.attr("owner")
        self.assertEqual((a.value, a.since), ("Arun", "2026-01-15"))
        self.assertEqual([(p.value, p.start, p.end) for p in a.trail], [("Mira", "2026-01-01", "2026-01-15")])

    def test_dated_repeat_of_history_is_not_a_new_entry(self):
        self.s.remember("Tamsin", "host", "A", since="2026-01-01")
        self.s.remember("Tamsin", "host", "B", since="2026-02-01")
        self.s.remember("Tamsin", "host", "C", since="2026-03-01")
        self.assertIn("already recorded", self.s.remember("Tamsin", "host", "A", since="2026-01-10"))
        self.s.remember("Tamsin", "host", "B", since="2026-01-20")   # B started earlier than we knew
        self.assertEqual([(p.value, p.start, p.end) for p in self.attr("host").trail],
                         [("B", "2026-01-20", "2026-03-01"), ("A", "2026-01-01", "2026-01-20")])

    def test_bad_attributes_and_names(self):
        self.assertIn("not recorded", self.s.remember("Tamsin", "port: http", "80"))
        self.assertIn("not recorded", self.s.remember("Tamsin", "", "80"))
        self.assertIn("not recorded", self.s.remember("Tamsin", "notes", "line one\nline two"))
        r = self.s.name("Tamsin", ["S", "it", "[x]"])
        self.assertIn("refused as names: S, it, [x]", r)
        self.s.remember("Tamsin", "GPU model", "GB10")
        self.assertIn("- **GPU model:** GB10", (Path(self.dir.name) / "notes/tamsin.md").read_text())

    def test_import_junk_is_refused(self):
        self.assertIn("status", self.s.remember("Tamsin", "status",
                      "private plugins committed and wrapper fixed in five commits"))
        self.assertIn("since", self.s.remember("Tamsin", "status", "back from Best Buy"))
        self.assertIn("says nothing", self.s.remember("Tamsin", "attribute", "x"))
        for bad in ["ava-flashnext-20260922", "project_x.md", ".env.bak-ablit", "config.yaml.bak"]:
            self.assertIn("not created", self.s.remember(bad, "status", "up", create=True), bad)
        for ok in ["status.timer", "gen_status.py", "config.yaml"]:
            self.assertIn("created", self.s.remember(ok, "schedule", "every 10 minutes", create=True), ok)
        self.assertIn("created Brain", self.s.remember("Brain (LLM)", "model", "Flash-Next", create=True))
        self.assertEqual(self.s.find("Brain").attrs["model"].value, "Flash-Next")
        self.assertIn("created", self.s.remember("api.shopfront.example", "host", "vm-5", create=True))
        self.assertIn("created", self.s.remember("CVE-2026-65660", "status", "patched", create=True))

    def test_alias_lists_are_cleaned(self):
        r = self.s.name("config.yaml", ["hermes config, Ava prompt", "hermes config", "Hermes-Config", "Ava's plan"])
        self.s.name("config.yaml", ["hermes config, Ava prompt"])
        self.assertEqual(self.s.find("config.yaml").aliases, ["hermes config", "Ava prompt"])
        self.assertIn("refused as names: Ava's plan", r)

    def test_separator_variants_are_one_name(self):
        self.s.remember("portfolio-site", "host", "vm-5", since="2026-05-01", create=True)
        self.assertIn("still vm-5", self.s.remember("portfolio site", "host", "vm-5", create=True))
        self.assertEqual(len([r for r in self.s.rows if r.startswith("portfolio")]), 1)

    def test_a_skill_is_not_the_person_it_serves(self):
        self.s.name("Ava", ["Ava agent"], kind="person")
        r = self.s.name("deploy-helper", ["Ava", "deploy-runner"], kind="skill")
        self.assertTrue(r.startswith("created deploy-helper"), r)
        self.assertEqual(self.s.find("deploy-runner").name, "deploy-helper")
        self.assertEqual(self.s.find("Ava").aliases, ["Ava agent"])
        self.s.name("Ava bot", ["Ava"], kind="person")   # same kind: still another name for Ava
        self.assertEqual(self.s.find("Ava bot").name, "Ava")

    def test_import_repeats_do_not_confirm(self):
        self.s.remember("Brain", "model", "qwen3.8-27b", since="2026-05-01", create=True)
        self.assertNotIn("confirmed", self.s.remember("Brain", "model", "qwen3.8-27b", confirm=False))
        self.assertEqual(self.s.find("Brain").attrs["model"].confirmed, "")
        self.assertIn("confirmed 2026-06-01", self.s.remember("Brain", "model", "qwen3.8-27b"))

    def test_the_label_recall_prints_finds_the_thing(self):
        self.s.name("Brain", ["the brain"])
        slug = self.s.find("Brain").slug
        for ref in (slug, f"[{slug} · 2026-06-01]", f"{slug}-history", f"notes/{slug}.md"):
            self.assertEqual(self.s.find(ref).name, "Brain", ref)

    def test_two_writers_share_one_folder(self):
        other = Store(Path(self.dir.name), today="2026-06-01")
        other.name("Orrel", [], "machine")
        self.s.name("Brisk", [], "machine")      # must not drop Orrel from the index
        index = (Path(self.dir.name) / "MEMORY.md").read_text()
        self.assertIn("[Orrel]", index)
        self.assertIn("[Brisk]", index)
        self.assertIn("nothing new", self.s.name("Orrel"))   # sees the other writer's thing


class AliasKindTest(unittest.TestCase):
    def test_other_things_and_descriptions_are_not_names(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d), today="2026-10-08")
            s.name("Sam", [], "person")
            self.assertTrue(s.name("Sam", ["Waikiki Beach"], "place").startswith("not added"))
            self.assertIn("refused", s.name("Sam", ["my mom"], "person"))
            self.assertIn("added names", s.name("Sam", ["Sammy"], "person"))
            self.assertEqual(s.find("Sam").aliases, ["Sammy"])
            self.assertIsNone(s.find("Waikiki Beach"))


if __name__ == "__main__":
    unittest.main()


class TidyTest(unittest.TestCase):
    def test_session_log_settings_move_out(self):
        from deltos.doctor import tidy
        d = tempfile.TemporaryDirectory()
        s = Store(Path(d.name), today="2026-06-01")
        s.remember("Hermes", "setting thinking", "LOW", create=True)
        s.remember("Hermes", "setting compression.threshold_tokens", "140000")
        p = Path(d.name) / "notes" / "hermes.md"
        p.write_text(p.read_text().replace("## Details\n\n", "## Details\n\n- **Setting 141 assistant turns:** 27B run (since 2026-06-01)\n  - was: 12 (2026-05-01 to 2026-06-01)\n", 1))
        self.assertIn("not a setting", s.remember("Hermes", "setting 37 calls", "27B run"))
        self.assertEqual(tidy(Path(d.name), apply=True), {"hermes": 1})
        t = Store(Path(d.name)).find("Hermes")
        self.assertEqual(sorted(t.attrs), ["setting compression.threshold_tokens", "setting thinking"])
        extra = (Path(d.name) / "notes" / "hermes-extra.md").read_text()
        self.assertIn("141 assistant turns", extra)
        self.assertIn("was: 12", extra)


class UnnameTest(unittest.TestCase):
    def test_a_wrong_merge_comes_apart(self):
        d = tempfile.TemporaryDirectory()
        s = Store(Path(d.name), today="2026-06-01")
        s.name("shopfront", ["shopfront.example", "shopfront-api"])
        s.name("shopfront-site", [])
        self.assertIn("skipped names already used", s.name("shopfront-site", ["shopfront.example"]))
        self.assertIn("removed names shopfront.example", s.unname("shopfront", ["shopfront.example", "nope"]))
        self.assertIn("added names shopfront.example", s.name("shopfront-site", ["shopfront.example"]))
        self.assertEqual(Store(Path(d.name)).find("shopfront.example").name, "shopfront-site")
        self.assertEqual(s.find("shopfront-api").name, "shopfront")
        self.assertIn("own name", s.unname("shopfront", ["shopfront"]))

    def test_an_event_status_gives_the_state_back(self):
        from deltos.doctor import restore_status
        d = tempfile.TemporaryDirectory()
        s = Store(Path(d.name), today="2026-10-08")
        s.remember("chatbot", "status", "PAUSED", since="2026-10-02", create=True)
        p = Path(d.name) / "notes" / "chatbot.md"
        p.write_text(p.read_text().replace("- **Status:** PAUSED (since 2026-10-02)",
                                           "- **Status:** brake skipped on the 37 bot profiles (since 2026-10-06)\n"
                                           "  - was: PAUSED (2026-10-02 to 2026-10-06)"))
        self.assertIn("not a status", s.remember("chatbot", "status", "installed Notely 2.18.0"))
        self.assertIn("since", s.remember("Tamsin", "status", "PR closed 2026-10-06", create=True))
        self.assertEqual(restore_status(Path(d.name)), {"chatbot": 1})
        a = Store(Path(d.name)).find("chatbot").attrs["status"]
        self.assertEqual((a.value, a.since, a.trail), ("PAUSED", "2026-10-02", []))
        self.assertIn("brake skipped", (Path(d.name) / "notes" / "chatbot-extra.md").read_text())


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.s = Store(self.root, today="2026-10-08")
        self.s.name("Beth", ["Bethany"], "person")
        self.s.remember("Beth", "status", "partner", since="2026-08-18")
        self.s.remember("Beth", "status", "girlfriend", since="2026-09-28")
        self.s.name("Bethany Ann Marlowe", ["Bethany Marlowe"], "person")
        self.s.remember("Bethany Marlowe", "status", "in school", since="2026-10-03")
        self.s.remember("Bethany Marlowe", "email", "b@example.com", since="2026-07-22")

    def tearDown(self):
        self.dir.cleanup()

    def test_merge_joins_names_entries_and_history(self):
        msg = self.s.merge("Bethany Ann Marlowe", "Beth")
        self.assertIn("merged Beth into Bethany Ann Marlowe", msg)
        self.assertIn("status", msg)   # both had one: say so
        self.assertFalse((self.root / "notes/beth.md").exists())
        t = Store(self.root).find("Beth")   # every old name still finds it, after a fresh index too
        self.assertEqual(t.name, "Bethany Ann Marlowe")
        self.assertEqual(t.attrs["status"].value, "in school")          # newer wins
        self.assertEqual([p.value for p in t.attrs["status"].trail], ["girlfriend", "partner"])
        self.assertEqual(t.attrs["email"].value, "b@example.com")
        self.assertTrue({"Beth", "Bethany", "Bethany Marlowe"} <= set(t.aliases))

    def test_merge_refuses_person_with_thing(self):
        self.s.name("Notely", [], "app")
        self.assertIn("not merged", self.s.merge("Beth", "Notely"))

    def test_name_thing_merge_through_the_surface(self):
        from deltos.memory import Memory
        m = Memory(self.root)
        out = m.call("name_thing", {"thing": "Bethany Ann Marlowe", "other_names": ["Beth"], "merge": True})
        self.assertIn("merged Beth", out)
        self.assertIn("Bethany Ann Marlowe", m.call("memory_show", {"thing": "Beth"}))
