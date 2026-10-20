import datetime
import tempfile
import unittest
from pathlib import Path

from deltos.pages import PageError, Pages


class PagesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "pages/wiki").mkdir(parents=True)
        (self.tmp / "mem").mkdir()
        (self.tmp / "pages/wiki/teamwiki.md").write_text(
            "---\ntitle: Team wiki\nupdated: 2026-09-09\n---\n\nThe team wiki serves on port 8080.\n")
        (self.tmp / "pages/wiki/long.md").write_text(
            "---\nupdated: 2026-10-01\n---\n\nA port here.\nA team there.\nA wiki elsewhere.\n")
        (self.tmp / "pages/index.md").write_text("Index of pages\n")
        (self.tmp / "mem/project_notely.md").write_text("---\nname: notely\n---\n\nNotely Link uses :8443.\n")
        self.p = Pages(self.tmp / "pages", {"memory": self.tmp / "mem"})

    def test_top_prefers_words_together(self):
        top = self.p.top("What port is the team wiki on?")
        self.assertEqual(top[0][0], "wiki/teamwiki.md")
        self.assertIn("8080", " ".join(top[0][2]))

    def test_search_and_read_across_folders(self):
        self.assertIn("[memory/project_notely.md", self.p.search("8443"))
        self.assertIn("Notely Link", self.p.read("memory/project_notely"))
        self.assertIn("8080", self.p.read("teamwiki"))   # a bare name resolves when unique

    def test_readonly_folder_refuses_writes(self):
        with self.assertRaisesRegex(PageError, "read-only"):
            self.p.edit("memory/project_notely.md", "x")

    def test_write_edit_forget(self):
        ref = self.p.write("Decision\nWe keep vm-6 for builds.", datetime.datetime(2026, 10, 8, 7, 0))
        self.assertTrue(ref.startswith("notes/2026-10-08-070000-decision"))
        self.p.edit(ref, "vm-7", "vm-6", today="2026-10-09")
        text = self.p.read(ref)
        self.assertIn("vm-7", text)
        self.assertIn("updated: '2026-10-09'", text)
        with self.assertRaisesRegex(PageError, "exactly one"):
            self.p.edit(ref, "x", "not there")
        self.p.forget(ref)
        self.assertEqual(self.p.search("vm-7"), "(no matches)")
        self.assertTrue((self.tmp / "pages/.trash" / ref).exists())
        with self.assertRaisesRegex(PageError, "structural"):
            self.p.forget("index.md")


if __name__ == "__main__":
    unittest.main()


class Echo(unittest.TestCase):
    def test_digest_echo_of_the_question_is_not_a_hit(self):
        with tempfile.TemporaryDirectory() as d:
            sess = Path(d, "sessions"); sess.mkdir()
            (sess / "today.md").write_text(
                "## 09:00 · default · tui · Code name test\n- asked: Do you know the working code name for the memory system?\n"
                "- ended: not processed\n\n## 08:00 · default · tui · Naming\n- asked: is the memory system code name final?\n")
            hits = Pages(Path(d, "w"), {"sessions": sess}).top("Do you know the working code name for the memory system?")
            lines = [ln for _, _, ls in hits for ln in ls]
            self.assertTrue(any("final" in ln for ln in lines))            # another chat still counts
            self.assertFalse(any("not processed" in ln or "Do you know" in ln for ln in lines))


def test_newest_dated_entry_gets_a_slot(tmp_path):
    # The summary lines match the question best; the entry that says how it ended
    # (newest date) still makes the snippet, replacing an undated line.
    (tmp_path / "gate.md").write_text(
        "---\nname: gate_note\ndescription: verify gate on stop, enabled 09-27\nupdated: 2026-09-28\n---\n\n"
        "**Cause:** verify gate on stop enabled in config during the diag\n"
        "**Repro:** verify gate on stop repeats the wrap-up\n"
        "**09-27 on:** verify gate set\n"
        "**✅ 09-28 TURNED OFF:** verify gate false\n")
    _, _, lines = Pages(tmp_path).top("is the verify gate on stop enabled", 1)[0]
    assert any("TURNED OFF" in ln for ln in lines) and len(lines) == 3


def test_entry_date_reads_line_starts_only():
    from deltos.pages import _entry_date
    assert _entry_date("**✅ 09-28 TURNED OFF**", "2026-10-01") == "2026-09-28"
    assert _entry_date("- 2026-10-06 RESOLVED", "2026-10-08") == "2026-10-06"
    assert _entry_date("- 12-20 planned", "2026-10-01") == "2025-12-20"   # after the page: last year
    assert _entry_date("description: fixed 09-27; posted 09-28 upstream after review", "2026-10-01") == ""
    assert _entry_date("Qwen3.8-27B on :8000-12", "2026-10-01") == ""


def test_tied_pages_newest_first(tmp_path):
    for name, day in (("a-old.md", "2026-01-01"), ("b-new.md", "2026-09-01"), ("c-mid.md", "2026-05-01")):
        (tmp_path / name).write_text(f"---\nupdated: {day}\n---\n\nthe zephyr port is 1234\n")
    refs = [r for r, _, _ in Pages(tmp_path).top("zephyr port", 2)]
    assert refs == ["b-new.md", "c-mid.md"]


def _chat(tmp_path):
    (tmp_path / "chat.md").write_text(
        "---\nupdated: 2023-05-28\n---\n\n"
        "user: I'm thinking of cleaning my jewelry collection this weekend. " + "It has been a busy month at work. " * 8
        + "Last month I finally picked up the engagement ring, and the jewelry box is full now.\n"
        "assistant: Cleaning jewelry is easy with a soft cloth.\n"
        "Keep each piece of jewelry in its own pouch, and clean the jewelry collection monthly.\n")


def test_preview_off_keeps_line_starts(tmp_path, monkeypatch):
    import deltos.pages as P
    monkeypatch.setattr(P, "PREVIEW", False)
    _chat(tmp_path)
    lines = P.Pages(tmp_path).top("How many pieces of jewelry did I get, and where is my engagement ring?", 1)[0][2]
    assert not any("engagement ring" in ln for ln in lines)   # cut at LINE_CHARS from the start


def test_preview_shows_the_part_that_matches(tmp_path, monkeypatch):
    import deltos.pages as P
    monkeypatch.setattr(P, "PREVIEW", True)
    _chat(tmp_path)
    lines = P.Pages(tmp_path).top("How many pieces of jewelry did I get, and where is my engagement ring?", 1)[0][2]
    own = [ln for ln in lines if ln.startswith("user: ")]
    assert own and "engagement ring" in own[0] and "… " in own[0] and len(own[0]) <= P.LINE_CHARS
    assert lines.index(own[0]) == 0                          # the person's own line first


def test_preview_keeps_the_assistant_when_asked_what_it_said(tmp_path, monkeypatch):
    import deltos.pages as P
    monkeypatch.setattr(P, "PREVIEW", True)
    _chat(tmp_path)
    lines = P.Pages(tmp_path).top("In our previous conversation, how did you say to store each piece of jewelry?", 1)[0][2]
    assert any("own pouch" in ln for ln in lines)


def test_window_cuts_a_long_sentence_near_its_match(monkeypatch):
    import deltos.pages as P
    monkeypatch.setattr(P, "PREVIEW", True)
    line = "user: " + "word " * 120 + "the zephyr port is 1234 " + "word " * 120
    out = P._window(line, {"zephyr": 1.0})
    assert "zephyr port is 1234" in out and len(out) <= P.LINE_CHARS and out.startswith("user: … ")
    assert P._window("user: short line", {"zephyr": 1.0}) == "user: short line"


def test_search_lines_show_the_match(tmp_path, monkeypatch):
    import deltos.pages as P
    monkeypatch.setattr(P, "PREVIEW", True)
    _chat(tmp_path)
    assert "engagement ring" in P.Pages(tmp_path).search("engagement ring")


def _page(d: Path, name: str, day: str, *lines: str) -> None:
    (d / name).write_text(f"---\nupdated: {day}\n---\n\n" + "\n".join(lines) + "\n")


class Recency(unittest.TestCase):
    """10-19: a page is dated by its newest entry and labelled with its age, the
    previous message's words rank pages. Its age doesn't lower its score, and its
    title line still counts again (both were measured to bury pages that are true)."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.d, ignore_errors=True))

    def refs(self, q, n=5, today="2026-10-19", **kw):
        return [r for r, _, _ in Pages(self.d, today=today).top(q, n, **kw)]

    def _old_and_new(self, old_day, new_day):
        # The old page matches a little better (one more, common word); eight
        # other pages share that word, so it adds 1/9.
        _page(self.d, "old.md", old_day, "the zephyr port is 1234 beta")
        _page(self.d, "new.md", new_day, "the zephyr port is 4321")
        for k in range(8):
            _page(self.d, f"filler{k}.md", new_day, f"beta filler line {k}")

    def test_title_line_still_counts_again(self):
        # The page whose title says it: 5/6 + 5/6 against a newer page's 4/3. Counting
        # the title line once (tried 10-19) put the newer page first, and the gate's
        # live suite lost the page that held the answer.
        _page(self.d, "a.md", "2026-06-01", "title: zephyr gate notes", "", "nothing else here")
        _page(self.d, "b.md", "2026-10-15", "the zephyr port is 9000")
        _page(self.d, "c.md", "2026-10-01", "gate zephyr")
        self.assertEqual(self.refs("zephyr gate port")[0], "a.md")

    def test_an_old_page_that_says_it_best_still_leads(self):
        # An age decay on the score (tried 10-19) put a newer page above an older one
        # that matched better, and buried old pages that are still true.
        self._old_and_new("2026-07-15", "2026-10-19")
        for q in ("zephyr port beta", "what was the zephyr port beta before?"):
            refs = self.refs(q)
            self.assertLess(refs.index("old.md"), refs.index("new.md"), q)

    def test_injected_today_is_deterministic(self):
        self._old_and_new("2026-01-01", "2026-03-01")
        early = Pages(self.d, today="2026-03-01").top("zephyr port beta", 2)
        self.assertEqual(early, Pages(self.d, today="2026-03-01").top("zephyr port beta", 2))
        # today moves the ages, never the order
        self.assertEqual([r for r, _, _ in early], ["old.md", "new.md"])
        self.assertEqual(self.refs("zephyr port beta", 2, today="2031-03-01"), ["old.md", "new.md"])

    def test_page_dated_by_its_newest_entry(self):
        _page(self.d, "note.md", "2026-05-01", "- the zephyr port was 1234", "- 2026-10-06 RESOLVED: zephyr port now 4321")
        _page(self.d, "plan.md", "2026-05-01", "- 2027-01-05 zephyr port move planned")
        dates = {r: d for r, d, _ in Pages(self.d, today="2026-10-19").top("zephyr port", 5)}
        self.assertEqual(dates, {"note.md": "2026-10-06", "plan.md": "2026-10-19"})   # never after today

    def test_extra_terms_rank_pages_but_let_none_in(self):
        _page(self.d, "a.md", "2026-09-01", "the zephyr port is 1234")
        _page(self.d, "b.md", "2026-10-15", "the port is 5678")
        _page(self.d, "c.md", "2026-10-15", "zephyr relay only")
        self.assertEqual(self.refs("the port"), ["b.md", "a.md"])
        self.assertEqual(self.refs("the port", extra_terms={"zephyr": 0.5}), ["a.md", "b.md"])
        # min_cover counts the question's own words only
        _page(self.d, "d.md", "2026-10-15", "port relay mapping")
        self.assertEqual(self.refs("port relay", min_cover=2, extra_terms={"zephyr": 0.5}), ["d.md"])

    def test_age_phrase(self):
        from deltos.pages import age
        self.assertEqual(age("2026-07-10", "2026-10-19"), "3 months old")
        self.assertEqual(age("2026-10-19", "2026-10-19"), "today")
        self.assertEqual(age("2026-10-18", "2026-10-19"), "1 day old")
        self.assertEqual(age("2026-10-06", "2026-10-19"), "13 days old")
        self.assertEqual(age("2026-10-05", "2026-10-19"), "2 weeks old")
        self.assertEqual(age("2026-08-21", "2026-10-19"), "8 weeks old")
        self.assertEqual(age("2026-08-20", "2026-10-19"), "2 months old")
        self.assertEqual(age("", "2026-10-19"), "")
        self.assertEqual(age("2026-02-30", "2026-10-19"), "")

    def test_label_shows_the_age(self):
        from deltos import Memory
        from deltos.config import Config
        wiki = self.d / "wiki"
        wiki.mkdir()
        _page(wiki, "ports.md", "2026-07-10", "the zephyr port is 1234")
        m = Memory(config=Config(root=self.d / "store", pages_write=wiki), today="2026-10-19")
        self.assertIn("[ports.md · 2026-07-10 · 3 months old]\n  the zephyr port is 1234",
                      m.context("what is the zephyr port"))
