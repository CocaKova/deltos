"""Trust order (a person's value stands against an agent's and a note import's,
an agent's against a note import's, unless the news is dated after it) and
time travel (what memory holds as true on a day)."""
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import deltos.sources.notes as notes
from deltos import Memory
from deltos.cli import main as cli
from deltos.config import Config
from deltos.memory import TOOLS, show_tool
from deltos.store import AGENT, HAND, IMPORT, PERSON, Store, held_trust, parse_thing


class TrustTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.root = Path(self.d.name)
        self.s = Store(self.root, today="2026-05-01")
        self.s.name("Tamsin", ["TS-2"], "machine")

    def attr(self):
        return Store(self.root).find("Tamsin").attrs["location"]

    def test_an_import_does_not_replace_an_agents_value_with_news_not_dated_after_it(self):
        self.s.remember("Tamsin", "location", "Bay 3", source="Ava")
        r = self.s.remember("Tamsin", "location", "Dock B", since="2026-05-01", confirm=False)
        self.assertTrue(r.startswith("not changed: Tamsin location stays Bay 3"), r)
        self.assertIn("set by Ava on 2026-05-01", r)
        self.assertEqual(self.attr().value, "Bay 3")
        # Older news still lands in the trail: it doesn't touch the current value.
        r = self.s.remember("Tamsin", "location", "the north bench", since="2026-03-01", confirm=False)
        self.assertIn("recorded as a past value", r)
        # News dated after it is a change.
        self.s.remember("Tamsin", "location", "the Annex", since="2026-05-02", confirm=False)
        a = self.attr()
        self.assertEqual((a.value, a.trail[0].value), ("the Annex", "Bay 3"))

    def test_a_confirmation_stands_until_its_own_day(self):
        self.s.remember("Tamsin", "location", "Bay 3", since="2026-04-01", confirm=False)   # an import
        self.s.today = "2026-06-01"
        self.s.remember("Tamsin", "location", "Bay 3", source="Ava")                        # seen again
        self.assertEqual(held_trust(self.attr()), (AGENT, "2026-06-01"))
        r = self.s.remember("Tamsin", "location", "Dock B", since="2026-05-20", confirm=False)
        self.assertIn("confirmed on 2026-06-01", r)
        self.assertEqual(self.attr().value, "Bay 3")
        self.s.remember("Tamsin", "location", "Dock B", since="2026-06-02", confirm=False)
        self.assertEqual(self.attr().value, "Dock B")

    def test_a_persons_value_stands_against_an_agent_the_same_day(self):
        self.s.remember("Tamsin", "location", "Bay 3", source=HAND)
        self.assertIn("source: hand", (self.root / "notes" / "tamsin.md").read_text())
        r = self.s.remember("Tamsin", "location", "Dock B", source="Ava")
        self.assertIn("set by hand on 2026-05-01; an agent changes it only with news dated after", r)
        self.s.today = "2026-05-02"
        self.s.remember("Tamsin", "location", "Dock B", source="Ava")
        self.assertEqual(self.attr().value, "Dock B")

    def test_equal_or_higher_trust_writes_as_before(self):
        self.s.remember("Tamsin", "location", "Bay 3", source="Ava")
        self.s.remember("Tamsin", "location", "Dock B", source="Bo")       # another agent, same day
        self.assertEqual(self.attr().value, "Dock B")
        self.s.remember("Tamsin", "location", "the Annex", source=HAND)    # a person, same day
        self.assertEqual(self.attr().value, "the Annex")
        self.s.remember("Tamsin", "location", "Bay 1", since="2026-05-01", confirm=False)
        self.assertEqual(self.attr().value, "the Annex")                   # a note import can't

    def test_a_less_trusted_correction_is_a_change_or_nothing(self):
        self.s.remember("Tamsin", "location", "Bay 3", source="Ava")
        r = self.s.remember("Tamsin", "location", "Dock B", correction=True, confirm=False)
        self.assertTrue(r.startswith("not changed:"), r)
        self.s.today = "2026-05-03"
        self.s.remember("Tamsin", "location", "Dock B", correction=True, confirm=False)
        a = self.attr()
        # Not "never true": a note can't overrule what an agent saw, only report a change.
        self.assertEqual((a.value, a.trail[0].value, a.trail[0].wrong), ("Dock B", "Bay 3", False))

    def test_who_stands_behind_a_value(self):
        t = parse_thing('---\ntitle: "X"\n---\n\n# X\n\nX.\n\n## Details\n\n- **Owner:** Arun\n'
                        "- **Host:** box-7 (since 2026-01-01)\n- **Port:** 80 (since 2026-01-01; source: Ava)\n"
                        "- **Mode:** fast (since 2026-01-01; source: hand)\n")
        self.assertEqual([held_trust(t.attrs[k])[0] for k in ("owner", "host", "port", "mode")],
                         [PERSON, IMPORT, AGENT, PERSON])

    def test_the_import_holds_the_line_without_a_repair_call(self):
        self.s.remember("Tamsin", "location", "Bay 3", source="Ava")
        calls = []
        real = notes.chat
        self.addCleanup(setattr, notes, "chat", real)
        notes.chat = lambda *a, **k: calls.append(a) or ({"content": ""}, {})
        m = notes.Migrator(self.root)
        st = m.session("memory@a", "2026-05-01", "", git_mode=True, replay={"raw": "Tamsin | location | Dock B | -"})
        self.assertEqual((st.get("held"), st.get("refused"), calls), (1, 0, []))
        self.assertIn("not changed: Bay 3 stays, set by Ava on 2026-05-01", st["refusals"][0])
        self.assertIn("held 1", notes.summary([st]))
        st = m.session("memory@b", "2026-05-04", "", git_mode=True, replay={"raw": "Tamsin | location | Dock B | -"})
        self.assertEqual((st.get("held"), st["facts"]), (None, 1))
        self.assertEqual(self.attr().value, "Dock B")


class AsOfTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.root = Path(self.d.name)
        s = self.s = Store(self.root, today="2026-09-01")
        s.name("Tamsin", ["TS-2"], "machine")
        for v, d in (("the north bench", "2026-01-09"), ("Bay 3 rack 12", "2026-03-06"), ("the Annex closet", "2026-05-09"),
                     ("Dock B", "2026-07-01")):
            s.remember("Tamsin", "location", v, since=d)
        s.remember("Tamsin", "serial number", "SN-11542-T", since="2026-01-23")
        s.remember("Tamsin", "serial number", "SN-48213-K", correction=True)
        s.remember("Tamsin", "status", "retired", since="2026-08-20")
        s.remember("Tamsin", "client", "Acme", since="2026-02-01")
        s.remember("Tamsin", "client", "Brightside", since="2026-06-01", another=True)

    def line(self, day, label):
        out = self.s.as_of("TS-2", day)
        return next(ln for ln in out.splitlines() if ln.startswith(f"- **{label}:**"))

    def test_each_entry_on_a_day(self):
        self.assertTrue((self.root / "notes" / "tamsin-history.md").exists())   # the first value is cold
        self.assertEqual(self.line("2026-02-01", "Location"), "- **Location:** the north bench (2026-01-09 to 2026-03-06)")
        self.assertEqual(self.line("2026-05-09", "Location"), "- **Location:** the Annex closet (2026-05-09 to 2026-07-01)")
        self.assertEqual(self.line("2026-08-01", "Location"), "- **Location:** Dock B (since 2026-07-01, still current)")
        self.assertEqual(self.line("2026-01-01", "Location"),
                         "- **Location:** nothing known on that day (first value from 2026-01-09)")

    def test_a_mistake_is_never_offered(self):
        self.assertEqual(self.line("2026-02-01", "Serial number"),
                         "- **Serial number:** SN-48213-K (since 2026-01-23, still current)")
        self.assertNotIn("SN-11542-T", self.s.as_of("Tamsin", "2026-02-01"))

    def test_status_and_items(self):
        self.assertEqual(self.line("2026-08-25", "Status"), "- **Status:** retired / decommissioned since 2026-08-20")
        self.assertIn("nothing known on that day", self.line("2026-08-01", "Status"))
        self.assertEqual(self.line("2026-03-01", "Client"), "- **Client:** Acme (from 2026-02-01, still current)")
        self.assertEqual(self.line("2026-07-01", "Client"),
                         "- **Client:** Brightside (since 2026-06-01, still current); also Acme (from 2026-02-01, "
                         "still current)")
        self.assertIn("first value from 2026-02-01", self.line("2026-01-01", "Client"))

    def test_through_the_tool_and_the_cli(self):
        m = Memory(config=Config(root=self.root, owner="Sam"), agent="Ava")
        self.assertIn("the north bench", m.call("memory_show", {"thing": "Tamsin", "as_of": "2026-02-01"}))
        self.assertIn("not a date", m.call("memory_show", {"thing": "Tamsin", "as_of": "Feb 1"}))
        show = next(t for t in TOOLS if t["name"] == "memory_show")
        self.assertNotIn("as_of", show["parameters"]["properties"])          # opt-in until scored on two models
        self.assertIn("as_of", show_tool(True)["parameters"]["properties"])
        out = io.StringIO()
        with redirect_stdout(out):
            cli(["--root", str(self.root), "show", "Tamsin", "--as-of", "2026-04-01"])
            cli(["--root", str(self.root), "remember", "Tamsin", "owner", "Arun"])
        self.assertIn("Bay 3 rack 12 (2026-03-06 to 2026-05-09)", out.getvalue())
        self.assertIn("source: hand", (self.root / "notes" / "tamsin.md").read_text())


if __name__ == "__main__":
    unittest.main()
