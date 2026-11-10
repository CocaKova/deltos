"""A value someone looked at stands against a note, whatever the note's date: the
note's value is kept beside it ("said otherwise"), the entry is due for a look
first, and a look (or a confirmation, or a new value from a person or agent)
answers it. 11-03 an agent saw a LAN address answer; a to-do line dated 11-05
that still carried the old address replaced it."""
import tempfile
import unittest
from pathlib import Path

import deltos.sources.notes as notes
from deltos.checks import due, render_due
from deltos.store import Store, parse_thing, render_thing


class SaidOtherwiseTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.root = Path(self.d.name)
        s = Store(self.root, today="2026-05-03")
        s.name("Halden", [], "machine")
        s.remember("Halden", "LAN address", "10.0.0.87", source="Ava", check="ping 10.0.0.87 answered")
        s.remember("Halden", "owner", "Rowan", source="Ava")

    def attr(self, label="lan address"):
        return Store(self.root).find("Halden").attrs[label]

    def test_a_later_note_does_not_overturn_a_look(self):
        s = Store(self.root, today="2026-05-05")
        r = s.remember("Halden", "LAN address", "10.0.0.116", since="2026-05-05", confirm=False)
        self.assertTrue(r.startswith("not changed: Halden lan address stays 10.0.0.87"), r)
        self.assertIn("a note doesn't overturn a look", r)
        self.assertEqual(self.attr().value, "10.0.0.87")

    def test_without_a_look_a_later_note_still_changes_an_agents_value(self):
        s = Store(self.root, today="2026-05-05")
        s.remember("Halden", "owner", "Mira", since="2026-05-05", confirm=False)
        self.assertEqual(self.attr("owner").value, "Mira")

    def test_the_import_keeps_what_the_note_said_beside_the_value(self):
        m = notes.Migrator(self.root)
        m.store.today = "2026-05-05"
        st = m.session("wiki@a", "2026-05-05", "", git_mode=True,
                       replay={"raw": "Halden | LAN address | 10.0.0.116 | 2026-05-05"})
        self.assertEqual((st.get("held"), st.get("disputed")), (1, 1))
        a = self.attr()
        self.assertEqual((a.value, a.other), ("10.0.0.87", ("10.0.0.116", "a note", "2026-05-05")))
        text = (self.root / "notes" / "halden.md").read_text()
        self.assertIn("  - said otherwise: 10.0.0.116 (a note, 2026-05-05; not seen)", text)
        self.assertEqual(render_thing(parse_thing(text)), text)   # round trip
        # Due first, with what was said, though it was seen two days ago.
        rows = due(self.root, today="2026-05-05")
        self.assertEqual((rows[0]["attribute"], rows[0]["other"][0]), ("LAN address", "10.0.0.116"))
        self.assertIn("but a note said 10.0.0.116 on 2026-05-05", render_due(rows))
        # The same line again changes nothing.
        st = m.session("wiki@b", "2026-05-05", "", git_mode=True,
                       replay={"raw": "Halden | LAN address | 10.0.0.116 | 2026-05-05"})
        self.assertEqual((st.get("held"), st.get("disputed")), (1, None))

    def test_a_look_answers_it(self):
        s = Store(self.root, today="2026-05-05")
        self.assertTrue(s.dispute("Halden", "LAN address", "10.0.0.116", "2026-05-05"))
        self.assertFalse(s.dispute("Halden", "LAN address", "10.0.0.87", "2026-05-05"))   # the value itself
        s.today = "2026-05-06"
        s.remember("Halden", "LAN address", "10.0.0.87", source="Ava", check="ping 10.0.0.87 answered")
        self.assertIsNone(self.attr().other)
        self.assertEqual(self.attr().confirmed, "2026-05-06")
        # Seen changed: the new value, nothing said otherwise.
        s.dispute("Halden", "LAN address", "10.0.0.116", "2026-05-06")
        s.today = "2026-05-07"
        s.remember("Halden", "LAN address", "10.0.0.116", source="Ava", check="ping 10.0.0.116 answered")
        a = self.attr()
        self.assertEqual((a.value, a.other, a.trail[0].value), ("10.0.0.116", None, "10.0.0.87"))


if __name__ == "__main__":
    unittest.main()
