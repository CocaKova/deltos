"""The per-turn block reads what pages say now: a snapshot page (dated sections that each
restate the list) says its newest section."""
import tempfile
import unittest
from pathlib import Path

from deltos.pages import Pages


def snapshot(days):
    out = ["# To-do", ""]
    for d, extra in days:
        out += [f"## {d}", "- [ ] Water the ferns on the balcony before the frost",
                "- [ ] Call the Halden office about the lease renewal", f"- [ ] {extra}", ""]
    return "\n".join(out)


class CurrentPages(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.home = Path(self.d.name) / "home"
        self.mem = Path(self.d.name) / "mem"
        self.home.mkdir()
        self.mem.mkdir()

    def test_snapshot_page_says_its_newest_section(self):
        (self.home / "todo.md").write_text(snapshot([("2026-05-03", "Book the Quarry dentist"),
                                                     ("2026-05-02", "Return the borrowed tent"),
                                                     ("2026-05-01", "Return the borrowed tent")]))
        p = Pages(self.home)
        self.assertEqual(p.latest_section(self.home / "todo.md"), (3, 7))
        self.assertEqual(p.top("borrowed tent", current=True), [])
        self.assertTrue(p.top("borrowed tent"))   # a search still finds the past
        self.assertTrue(p.top("quarry dentist", current=True))

    def test_a_log_that_adds_facts_is_not_a_snapshot(self):
        (self.home / "proj.md").write_text("# Ledger\n\n## 2026-05-01\nChose Postgres for the ledger store.\n\n"
                                           "## 2026-05-02\nPort moved to 8642 after the clash.\n\n"
                                           "## 2026-05-03\nNightly backup goes to the Annex.\n")
        self.assertIsNone(Pages.latest_section(self.home / "proj.md"))


if __name__ == "__main__":
    unittest.main()
