"""deltos/rg.py: memory reads with ripgrep when it is installed and with the same search
in Python when it is not. The two must print the same lines, and recall and pages must
answer the same with either."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deltos import Memory, rg
from deltos.demo import seed

HAVE_RG = shutil.which("rg") is not None


def tree(root: Path):
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "gateway.md").write_text(
        "# Gateway\n\n- **Port:** 8787 (since 2026-08-01)\n  - was: 8080 (2026-01-01 to 2026-08-01)\n"
        "- **Host:** Box-7 (since 2026-08-01)\n")
    (root / "notes" / "cafe.md").write_text("# Café Órbita\n\n- **Owner:** ÉLODIE (since 2026-08-01)\nport of call\n")
    (root / "notes" / "crlf.md").write_text("first line\r\nthe PORT is open\r\nlast", newline="")
    (root / "notes" / "notes.txt").write_text("port in a text file\n")
    (root / "notes" / "old.md.bak1").write_text("port in a backup\n")
    (root / "notes" / "ARCHIVE-2026.md").write_text("port in an archive\n")
    (root / "notes" / "MEMORY.md").write_text("port in the index\n")
    (root / "notes" / ".hidden.md").write_text("port in a hidden file\n")
    (root / "notes" / ".trash").mkdir()
    (root / "notes" / ".trash" / "gone.md").write_text("port in the trash\n")
    (root / "notes" / "deep" / "er").mkdir(parents=True)
    (root / "notes" / "deep" / "er" / "page.md").write_text("a deep page\nwith a Port: 1 (a.b) [x]* line\n")
    (root / "notes" / "binary.md").write_bytes(b"port\0binary\n")
    (root / "notes" / "empty.md").write_text("")
    os.symlink(root / "notes" / "gateway.md", root / "notes" / "link.md")
    (root / "other").mkdir()
    (root / "other" / "page.md").write_text("another folder's port\n")


CASES = [
    (["-i", "-n", "--no-heading", "-F", "-e", "port", "notes"], True),
    (["-i", "-n", "--no-heading", "-F", "-e", "PORT", "-e", "box-7", "-e", "élodie", "notes"], True),
    (["-i", "-n", "--no-heading", "-F", "-e", "(a.b) [x]*", "-e", "nothing here", "notes"], True),
    (["-i", "-n", "--no-heading", "-F", "-e", "zzz-no-such-text", "notes"], True),
    (["-i", "-n", "--no-heading", "--with-filename", "--glob", "*.md", "--glob", "!*.bak*", "--glob", "!ARCHIVE-*",
      "--glob", "!MEMORY.md", "-F", "-e", "port", "-e", "deep", "{root}/notes", "{root}/other"], False),
    (["-i", "-n", "--no-heading", "--with-filename", "--glob", "*.md", "-F", "-e", "port", "{root}/other"], False),
]


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        tree(self.root)

    def args(self, case):
        return [a.replace("{root}", str(self.root)) for a in case[0]], (self.root if case[1] else None)

    def test_skips_what_ripgrep_skips(self):
        out = rg.search(["-i", "-n", "--no-heading", "-F", "-e", "port", "notes"], self.root)
        files = {ln.split(":")[0] for ln in out.splitlines()}
        self.assertEqual(files, {"notes/gateway.md", "notes/cafe.md", "notes/crlf.md", "notes/notes.txt",
                                 "notes/old.md.bak1", "notes/ARCHIVE-2026.md", "notes/MEMORY.md",
                                 "notes/deep/er/page.md"})
        self.assertIn("notes/gateway.md:3:- **Port:** 8787 (since 2026-08-01)\n", out)

    def test_globs(self):
        args, cwd = self.args(CASES[4])
        files = {os.path.relpath(ln.split(":")[0], self.root) for ln in rg.search(args, cwd).splitlines()}
        self.assertEqual(files, {"notes/gateway.md", "notes/cafe.md", "notes/crlf.md", "notes/deep/er/page.md",
                                 "notes/.hidden.md", "other/page.md"})   # a glob that names a hidden file finds it

    def test_unknown_argument_is_an_error(self):
        with self.assertRaises(ValueError):
            rg.search(["-i", "--pcre2", "-e", "port", "notes"], self.root)

    def test_no_pattern_or_no_path_finds_nothing(self):
        self.assertEqual(rg.search(["-i", "-F", "notes"], self.root), "")
        self.assertEqual(rg.search(["-i", "-F", "-e", "port"], self.root), "")

    def test_run_without_ripgrep(self):
        with mock.patch("shutil.which", return_value=None), \
                mock.patch("subprocess.run", side_effect=AssertionError("ripgrep was called")):
            self.assertFalse(rg.have())
            self.assertIn("notes/gateway.md:3:", rg.run(CASES[0][0], cwd=self.root))
        with mock.patch.dict(os.environ, {"DELTOS_RG": "0"}):
            self.assertFalse(rg.have())

    @unittest.skipUnless(HAVE_RG, "ripgrep is not installed")
    def test_same_lines_as_ripgrep(self):
        for case in CASES:
            args, cwd = self.args(case)
            with mock.patch.dict(os.environ, {"DELTOS_RG": "1"}):
                real = rg.run(args, cwd=cwd)
            self.assertEqual(sorted(real.splitlines()), sorted(rg.search(args, cwd).splitlines()), args)


class MemoryWithoutRipgrepTest(unittest.TestCase):
    """The whole read path on the demo store: the same block and the same page search."""

    QUESTIONS = ["What version is Orchard on?", "Which port is Postgres on?",
                 "what is the local model's url", "Is the old server still running?", "Where does Mara live now?",
                 "what did we decide about the price", "newsletter send day"]

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        seed(self.root / "store", today="2026-11-10")
        pages = self.root / "store" / "pages"
        pages.mkdir()
        (pages / "launch-plan.md").write_text(
            '---\ntitle: "Launch plan"\nupdated: "2026-11-01"\n---\n\n# Launch plan\n\n'
            "Orchard 3.5 ships after the Postgres upgrade. Mara signs off the pricing page.\n")

    def answers(self, flag: str):
        with mock.patch.dict(os.environ, {"DELTOS_RG": flag, "DELTOS_ROOT": str(self.root / "store")}):
            m = Memory(today="2026-11-10")
            m.recall().reload()
            return ([m.context(q) for q in self.QUESTIONS],
                    [m.call("page_search", {"text": q}) for q in ("pricing page", "postgres upgrade", "no such words")])

    def test_answers_without_ripgrep(self):
        blocks, pages = self.answers("0")
        self.assertIn("3.5.0", blocks[0])
        self.assertIn("5432", blocks[1])
        self.assertIn("launch-plan", pages[0])

    @unittest.skipUnless(HAVE_RG, "ripgrep is not installed")
    def test_same_answers_with_and_without_ripgrep(self):
        self.assertEqual(self.answers("1"), self.answers("0"))


if __name__ == "__main__":
    unittest.main()
