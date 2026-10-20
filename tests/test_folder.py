"""A folder of markdown notes as an import source (deltos/sources/folder.py): a new folder gives
whole files, an edit only its new lines, a deleted file nothing; the snapshot of what was
imported moves only with a promoted run (stage/.carry), so a held run finds the same changes
again; skip parts and frontmatter noise are left out; no git anywhere is needed; and deltos
import puts git and folder jobs into one staged run. No model is called. Also the nightly's
learn step (adapters/hermes/nightly.sh) with a stand-in deltos."""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from deltos import cli, llm, runs
from deltos.sources import folder, notes
from deltos.sources.folder import Folder, folder_jobs

QUIET = lambda s: None   # noqa: E731
OCT1 = time.mktime((2026, 10, 1, 12, 0, 0, 0, 0, -1))
OCT2 = time.mktime((2026, 10, 2, 12, 0, 0, 0, 0, -1))


class FakeHealth:
    """deltos.health's API, with the verdict a test picks."""

    def __init__(self, decision="promote"):
        self.decision = decision

    def assess(self, rows, hist, delta=None, errors=None):
        return SimpleNamespace(decision=self.decision, reasons=["a test hold"] if self.decision == "hold" else [],
                               metrics={"n": len(rows)})

    def history(self, root, kind):
        return []

    def store_delta(self, base, after):
        return {}

    def render(self, v, rid, label):
        return f"held {label} run {rid}: {'; '.join(v.reasons)}\n"

    def fingerprint(self, parts):
        return "f" * 12


def lines_of(jobs) -> dict[str, list[str]]:
    """Each file's lines in the jobs' texts, by '<label>/<relpath>' (the block headers)."""
    out, cur = {}, None
    for j in jobs:
        for ln in j[4].splitlines():
            if ln.startswith("--- "):
                cur = ln[4:].removesuffix(" (continued)")
                out.setdefault(cur, [])
            else:
                out[cur].append(ln)
    return out


class FolderBase(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.tmp = Path(d.name)
        self.notes = self.tmp / "notes"
        self.root = self.tmp / "mem"
        (self.root / "notes").mkdir(parents=True)
        self.spec = f"{self.notes}:wiki"

    def write(self, rel: str, text: str, when: float = OCT1) -> Path:
        p = self.notes / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        os.utime(p, (when, when))
        return p

    def promote(self, f: Folder, jobs) -> None:
        """What a promoted run does for a folder: its rows in the log, then .carry onto memory."""
        stage = self.tmp / "stage"
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir()
        if (self.root / "migrate-log.jsonl").exists():
            shutil.copy2(self.root / "migrate-log.jsonl", stage / "migrate-log.jsonl")
        rows = [{"note": j[2], "day": j[1], "chunk": j[3], "of": j[5], "facts": 0} for j in jobs]
        with open(stage / "migrate-log.jsonl", "a") as fh:
            fh.write("".join(json.dumps(r) + "\n" for r in rows))
        f.carry(stage)
        with open(self.root / "migrate-log.jsonl", "a") as fh:
            fh.write("".join(json.dumps(r) + "\n" for r in rows))
        runs._carry(stage / ".carry", self.root, self.tmp / "before")

    def jobs(self, skip=()):
        f = Folder(self.spec, self.root, list(skip))
        return f, f.jobs(notes.done_sessions(self.root))


class FolderJobsTest(FolderBase):
    def test_a_new_folder_gives_whole_files_as_units(self):
        self.write("garden.md", "# Garden\nThe plum tree is by the north fence.\nThe shed is green.\n")
        self.write("trips/rome.md", "# Rome\nThe flight is on Friday.\n", OCT2)
        f, jobs = self.jobs()
        self.assertEqual(lines_of(jobs), {
            "wiki/garden.md": ["# Garden", "The plum tree is by the north fence.", "The shed is green."],
            "wiki/trips/rome.md": ["# Rome", "The flight is on Friday."]})
        # One note per day of mtimes, shaped like the git source's jobs: (day, day, note, piece, text, of).
        self.assertEqual([(j[0], j[1], j[3], j[5]) for j in jobs], [("2026-10-01", "2026-10-01", 0, 1),
                                                                   ("2026-10-02", "2026-10-02", 0, 1)])
        self.assertRegex(jobs[0][2], r"^wiki@2026-10-01-[0-9a-f]{8}$")
        self.assertTrue(jobs[0][4].startswith("--- wiki/garden.md\n"))
        self.assertEqual(folder_jobs(self.spec, self.root, []), jobs)

    def test_a_big_first_import_is_packed_and_bounded(self):
        for i in range(6):
            self.write(f"n{i}.md", "\n".join(f"Line {i}.{k} of a long page about the shed." for k in range(80)))
        f, jobs = self.jobs()
        self.assertGreater(len(jobs), 1)
        self.assertTrue(all(len(j[4]) <= notes.MAX_CHARS + 200 for j in jobs))
        self.assertEqual({j[5] for j in jobs}, {len(jobs)})
        self.assertEqual(sum(len(v) for v in lines_of(jobs).values()), 6 * 80)

    def test_editing_one_line_gives_only_that_line(self):
        self.write("garden.md", "# Garden\nThe plum tree is by the north fence.\nThe shed is green.\n")
        self.write("cars.md", "# Cars\nThe van is blue.\n")
        f, jobs = self.jobs()
        self.promote(f, jobs)
        self.assertEqual(self.jobs()[1], [])
        self.write("garden.md", "# Garden\nThe plum tree is by the north fence.\nThe shed is red now.\n", OCT2)
        f, jobs = self.jobs()
        self.assertEqual(lines_of(jobs), {"wiki/garden.md": ["The shed is red now."]})
        self.assertEqual(jobs[0][0], "2026-10-02")
        self.promote(f, jobs)
        self.write("garden.md", "# Garden\nA new first line.\nThe plum tree is by the north fence.\n"
                                "The shed is red now.\n", OCT2)
        self.assertEqual(lines_of(self.jobs()[1]), {"wiki/garden.md": ["A new first line."]})

    def test_a_deleted_file_gives_nothing(self):
        self.write("garden.md", "# Garden\nThe shed is green.\n")
        self.write("cars.md", "# Cars\nThe van is blue.\n")
        f, jobs = self.jobs()
        self.promote(f, jobs)
        (self.notes / "cars.md").unlink()
        self.write("garden.md", "# Garden\n", OCT2)   # lines removed: not facts either
        self.assertEqual(self.jobs()[1], [])
        self.write("cars.md", "# Cars\nThe van is blue.\nThe van has winter tyres.\n", OCT2)   # back again
        self.assertEqual(lines_of(self.jobs()[1]), {"wiki/cars.md": ["The van has winter tyres."]})

    def test_the_skip_list_and_dot_folders(self):
        self.write("garden.md", "The shed is green.\n")
        self.write("garden.md.bak.md", "The shed is green.\n")
        self.write("old/ARCHIVE-index.md", "Everything ever.\n")
        self.write(".obsidian/workspace.md", "Editor state.\n")
        self.write("notes.txt", "Not markdown.\n")
        f, jobs = self.jobs(skip=[".bak", "ARCHIVE-index", ""])
        self.assertEqual(list(lines_of(jobs)), ["wiki/garden.md"])

    def test_frontmatter_noise_is_dropped(self):
        self.write("garden.md", "---\nupdated: 2026-10-01\ndate: 2026-09-01\nnode_type: page\n"
                                "originSessionId: abc\ntitle: Garden\n---\n\n   \nThe shed is green.\n")
        f, jobs = self.jobs()
        # The same lines the git source drops (its "---" rule only matches a word after the dashes).
        self.assertEqual(lines_of(jobs), {"wiki/garden.md": ["---", "title: Garden", "---", "The shed is green."]})
        self.promote(f, jobs)
        self.write("garden.md", "---\nupdated: 2026-10-02\ndate: 2026-09-01\nnode_type: page\n"
                                "originSessionId: abc\ntitle: Garden\n---\n\n   \nThe shed is green.\n", OCT2)
        f, jobs = self.jobs()
        self.assertEqual(jobs, [])   # only the updated: line changed
        self.assertEqual(len(f.batches), 1)
        self.promote(f, jobs)        # a day with no pieces still advances when a run carries
        self.assertEqual(Folder(self.spec, self.root, []).batches, [])

    def test_no_git_anywhere(self):
        self.write("garden.md", "The shed is green.\n")
        empty = self.tmp / "nobin"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}):
            f, jobs = self.jobs()
            self.assertEqual(lines_of(jobs), {"wiki/garden.md": ["The shed is green."]})
            self.promote(f, jobs)
            (self.root / ".git").mkdir()   # a store that looks like a repo, and no git to ask: still fine
            self.write("garden.md", "The shed is green.\nThe gate is new.\n", OCT2)
            self.assertEqual(lines_of(self.jobs()[1]), {"wiki/garden.md": ["The gate is new."]})
        self.assertTrue((self.root / ".sources" / "wiki" / "manifest.json").is_file())
        self.assertFalse((self.notes / ".git").exists())

    def test_a_store_that_is_a_git_repo_keeps_the_snapshot_out(self):
        if not shutil.which("git"):
            self.skipTest("needs git")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.write("garden.md", "The shed is green.\n")
        self.jobs()
        self.jobs()
        self.assertEqual((self.root / ".git" / "info" / "exclude").read_text().splitlines().count(".sources/"), 1)

    def test_a_label_is_optional(self):
        self.assertEqual(folder.parse("~/notes:notes"), (Path("~/notes").expanduser(), "notes"))
        self.assertEqual(folder.parse(str(self.notes)), (self.notes, "notes"))


class FolderRunTest(FolderBase):
    """deltos import itself, with a stub model and a stand-in verdict."""

    def setUp(self):
        super().setUp()
        real = notes.chat
        self.addCleanup(setattr, notes, "chat", real)
        notes.chat = lambda messages, max_tokens=0, **kw: (
            {"content": "Shed | color | green | -"}, {"completion_tokens": 10, "finish_reason": "stop"})

    def config(self, repos: bool = False) -> None:
        (self.root / "deltos.toml").write_text(
            "[import]\n" + (f'repos = ["{self.tmp}/repo:HEAD:repo"]\n' if repos else "")
            + f'folders = ["{self.spec}"]\nworkers = 1\n')

    def imp(self, decision="promote", git=()) -> int:
        with mock.patch.dict(os.environ), mock.patch.object(notes, "git_jobs", return_value=list(git)), \
                mock.patch.object(llm, "configure"), mock.patch.object(runs, "health", FakeHealth(decision)), \
                mock.patch("sys.stdout"):
            os.environ.pop("DELTOS_CONFIG", None)
            return cli.main(["--root", str(self.root), "import"])

    def test_a_held_run_leaves_the_snapshot_and_gives_the_same_units_next_time(self):
        self.config()
        self.write("garden.md", "# Garden\nThe shed is green.\n")
        first = self.jobs()[1]
        self.assertEqual(self.imp("hold"), 3)
        self.assertFalse((self.root / ".sources").exists())
        rid = runs.ledger(self.root)[-1]["id"]
        carried = self.root / ".runs" / rid / "stage" / ".carry" / ".sources" / "wiki"
        self.assertEqual(json.loads((carried / "manifest.json").read_text()), {"garden.md": notes_sha(self.notes / "garden.md")})
        runs.discard(self.root, rid, say=QUIET)
        self.assertEqual(self.jobs()[1], first)
        self.assertEqual(self.imp("promote"), 0)
        self.assertEqual(runs.ledger(self.root)[-1]["decision"], "promoted")
        self.assertEqual((self.root / ".sources" / "wiki" / "files" / "garden.md").read_text(), "# Garden\nThe shed is green.\n")
        self.assertEqual(self.jobs()[1], [])
        # Undone, the snapshot goes back with the run, and the units come back without the hold.
        runs.undo(self.root, runs.ledger(self.root)[-1]["id"], hold=False, say=QUIET)
        self.assertEqual(self.jobs()[1], first)

    def test_a_piece_cut_by_the_limit_is_read_again(self):
        self.config()
        for i in range(4):
            self.write(f"n{i}.md", "\n".join(f"The shed note {i}.{k} says the shed is green." for k in range(60)))
        first = self.jobs()[1]
        self.assertGreater(len(first), 1)
        with mock.patch.dict(os.environ), mock.patch.object(llm, "configure"), \
                mock.patch.object(runs, "health", FakeHealth()), mock.patch("sys.stdout"):
            os.environ.pop("DELTOS_CONFIG", None)
            self.assertEqual(cli.main(["--root", str(self.root), "import", "--limit", "1"]), 0)
        self.assertFalse((self.root / ".sources").exists())   # the day is not whole yet
        self.assertEqual(self.jobs()[1], first[1:])
        self.assertEqual(self.imp(), 0)
        self.assertEqual(self.jobs()[1], [])
        self.assertEqual(len(json.loads((self.root / ".sources" / "wiki" / "manifest.json").read_text())), 4)

    def test_the_cli_import_merges_git_and_folder_jobs_into_one_run(self):
        self.config(repos=True)
        self.write("garden.md", "# Garden\nThe shed is green.\n")
        git = [("2026-09-30", "2026-09-30", "repo@abc1234", 0, "--- repo/sheds.md\nThe shed is green.\n", 1)]
        mine = self.jobs()[1]
        self.assertEqual(len(mine), 1)
        before = len(runs.ledger(self.root))
        self.assertEqual(self.imp(git=git), 0)
        led = runs.ledger(self.root)[before:]
        self.assertEqual([(r["kind"], r["decision"], r["rows"]) for r in led], [("note", "promoted", 2)])
        rows = [json.loads(x) for x in (self.root / "migrate-log.jsonl").read_text().splitlines()]
        self.assertEqual([r["note"] for r in rows], ["repo@abc1234", mine[0][2]])   # sorted by day, one run
        self.assertRegex(rows[1]["note"], r"^wiki@2026-10-01-")
        self.assertEqual(self.jobs()[1], [])
        # Nothing new in either: no run at all.
        self.assertEqual(self.imp(git=[]), 0)
        self.assertEqual(len(runs.ledger(self.root)), before + 1)


def notes_sha(p: Path) -> str:
    return hashlib.sha1(p.read_bytes()).hexdigest()


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class NightlyLearnTest(unittest.TestCase):
    """adapters/hermes/nightly.sh runs `deltos learn --wait` after the import: a held sweep is
    logged with its hold file's first line, a failed one with its code, and neither changes
    the job's exit code."""
    SCRIPT = Path(__file__).resolve().parents[1] / "adapters" / "hermes" / "nightly.sh"

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.tmp = Path(d.name)
        (self.tmp / "src" / "deltos").mkdir(parents=True)
        (self.tmp / "src" / "deltos" / "__init__.py").write_text("")
        (self.tmp / "mem").mkdir()

    def nightly(self, learn_rc: int) -> tuple[int, str, list[str]]:
        (self.tmp / "src" / "deltos" / "__main__.py").write_text(
            "import sys\n"
            "root = sys.argv[sys.argv.index('--root') + 1]\n"
            "open(root + '/calls', 'a').write(' '.join(sys.argv[3:]) + '\\n')\n"
            "if sys.argv[3] == 'learn':\n"
            f"    print('stand-in learn')\n"
            f"    if {learn_rc} == 3:\n"
            "        open(root + '/learn.hold', 'w').write('held learn run r1: loops in 3 of 4 pieces\\n')\n"
            f"    sys.exit({learn_rc})\n"
            "print('stand-in ' + sys.argv[3])\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("DELTOS_", "PYTHON"))}
        env.update(HOME=str(self.tmp), DELTOS_SRC=str(self.tmp / "src"), DELTOS_ROOT=str(self.tmp / "mem"),
                   DELTOS_SITE=str(self.tmp / "no-site.sh"))
        p = subprocess.run(["bash", str(self.SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
        log = (self.tmp / "mem" / "nightly.log").read_text()
        return p.returncode, log, (self.tmp / "mem" / "calls").read_text().splitlines()

    def test_the_learn_sweep_runs_after_the_import(self):
        rc, log, calls = self.nightly(0)
        self.assertEqual((rc, calls), (0, ["import", "learn --wait", "sessions --days 2", "status --line"]))
        self.assertIn("stand-in learn", log)
        self.assertNotIn("learn HELD", log)
        self.assertNotIn("learn FAILED", log)

    def test_a_held_sweep_is_logged_with_its_reason(self):
        rc, log, calls = self.nightly(3)
        self.assertEqual(rc, 0)
        self.assertIn("learn HELD: held learn run r1: loops in 3 of 4 pieces", log)
        self.assertEqual(calls[-2:], ["sessions --days 2", "status --line"])

    def test_a_failed_sweep_is_logged_and_the_job_goes_on(self):
        rc, log, calls = self.nightly(1)
        self.assertEqual(rc, 0)
        self.assertIn("learn FAILED rc=1", log)
        self.assertEqual(calls, ["import", "learn --wait", "sessions --days 2", "status --line"])


if __name__ == "__main__":
    unittest.main()
