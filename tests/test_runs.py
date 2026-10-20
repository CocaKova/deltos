"""Staged runs (deltos/runs.py): live memory is untouched until promote, promote replays the
run under the store lock so an agent's write made meanwhile stands, a held run leaves memory
byte-identical, and undo takes out one run (with or without git) without undoing an entry
someone changed after it. Verdicts come from a stand-in for deltos/health.py so each test
picks promote or hold; no model is called."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import deltos

try:
    from deltos import health
except ImportError:   # deltos/health.py not on this branch yet: a stand-in with its API
    health = types.ModuleType("deltos.health")
    health.assess = lambda rows, hist, delta=None, errors=None: SimpleNamespace(
        decision="promote" if rows else "empty", reasons=[], metrics={})
    health.history = lambda root, kind: []
    health.store_delta = lambda base, after: {}
    health.render = lambda v, rid, label: f"held {label} run {rid}: {'; '.join(v.reasons)}\n"
    health.fingerprint = lambda parts: "0" * 12
    sys.modules["deltos.health"] = health
    deltos.health = health

from deltos import cli, llm, runs                 # noqa: E402
from deltos.memory import Memory                  # noqa: E402
from deltos.sources import notes                  # noqa: E402
from deltos.store import Store, parse_thing       # noqa: E402

_lines = runs._lines

QUIET = lambda s: None   # noqa: E731

# Two logged import sessions (what a model said), applied again with no model.
ROWS = [
    {"note": "notes@a1", "day": "2026-10-04", "chunk": 0, "of": 1, "facts": 4,
     "raw": "Nimbus | version | 4.1 | 2026-10-04\n"
            "Nimbus | host | box-9 | 2026-10-04\n"
            "Nimbus | status | restarted at 10:42 by the nightly cron job | 2026-10-04\n"
            "= Quill | | app\n"
            "Quill | port | 8080 | 2026-10-04"},
    {"note": "notes@a2", "day": "2026-10-05", "chunk": 0, "of": 1, "facts": 1,
     "raw": "Brisk | port | 9200 | 2026-10-05\n"
            "+ 2026-10-05 | moved Brisk to the new rack"},
]
TEXTS = {("notes@a1", 0, None): "Nimbus 4.1 is out; it moved to box-9 and restarted at 10:42 by the nightly "
                                 "cron job. Quill listens on 8080.",
         ("notes@a2", 0, None): "Brisk now on port 9200; moved Brisk to the new rack."}
LATER = [{"note": "notes@b1", "day": "2026-10-06", "chunk": 0, "of": 1, "facts": 1,
          "raw": "Nimbus | status | paused for the weekly disk check by Dana | 2026-10-06\n"
                 "Brisk | version | 2.0 | 2026-10-06"}]


class FakeHealth:
    """deltos.health's API, with the verdict a test picks."""

    def __init__(self, decision="promote", reasons=()):
        self.decision, self.reasons, self.seen = decision, list(reasons), []

    def assess(self, rows, hist, delta=None, errors=None):
        self.seen.append((rows, hist, delta, errors))
        return SimpleNamespace(decision=self.decision, reasons=self.reasons, metrics={"n": len(rows)})

    def history(self, root, kind):
        return []

    def store_delta(self, base, after):
        return {}

    def render(self, v, rid, label):
        return f"held {label} run {rid}: {'; '.join(v.reasons)}\n  loops 2 of 2 (normal 0 of 0)\n" \
               f"deltos run promote {rid}\ndeltos run discard {rid}\n"

    def fingerprint(self, parts):
        return "f" * 12


def tree(root: Path, *top: str) -> dict:
    """Every file under notes/ (and the named top-level files) with its bytes."""
    out = {p.relative_to(root).as_posix(): p.read_bytes() for p in (root / "notes").rglob("*") if p.is_file()}
    out.update({f: (root / f).read_bytes() for f in top if (root / f).exists()})
    return out


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


class RunsTest(unittest.TestCase):
    GIT = True

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.tmp = Path(d.name)
        self.root = self.tmp / "mem"
        s = Store(self.root)
        s.name("Nimbus", ["nb"], "app")
        s.remember("Nimbus", "host", "box-7", since="2026-10-01", create=True, confirm=False)   # an import's
        s.remember("Nimbus", "owner", "Dana", since="2026-10-01", source="Ava")                 # the agent's
        s.name("Brisk", [], "service")
        s.remember("Brisk", "port", "9100", since="2026-10-01", create=True, confirm=False)
        s.aside("Nimbus", ["- note: an older aside (2026-09-01)"])
        (self.root / "migrate-log.jsonl").write_text(
            json.dumps({"note": "notes@old", "day": "2026-10-01", "chunk": 0, "raw": "", "facts": 0}) + "\n")
        if self.GIT:
            git(self.root, "init", "-q")
            git(self.root, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
            git(self.root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "start")
        self.top = ("MEMORY.md", "migrate-log.jsonl", "migrate-texts.jsonl", "migrate-errors.jsonl")

    def run_(self, decision="promote", rows=ROWS, texts=TEXTS, work=None, reasons=("loops in 2 of 2 pieces",)):
        fake = FakeHealth(decision, reasons if decision == "hold" else ())
        with mock.patch.object(runs, "health", fake):
            rc = runs.execute(self.root, "note", work or runs.replay_work(rows, texts), label="import", say=QUIET,
                              owner="Sam", fingerprint=fake.fingerprint({}))
        return rc, runs.ledger(self.root)[-1]["id"]

    def remember(self, thing, attribute, value):
        return Memory(self.root, agent="Ava").call("remember", {"thing": thing, "attribute": attribute, "value": value})

    def attrs(self, thing):
        t = Store(self.root).find(thing)
        return {} if t is None else {k: (a.value, a.source) for k, a in t.attrs.items()}

    # 1
    def test_live_is_untouched_until_promote(self):
        snap, seen = tree(self.root, *self.top), {}

        def work(stage):
            runs.replay_work(ROWS, TEXTS)(stage)
            seen["live"], seen["stage"] = tree(self.root, *self.top), tree(stage, *self.top)
        rc, rid = self.run_(work=work)
        self.assertEqual(rc, 0)
        self.assertEqual(seen["live"], snap)
        self.assertNotEqual(seen["stage"], snap)
        self.assertEqual(self.attrs("Nimbus")["version"], ("4.1", ""))
        self.assertEqual(self.attrs("Quill")["port"], ("8080", ""))
        j = runs.journal(self.root, rid)
        self.assertEqual(j["status"], "promoted")
        self.assertFalse((self.root / runs.RUNS / rid / "stage").exists())
        self.assertIn("notes/nimbus.md", j["changed"])
        # the logged rows reach the live log exactly as logged, with their texts
        live = (self.root / "migrate-log.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(x) for x in live[1:]], ROWS)
        self.assertEqual(len((self.root / "migrate-texts.jsonl").read_text().splitlines()), 2)

    # 2
    def test_agent_write_during_the_run_survives_promote_and_stays_out_of_the_run_commit(self):
        def work(stage):
            runs.replay_work(ROWS, TEXTS)(stage)
            self.remember("Nimbus", "host", "box-12")   # on live, while the run works on its copy
        rc, rid = self.run_(work=work)
        self.assertEqual(rc, 0)
        self.assertEqual(self.attrs("Nimbus")["host"], ("box-12", "Ava"))
        self.assertEqual(self.attrs("Nimbus")["version"], ("4.1", ""))
        if not self.GIT:
            return
        commit = runs.ledger(self.root)[-1]["commit"]
        self.assertTrue(commit)
        self.assertIn(f"import run {rid}: import summary:", git(self.root, "log", "-1", "--format=%s", commit))
        self.assertEqual(git(self.root, "log", "-1", "--format=%an", commit).strip(), "deltos")
        added = [ln for ln in git(self.root, "show", commit).splitlines() if ln.startswith("+")]
        self.assertTrue(any("4.1" in ln for ln in added))
        self.assertFalse(any("box-12" in ln for ln in added))
        during = git(self.root, "log", "-1", "--format=%s", commit + "^")
        self.assertEqual(during.strip(), f"writes during import run {rid}")
        self.assertIn("box-12", git(self.root, "show", commit + "^"))

    # 3
    def test_hold_leaves_memory_byte_identical_and_a_later_promote_replays_onto_newer_memory(self):
        snap = tree(self.root, *self.top)
        rc, rid = self.run_("hold")
        self.assertEqual(rc, 3)
        hold = (self.root / "import.hold").read_text()
        self.assertTrue(hold.startswith(f"held import run {rid}: loops in 2 of 2 pieces\n"))
        self.assertIn(f"deltos run promote {rid}", hold)
        self.assertEqual(tree(self.root, *self.top), snap)
        row = runs.ledger(self.root)[-1]
        self.assertEqual((row["decision"], row["reasons"], row["fingerprint"]), ("held", ["loops in 2 of 2 pieces"], "f" * 12))
        self.assertTrue((self.root / runs.RUNS / rid / "stage").is_dir())
        # while held, that kind does not run again
        called = []
        rc2, _ = self.run_(work=lambda st: called.append(st))
        self.assertEqual((rc2, called), (0, []))
        self.assertIn("HOLD import.hold", runs.status(self.root, line=True))
        # memory moves on; then the held run is promoted by hand
        self.remember("Brisk", "status", "live")
        j = runs.promote(self.root, rid, by="hand", say=QUIET)
        self.assertEqual(j["status"], "promoted")
        self.assertEqual(self.attrs("Brisk")["status"], ("live", "Ava"))
        self.assertEqual(self.attrs("Brisk")["port"], ("9200", ""))
        self.assertEqual(self.attrs("Nimbus")["version"], ("4.1", ""))
        self.assertFalse((self.root / "import.hold").exists())
        self.assertEqual((runs.ledger(self.root)[-1]["decision"], runs.ledger(self.root)[-1]["by"]), ("promoted", "hand"))
        with self.assertRaises(runs.RunError):
            runs.promote(self.root, rid, say=QUIET)

    # 4
    def test_undo_with_no_later_writes_restores_memory_byte_for_byte(self):
        snap = tree(self.root, *self.top)
        rc, rid = self.run_()
        self.assertNotEqual(tree(self.root, *self.top), snap)
        j = runs.undo(self.root, rid, hold=False, say=QUIET)
        self.assertEqual(tree(self.root, *self.top), snap)
        self.assertEqual((j["kept"], j["skipped"]), ([], []))
        self.assertFalse((self.root / "import.hold").exists())
        self.assertEqual(runs.ledger(self.root)[-1]["decision"], "undone")
        if self.GIT:
            self.assertEqual(git(self.root, "log", "-1", "--format=%s").strip(), f"undo import run {rid}")
            self.assertEqual(git(self.root, "status", "--porcelain", "--", "notes", "MEMORY.md", "migrate-log.jsonl"), "")

    def test_undo_writes_the_hold_file_so_the_units_do_not_come_straight_back(self):
        rc, rid = self.run_()
        runs.undo(self.root, rid, say=QUIET)
        self.assertEqual((self.root / "import.hold").read_text(),
                         f"undone import run {rid}; delete this file to import those units again\n")
        with self.assertRaises(runs.RunError):
            runs.undo(self.root, rid, say=QUIET)

    # 5
    def test_undo_after_a_later_agent_write_keeps_it_and_reverts_the_runs_entries(self):
        rc, rid = self.run_()
        self.remember("Nimbus", "model", "Orca-2")     # a new entry on a profile the run wrote
        self.remember("Nimbus", "version", "4.2")      # an entry the run wrote, changed since
        j = runs.undo(self.root, rid, hold=False, say=QUIET)
        a = self.attrs("Nimbus")
        self.assertEqual(a["model"], ("Orca-2", "Ava"))
        self.assertEqual(a["version"], ("4.2", "Ava"))
        self.assertEqual(a["host"], ("box-7", ""))      # the run's box-9 is gone
        self.assertEqual(a["owner"], ("Dana", "Ava"))
        self.assertEqual(self.attrs("Quill"), {})       # the thing the run made is gone
        self.assertEqual(self.attrs("Brisk")["port"], ("9100", ""))
        self.assertEqual(j["kept"], ["notes/nimbus.md version: kept, changed after the run"])
        self.assertNotIn("Quill", (self.root / "MEMORY.md").read_text())

    # 6
    def test_undo_removes_exactly_the_runs_log_and_set_aside_lines(self):
        rc, first = self.run_()
        rc, second = self.run_(rows=LATER, texts={})
        extra = self.root / "notes" / "nimbus-extra.md"
        self.assertIn("restarted at 10:42", extra.read_text())
        self.assertIn("paused for the weekly disk check", extra.read_text())
        runs.undo(self.root, first, hold=False, say=QUIET)
        text = extra.read_text()
        self.assertIn("an older aside", text)
        self.assertIn("paused for the weekly disk check", text)
        self.assertNotIn("restarted at 10:42", text)
        log = [json.loads(x)["note"] for x in (self.root / "migrate-log.jsonl").read_text().splitlines()]
        self.assertEqual(log, ["notes@old", "notes@b1"])
        self.assertFalse((self.root / "migrate-texts.jsonl").exists())
        self.assertFalse(list((self.root / "notes").glob("log-*.md")))   # the run's daily-log line, its file
        self.assertEqual(self.attrs("Brisk")["version"], ("2.0", ""))   # the later run's entry stays

    # 7
    def test_half_promoted_run_undo_keeps_agent_entries(self):
        snap = tree(self.root, "migrate-log.jsonl")
        with mock.patch.object(runs, "_carry", side_effect=RuntimeError("killed")):
            with self.assertRaises(RuntimeError):
                self.run_()
        rid = runs.ledger(self.root)[-1]["id"]
        self.assertEqual(runs.ledger(self.root)[-1]["decision"], "failed")
        self.assertEqual(runs.journal(self.root, rid)["status"], "promoting")
        self.assertEqual(runs.promoting(self.root), [rid])
        self.assertIn(f"HALF-PROMOTED {rid}", runs.status(self.root, line=True))
        self.assertEqual(self.run_(rows=LATER)[0], 1)   # nothing new is staged on top of it
        self.remember("Nimbus", "version", "4.3")
        self.remember("Nimbus", "model", "Orca-2")
        j = runs.undo(self.root, rid, hold=False, say=QUIET)
        self.assertTrue(j["crash"])
        a = self.attrs("Nimbus")
        self.assertEqual((a["version"], a["model"], a["owner"]), (("4.3", "Ava"), ("Orca-2", "Ava"), ("Dana", "Ava")))
        self.assertEqual(a["host"], ("box-7", ""))
        self.assertEqual(self.attrs("Quill"), {})
        self.assertEqual(self.attrs("Brisk")["port"], ("9100", ""))
        self.assertEqual(tree(self.root, "migrate-log.jsonl")["migrate-log.jsonl"], snap["migrate-log.jsonl"])
        self.assertNotIn("restarted at 10:42", (self.root / "notes" / "nimbus-extra.md").read_text())
        self.assertEqual(runs.promoting(self.root), [])

    # 9
    def test_a_concurrent_run_is_refused(self):
        inner = []
        rc, _ = self.run_(work=lambda st: inner.append(runs.execute(self.root, "chat", lambda s: None, label="learn",
                                                                       say=QUIET)))
        self.assertEqual((rc, inner), (0, [1]))

    # 10
    def test_an_empty_run_leaves_no_folder(self):
        rc, rid = self.run_(work=lambda st: None)
        self.assertEqual(rc, 0)
        self.assertEqual(runs.ledger(self.root)[-1]["decision"], "empty")
        self.assertEqual([p.name for p in (self.root / runs.RUNS).iterdir()], [".lock"])

    # 11
    def test_an_exception_in_the_work_leaves_memory_untouched(self):
        snap = tree(self.root, *self.top)

        def work(stage):
            runs.replay_work(ROWS, TEXTS)(stage)
            raise ValueError("the model went away")
        with self.assertRaises(ValueError):
            self.run_(work=work)
        self.assertEqual(tree(self.root, *self.top), snap)
        row = runs.ledger(self.root)[-1]
        self.assertEqual(row["decision"], "failed")
        self.assertIn("the model went away", row["error"])
        self.assertEqual(runs.journal(self.root, row["id"])["status"], "failed")
        runs.discard(self.root, row["id"], say=QUIET)
        self.assertEqual(runs.ledger(self.root)[-1]["decision"], "discarded")

    def test_only_errors_is_a_failed_run(self):
        def work(stage):
            with open(stage / "migrate-errors.jsonl", "a") as fh:
                fh.write(json.dumps({"note": "notes@x", "day": "2026-10-04", "error": "ConnectionError()"}) + "\n")
        rc, rid = self.run_(work=work)
        self.assertEqual(rc, 1)
        self.assertEqual(runs.ledger(self.root)[-1]["decision"], "failed")
        self.assertFalse((self.root / "migrate-errors.jsonl").exists())

    def test_discard_drops_a_held_run_and_its_hold(self):
        rc, rid = self.run_("hold")
        runs.discard(self.root, rid, say=QUIET)
        self.assertFalse((self.root / runs.RUNS / rid).exists())
        self.assertFalse((self.root / "import.hold").exists())
        self.assertEqual(self.run_()[0], 0)   # its units run again

    # 13
    def test_carried_files_land_only_on_promote(self):
        rel = ".sources/notes/manifest.json"

        def work(stage):
            runs.replay_work(ROWS, TEXTS)(stage)
            (stage / ".carry" / rel).parent.mkdir(parents=True)
            (stage / ".carry" / rel).write_text('{"a.md": "1"}')
        rc, rid = self.run_("hold", work=work)
        self.assertFalse((self.root / rel).exists())
        runs.promote(self.root, rid, by="hand", say=QUIET)
        self.assertEqual((self.root / rel).read_text(), '{"a.md": "1"}')
        self.assertEqual(runs.journal(self.root, rid)["carried"], [rel])
        runs.undo(self.root, rid, hold=False, say=QUIET)
        self.assertFalse((self.root / rel).exists())

    def test_a_crash_after_carrying_undoes_the_carried_files(self):
        rel = ".sources/notes/manifest.json"
        (self.root / rel).parent.mkdir(parents=True)
        (self.root / rel).write_text('{"a.md": "0"}')

        def work(stage):
            runs.replay_work(ROWS, TEXTS)(stage)
            (stage / ".carry" / rel).parent.mkdir(parents=True)
            (stage / ".carry" / rel).write_text('{"a.md": "1"}')
        with mock.patch.object(runs, "_changed", side_effect=RuntimeError("killed")):
            with self.assertRaises(RuntimeError):
                self.run_(work=work)
        rid = runs.promoting(self.root)[0]
        self.assertEqual((self.root / rel).read_text(), '{"a.md": "1"}')
        runs.undo(self.root, rid, hold=False, say=QUIET)
        self.assertEqual((self.root / rel).read_text(), '{"a.md": "0"}')
        self.assertEqual(self.attrs("Nimbus")["host"], ("box-7", ""))

    def test_status_runs_and_gc(self):
        self.assertEqual(runs.status(self.root, line=True), "deltos status: no runs yet")
        rc, rid = self.run_()
        line = runs.status(self.root, line=True)
        self.assertIn(f"import run {rid} promoted (5 facts)", line)
        self.assertIn(f"run {rid}: note (import), promoted", runs.show(self.root, rid))
        self.assertTrue((self.root / runs.RUNS / rid / "before").is_dir())
        self.assertEqual(runs.gc(self.root, keep=0), 1)
        self.assertFalse((self.root / runs.RUNS / rid / "before").exists())
        with self.assertRaises(runs.RunError):
            runs.undo(self.root, rid, say=QUIET)

    def test_texts_load_from_either_file(self):
        a, b = self.tmp / "texts.json", self.tmp / "migrate-texts.jsonl"
        a.write_text(json.dumps({"notes@a1#0": "one", "chat:default/s1#1": "two"}))
        b.write_text(json.dumps({"note": "chat:default/s1", "chunk": 1, "text": "two", "upto": 9}) + "\n")
        self.assertEqual(runs.load_texts(a), {("notes@a1", 0, None): "one", ("chat:default/s1", 1, None): "two"})
        self.assertEqual(runs.load_texts(b), {("chat:default/s1", 1, 9): "two"})

    # 12
    def test_drill_refuses_the_live_store(self):
        log = self.tmp / "log.jsonl"
        log.write_text("".join(json.dumps(r) + "\n" for r in ROWS))
        copy = self.tmp / "copy"
        shutil.copytree(self.root, copy)
        snap = tree(self.root, *self.top)
        with mock.patch.dict(os.environ, {"DELTOS_ROOT": str(self.root)}), \
                mock.patch.object(runs, "health", FakeHealth()), mock.patch("sys.stderr"), mock.patch("sys.stdout"):
            self.assertEqual(cli.main(["--root", str(self.root), "import", "--drill", str(log)]), 1)
            self.assertEqual(cli.main(["import", "--drill", str(log)]), 1)
            self.assertEqual(tree(self.root, *self.top), snap)
            self.assertEqual(cli.main(["--root", str(copy), "import", "--drill", str(log)]), 0)
        self.assertEqual(Store(copy).find("Nimbus").attrs["version"].value, "4.1")
        self.assertEqual(runs.ledger(copy)[-1]["decision"], "promoted")

    # 14
    @unittest.skipUnless(importlib.util.find_spec("deltos.ground"), "needs the grounding gate's text replay (W1)")
    def test_a_split_unit_replayed_with_its_text_gives_the_stage_notes(self):
        text = "--- notes/a.md\n" + "".join(f"- Nimbus listens on port {9000 + i} in rack {i}\n" for i in range(120))
        replies = [("Nimbus | port | 9000 | 2026-10-04\n" * 40, notes.MAX_TOKENS),
                   ("Nimbus | version | 4.1 | 2026-10-04\nNimbus | port | 9001 | 2026-10-04", 50),
                   ("Nimbus | rack | 7 | 2026-10-04", 50)]
        real = notes.chat
        self.addCleanup(setattr, notes, "chat", real)
        notes.chat = lambda messages, max_tokens=0, **kw: (({"content": replies[0][0]}, {"completion_tokens": replies.pop(0)[1]}))
        stage_notes = self.tmp / "stage-notes"

        def work(stage):
            notes.run(stage, [("2026-10-04", "2026-10-04", "notes@h1", 0, text, 1)], 1, say=QUIET)
            shutil.copytree(stage / "notes", stage_notes)
        rc, rid = self.run_(work=work)
        self.assertEqual(rc, 0)
        notes.chat = None
        live = {p.name: p.read_bytes() for p in (self.root / "notes").glob("*")}
        self.assertEqual(live, {p.name: p.read_bytes() for p in stage_notes.glob("*")})

    # 15: deltos import itself, with a note written well before its last edit
    def test_promote_dates_undated_facts_by_when_the_note_was_written(self):
        s = Store(self.root)
        s.name("Lumen", [], "app")
        s.remember("Lumen", "host", "box-7", since="2026-09-15", source="Ava")   # the agent's, mid-September
        (self.root / "deltos.toml").write_text('[import]\nrepos = ["notes-repo:HEAD:notes"]\nworkers = 1\n')
        # Written 09-01, last edited 10-04; the model gives both facts no date of their own.
        job = ("2026-09-01", "2026-10-04", "notes@n1", 0, "Lumen is blue and sits on box-3.", 1)
        real = notes.chat
        self.addCleanup(setattr, notes, "chat", real)
        notes.chat = lambda messages, max_tokens=0, **kw: (
            {"content": "Lumen | host | box-3 | -\nLumen | color | blue | -"},
            {"completion_tokens": 30, "finish_reason": "stop"})
        seen, run = {}, notes.run

        def staged(stage, *args, **kw):
            n = run(stage, *args, **kw)
            seen["stage"] = self.entries(Store(stage))
            seen["notes"] = {p.name: p.read_bytes() for p in (Path(stage) / "notes").glob("*")}
            return n
        with mock.patch.dict(os.environ), mock.patch.object(notes, "git_jobs", return_value=[job]), \
                mock.patch.object(notes, "run", side_effect=staged), mock.patch.object(llm, "configure"), \
                mock.patch.object(runs, "health", FakeHealth()), mock.patch("sys.stdout"):
            os.environ.pop("DELTOS_CONFIG", None)
            self.assertEqual(cli.main(["--root", str(self.root), "import"]), 0)
        notes.chat = None   # promote asks no model
        self.assertEqual(runs.ledger(self.root)[-1]["decision"], "promoted")
        self.assertEqual(seen["stage"]["Lumen"]["host"], ("box-7", "Ava", "2026-09-15"))
        self.assertEqual(seen["stage"]["Lumen"]["color"], ("blue", "", "2026-09-01"))
        # Live after promote dates them as the judged stage did, so the agent's value still stands.
        self.assertEqual(self.entries(Store(self.root)), seen["stage"])
        logged = json.loads(_lines(self.root / "migrate-log.jsonl")[-1])
        self.assertEqual((logged["note"], logged["day"], logged["created"]), ("notes@n1", "2026-10-04", "2026-09-01"))
        if importlib.util.find_spec("deltos.ground"):   # with W1 the receipts replay too: the same files
            self.assertEqual({p.name: p.read_bytes() for p in (self.root / "notes").glob("*")}, seen["notes"])

    def test_dated_leaves_rows_alone_when_written_is_the_day(self):
        log = self.tmp / "st" / "migrate-log.jsonl"
        log.parent.mkdir()
        old = json.dumps({"note": "notes@n1", "day": "2026-10-04", "chunk": 0}) + "\n"
        log.write_text(old)

        def work(stage):
            with open(stage / "migrate-log.jsonl", "a") as fh:
                for name, chunk in (("notes@n1", 1), ("notes@n2", 0), ("notes@n3", 0)):
                    fh.write(json.dumps({"note": name, "day": "2026-10-04", "chunk": chunk,
                                         **({"created": "2026-08-01"} if name == "notes@n3" else {})}) + "\n")
        runs.dated(work, [("2026-09-01", "2026-10-04", "notes@n1", 0, "", 1), ("2026-09-01", "2026-10-04", "notes@n1", 1, "", 1),
                          ("2026-10-04", "2026-10-04", "notes@n2", 0, "", 1),
                          ("2026-09-01", "2026-10-04", "notes@n3", 0, "", 1)])(log.parent)
        lines = _lines(log)
        self.assertEqual(lines[0], old)   # a row logged before the work is not rewritten
        self.assertEqual([json.loads(x).get("created") for x in lines[1:]], ["2026-09-01", None, "2026-08-01"])

    def entries(self, s):
        """Every profile's entries as (value, source, since)."""
        out = {}
        for p in sorted(s.notes.glob("*.md")):
            if not p.name.startswith("log-") and not p.stem.endswith(("-history", "-extra")):
                t = parse_thing(p.read_text())
                out[t.name] = {k: (a.value, a.source, a.since) for k, a in t.attrs.items()}
        return out


class GitlessRunsTest(RunsTest):
    """The same runs on a store with no git: promote and undo work from the journal alone."""
    GIT = False

    # 8
    def test_no_git_is_ever_called(self):
        snap = tree(self.root, *self.top)
        with mock.patch.object(runs.subprocess, "run", side_effect=AssertionError("git called")):
            rc, rid = self.run_()
            self.assertEqual(rc, 0)
            self.assertEqual(self.attrs("Nimbus")["version"], ("4.1", ""))
            runs.undo(self.root, rid, hold=False, say=QUIET)
        self.assertEqual(tree(self.root, *self.top), snap)
        self.assertFalse((self.root / ".git").exists())
        self.assertEqual(runs.ledger(self.root)[-2]["commit"], "")


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class NightlyTest(unittest.TestCase):
    """adapters/hermes/nightly.sh with a stand-in deltos: a machine's own steps (DELTOS_SITE) run
    in their own shell, so an unset variable or a failed step there never stops the import."""
    SCRIPT = Path(__file__).resolve().parents[1] / "adapters" / "hermes" / "nightly.sh"

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.tmp = Path(d.name)
        (self.tmp / "src" / "deltos").mkdir(parents=True)
        (self.tmp / "src" / "deltos" / "__init__.py").write_text("")
        (self.tmp / "src" / "deltos" / "__main__.py").write_text(
            "import sys\n"
            "root = sys.argv[sys.argv.index('--root') + 1]\n"
            "open(root + '/calls', 'a').write(' '.join(sys.argv[3:]) + '\\n')\n"
            "print('import: nothing to import' if sys.argv[3] == 'import' else 'stand-in ' + sys.argv[3])\n")
        (self.tmp / "mem").mkdir()

    def nightly(self, site: str) -> tuple[int, str, list[str]]:
        (self.tmp / "site.sh").write_text(site)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("DELTOS_", "PYTHON"))}
        env.update(HOME=str(self.tmp), DELTOS_SRC=str(self.tmp / "src"), DELTOS_ROOT=str(self.tmp / "mem"),
                   DELTOS_SITE=str(self.tmp / "site.sh"))
        p = subprocess.run(["bash", str(self.SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
        log = (self.tmp / "mem" / "nightly.log").read_text() if (self.tmp / "mem" / "nightly.log").exists() else ""
        calls = (self.tmp / "mem" / "calls").read_text().splitlines() if (self.tmp / "mem" / "calls").exists() else []
        return p.returncode, log, calls

    def test_an_unset_variable_in_the_site_steps_does_not_stop_the_import(self):
        # A checkpoint loop over a folder variable the site file never set (it was set in the old script).
        rc, log, calls = self.nightly('cd "$HOME" && touch site-ran\nfor d in "$NOTES_DIR"; do (cd "$d" || true); done\n')
        self.assertTrue((self.tmp / "site-ran").exists())
        self.assertEqual(rc, 0)
        self.assertIn("import ok", log)
        self.assertNotIn("site steps failed", log)
        self.assertEqual(calls, ["import", "learn --wait", "sessions --days 2", "status --line"])

    def test_a_failed_site_step_is_logged_and_the_job_goes_on(self):
        rc, log, calls = self.nightly("exit 5\n")
        self.assertEqual(rc, 0)
        self.assertIn("site steps failed rc=5", log)
        self.assertIn("import ok", log)
        self.assertEqual(calls, ["import", "learn --wait", "sessions --days 2", "status --line"])


if __name__ == "__main__":
    unittest.main()
