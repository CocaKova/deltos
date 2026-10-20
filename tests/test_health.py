"""Run health: a run is judged against the store's own accepted pieces of the same
kind, never against a constant fit to one model. Synthetic rows only."""
import importlib.machinery
import json
import os
import io
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest import mock

from deltos import health
from deltos.store import Store


def row(lines=10, loop=0, capped=False, lost=0, facts=None, refused=1, kind=None, subs=None, recovered=False):
    """A logged piece that said `lines` distinct lines."""
    r = {"note": "notes@x", "day": "2026-10-19", "raw": "\n".join(f"Thing{i} | port | {i} | -" for i in range(lines)),
         "facts": lines - 1 if facts is None else facts, "refused": refused, "capped": capped}
    if loop:
        r["loop"] = loop
    if recovered:
        r["recovered"] = True
    if lost:
        r["lost"] = lost
    if kind:
        r["kind"] = kind
    if subs:
        r["subs"] = subs
    return r


def normal(n=60, loops=1, cuts=3):
    """A store's history: pieces of 8-14 lines, a loop or a cut-off now and then."""
    return [health.signals(row(lines=8 + i % 7, loop=12 if i < loops else 0, capped=loops <= i < loops + cuts))
            for i in range(n)]


class AssessTest(unittest.TestCase):
    def test_a_run_like_its_history_promotes(self):
        run = [row(lines=8 + i % 7, loop=12 if i == 3 else 0, capped=i == 5) for i in range(20)]
        v = health.assess(run, normal())
        self.assertEqual((v.decision, v.reasons), ("promote", []))
        self.assertEqual((v.metrics["pieces"], v.metrics["history"], v.metrics["loop"]), (20, 60, 1))

    def test_ten_of_twenty_loops_against_one_in_sixty_holds(self):
        run = [row(lines=8 + i % 7, loop=12 if i < 10 else 0) for i in range(20)]
        v = health.assess(run, normal(loops=1, cuts=0))
        self.assertEqual(v.decision, "hold")
        self.assertTrue(v.reasons[0].startswith("loops in 10 of 20 pieces (normal 1 of 60, p="), v.reasons)
        self.assertLess(v.metrics["loop_p"], health.ALPHA)

    def test_one_loop_in_a_one_piece_run_promotes(self):
        v = health.assess([row(loop=12)], normal(loops=0, cuts=0))
        self.assertEqual(v.decision, "promote")   # fewer than MIN_EVENTS, however unusual
        self.assertEqual(v.metrics["loop"], 1)

    def test_three_times_the_output_holds_with_five_pieces_and_is_not_tested_with_two(self):
        five = health.assess([row(lines=33 + i) for i in range(5)], normal(loops=0, cuts=0))
        self.assertEqual(five.decision, "hold")
        self.assertEqual(five.reasons, ["output 35 lines per piece vs normal 11"])
        two = health.assess([row(lines=33 + i) for i in range(2)], normal(loops=0, cuts=0))
        self.assertEqual(two.decision, "promote")
        self.assertNotIn("volume_p", two.metrics)

    def test_more_output_that_is_not_material_promotes(self):
        v = health.assess([row(lines=15) for _ in range(20)], normal(loops=0, cuts=0))
        self.assertLess(v.metrics["volume_p"], health.ALPHA)   # clearly more, but 15 vs 11 is < MATERIAL
        self.assertEqual(v.decision, "promote")

    def test_a_recovered_loop_is_not_counted(self):
        run = [row(loop=12, recovered=True) for _ in range(10)] + [row() for _ in range(10)]
        v = health.assess(run, normal(loops=0, cuts=0))
        self.assertEqual((v.decision, v.metrics["loop"], v.metrics["recovered_loops"]), ("promote", 0, 10))

    def test_a_loop_in_a_half_counts_for_its_piece(self):
        s = health.signals(row(capped=True, subs=[row(lines=4, loop=12), row(lines=3)]))
        self.assertTrue(s["loop"])
        self.assertEqual(s["out_lines"], 10 + 4 + 3)

    def test_an_empty_run_is_empty(self):
        v = health.assess([], normal(), errors=[{"note": "n", "error": "down"}])
        self.assertEqual((v.decision, v.metrics["errors"]), ("empty", 1))

    def test_no_history_and_three_of_three_cut_offs_promotes(self):
        self.assertAlmostEqual(health.binom_sf(3, 3, 0.5), 0.125, places=12)
        v = health.assess([row(capped=True) for _ in range(3)], [])
        self.assertEqual(v.decision, "promote")
        self.assertEqual((v.metrics["cut"], v.metrics["history"]), (3, 0))
        self.assertNotIn("volume_p", v.metrics)

    def test_no_history_is_never_held_however_many_pieces_fail(self):
        # 10-19 review: an even prior held a first run at 7 of 7 cut off (p=0.008) even
        # when the halves kept every fact, and its hold file would stop every run after it
        seven = health.assess([row(capped=True, subs=[row(lines=5), row(lines=5)]) for _ in range(7)], [])
        self.assertEqual((seven.decision, seven.reasons, seven.metrics["cut"]), ("promote", [], 7))
        self.assertLess(seven.metrics["cut_p"], health.ALPHA)   # still reported, with its p
        worst = health.assess([row(lines=80, loop=12, capped=True, lost=3, facts=0) for _ in range(20)], [])
        self.assertEqual(worst.decision, "promote")
        with mock.patch.dict(health.HOLD_RULES, {"any": lambda m, h: "always"}):
            self.assertEqual(health.assess([row()], []).decision, "promote")
            self.assertEqual(health.assess([row()], normal()).reasons, ["always"])
        text = health.render(seven, "r0", "learn")
        self.assertEqual(text.splitlines()[0], "ok learn run r0: no accepted history of this kind yet, "
                                               "so nothing is judged")
        self.assertIn("(not tested: too little history)", text)
        # once it is history, the same habit is the normal: a second run like it promotes
        again = health.assess([row(capped=True) for _ in range(7)], [health.signals(r) for r in
                                                                    [row(capped=True) for _ in range(7)]])
        self.assertEqual(again.decision, "promote")

    def test_a_run_that_stores_nothing_holds(self):
        # 10-19 review: a model whose every line the store refuses, or that answers in
        # prose, neither loops nor runs long; promoted, its notes are done for good
        refused = health.assess([row(lines=10, facts=0, refused=10) for _ in range(20)], normal(loops=0, cuts=0))
        self.assertEqual(refused.decision, "hold")
        self.assertEqual(refused.reasons, ["stored 0 facts per piece vs normal 10"])
        self.assertEqual((refused.metrics["refused_share"], refused.metrics["facts_per_piece"]), (1.0, 0))
        prose = [dict(row(facts=0, refused=0), raw="I read the note.\nIt describes a setup.") for _ in range(20)]
        v = health.assess(prose, normal(loops=0, cuts=0))
        self.assertEqual(v.decision, "hold")
        self.assertEqual(v.reasons, ["stored 0 facts per piece vs normal 10"])   # and 2 lines vs 11: fewer, not judged
        text = health.render(v, "r9", "import")
        self.assertTrue(text.startswith("held import run r9: stored 0 facts per piece vs normal 10\n"))
        self.assertIn("facts stored     0 per piece (median), normal 10   p=", text)

    def test_fewer_facts_that_is_not_material_or_too_few_pieces_promotes(self):
        fewer = health.assess([row(facts=7) for _ in range(20)], normal(loops=0, cuts=0))
        self.assertLess(fewer.metrics["yield_p"], health.ALPHA)   # clearly fewer, but 7 vs 10 is < MATERIAL
        self.assertEqual(fewer.decision, "promote")
        two = health.assess([row(facts=0) for _ in range(2)], normal(loops=0, cuts=0))
        self.assertEqual(two.decision, "promote")
        self.assertNotIn("yield_p", two.metrics)

    def test_a_normal_that_stores_nothing_never_holds_on_yield(self):
        # a kind whose pieces usually store nothing (short chats): 0 is its normal
        hist = [health.signals(row(lines=3, facts=0 if i % 3 else 2, kind="chat")) for i in range(60)]
        v = health.assess([row(lines=3, facts=0, kind="chat") for _ in range(20)], hist)
        self.assertEqual((v.decision, v.metrics["facts_per_piece_normal"]), ("promote", 0))

    def test_hold_rules_add_reasons(self):
        with mock.patch.dict(health.HOLD_RULES, {"facts": lambda m, h: "no facts" if not m["facts"] else None}):
            self.assertEqual(health.assess([row(facts=0)], normal()).reasons, ["no facts"])
            self.assertEqual(health.assess([row()], normal()).decision, "promote")

    def test_refused_share_is_reported_never_judged(self):
        v = health.assess([row(facts=10, refused=200) for _ in range(20)], normal(loops=0, cuts=0))
        self.assertEqual(v.decision, "promote")
        self.assertEqual(v.metrics["refused_share"], round(200 / 210, 3))
        self.assertIn("refused_share_normal", v.metrics)


class SignalsTest(unittest.TestCase):
    def test_counts_the_import_adds_up_are_not_counted_twice(self):
        # notes.SUMMED: a split piece's facts and refused already hold its halves'
        r = row(facts=5, refused=2, subs=[{"facts": 2, "refused": 1, "ungrounded": 1}, {"facts": 1, "ungrounded": 2}])
        r["ungrounded"] = 3
        s = health.signals(r)
        from deltos.sources.notes import SUMMED
        self.assertEqual((s["facts"], s["refused"]), (5, 2))
        self.assertEqual(s["ungrounded"], 3 if "ungrounded" in SUMMED else 6)
        self.assertAlmostEqual(s["refused_share"], 2 / 7)

    def test_cut_and_lost(self):
        s = health.signals(row(capped=True, lost=1))
        self.assertEqual((s["cut"], s["lost"], s["loop"]), (True, True, False))


class StatsTest(unittest.TestCase):
    def test_binom_sf(self):
        self.assertAlmostEqual(health.binom_sf(2, 21, 0.06), 0.3618, places=4)
        self.assertEqual(health.binom_sf(0, 5, 0.1), 1.0)
        self.assertEqual(health.binom_sf(6, 5, 0.1), 0.0)
        self.assertLess(health.binom_sf(1500, 3000, 0.1), 1e-100)   # no overflow on thousands of lines

    def test_mwu_p(self):
        self.assertLess(health.mwu_p([30, 31, 32, 33, 34], list(range(8, 20)) * 5), 0.001)
        self.assertGreater(health.mwu_p(list(range(8, 20)), list(range(8, 20)) * 5), 0.3)
        self.assertEqual(health.mwu_p([5, 5], [5, 5, 5]), 1.0)   # all tied
        self.assertEqual(health.mwu_p([], [1, 2]), 1.0)


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.root = Path(self.d.name)

    def test_history_keeps_one_kind_and_the_last_window(self):
        rows = [row(lines=3, kind="chat") if i % 2 else row(lines=20 + i % 5) for i in range(200)]
        rows.insert(50, {"note": "n", "day": "d", "error": "timeout"})
        text = "\n".join(json.dumps(r) for r in rows) + '\n{"note": "torn'
        (self.root / "migrate-log.jsonl").write_text(text)
        chats, notes_ = health.history(self.root, "chat"), health.history(self.root, "note")
        self.assertEqual((len(chats), len(notes_)), (health.WINDOW, health.WINDOW))
        self.assertEqual({s["out_lines"] for s in chats}, {3})
        self.assertEqual({s["out_lines"] for s in notes_}, {20, 21, 22, 23, 24})
        self.assertEqual(health.history(self.root / "nowhere", "note"), [])

    def test_kind_defaults_to_note(self):
        self.assertEqual((health.kind({}), health.kind({"kind": "chat"})), ("note", "chat"))


class StoreDeltaTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.base, self.after = Path(self.d.name, "base"), Path(self.d.name, "after")
        s = Store(self.base, today="2026-10-19")
        s.remember("Nimbus", "port", "8890", create=True)
        s.remember("Quill", "host", "box-7", create=True)
        shutil.copytree(self.base / "notes", self.after / "notes")   # a staged copy keeps mtimes
        a = Store(self.after, today="2026-10-19")
        a.remember("Nimbus", "host", "box-9")                         # changed
        a.remember("Lumen", "port", "9100", create=True)              # new, one entry: thin
        a.name("Ferro", [], "service")
        a.remember("Ferro", "kind", "service")
        a.remember("Ferro", "port", "9200")
        a.remember("Ferro", "host", "box-2")                          # new, two non-kind entries
        (self.after / "notes" / "log-2026-10-19.md").write_text("a daily log\n")
        (self.after / "notes" / "quill-history.md").write_text("- host: box-1 (until 2026-10-01)\n")
        q = self.after / "notes" / "quill.md"                         # touched, same bytes: unchanged
        os.utime(q, ns=(q.stat().st_atime_ns, q.stat().st_mtime_ns + 10 ** 9))

    def test_two_tiny_stores(self):
        with mock.patch.object(health, "_lint_total", return_value=None):
            d = health.store_delta(self.base, self.after)
        self.assertEqual(d, {"new_things": 2, "new_thin": 1, "changed_profiles": 1})

    def test_lint_delta_only_from_a_lint_that_answers_in_shape(self):
        fake = types.ModuleType("deltos.lint")
        fake.__spec__ = importlib.machinery.ModuleSpec("deltos.lint", None)
        fake.lint = lambda root: {"total": 7 if Path(root).name == "after" else 5, "counts": {}}
        with mock.patch.dict(sys.modules, {"deltos.lint": fake}):
            self.assertEqual(health.store_delta(self.base, self.after)["lint_delta"], 2)
            fake.lint = lambda root: [("x", "x", [])]
            self.assertNotIn("lint_delta", health.store_delta(self.base, self.after))


class RenderTest(unittest.TestCase):
    def test_first_line_and_commands(self):
        run = [row(lines=40, loop=12 if i < 10 else 0) for i in range(20)]
        v = health.assess(run, normal(loops=1, cuts=0), delta={"new_things": 4, "new_thin": 1, "changed_profiles": 9})
        text = health.render(v, "20261019-050000-import", "import")
        lines = text.splitlines()
        self.assertEqual(lines[0], "held import run 20261019-050000-import: " + "; ".join(v.reasons))
        self.assertIn("output 40 lines per piece vs normal 11", lines[0])
        self.assertIn("deltos run promote 20261019-050000-import", text)
        self.assertIn("deltos run discard 20261019-050000-import", text)
        self.assertIn("new things 4", text)

    def test_a_healthy_run_and_an_empty_one(self):
        ok = health.render(health.assess([row()], normal()), "r1", "learn")
        self.assertTrue(ok.startswith("ok learn run r1: "))
        self.assertNotIn("run promote", ok)
        self.assertTrue(health.render(health.assess([], []), "r2", "import").startswith("empty import run r2"))


class FingerprintTest(unittest.TestCase):
    def test_canonical(self):
        a = health.fingerprint({"url": "http://h:1/v1", "model": "m", "temperature": 0.6, "prompt": "ab12"})
        b = health.fingerprint({"prompt": "ab12", "temperature": 0.6, "model": "m", "url": "http://h:1/v1"})
        self.assertEqual(a, b)
        self.assertEqual(len(a), 12)
        self.assertNotEqual(a, health.fingerprint({"url": "http://h:1/v1", "model": "m", "temperature": 0}))


class HealthcheckTest(unittest.TestCase):
    """bench/healthcheck.py replays a store's own git history: each commit that added
    rows is judged against the log at its parent; an undo takes its night out."""

    def test_a_bad_night_holds_and_the_night_after_its_undo_is_judged_without_it(self):
        from bench import healthcheck
        with tempfile.TemporaryDirectory() as d:
            root, log = Path(d), Path(d, "migrate-log.jsonl")
            git = lambda *a: subprocess.run(["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                            check=True, capture_output=True)
            git("init", "-q")

            def commit(rows, msg, keep=None):
                old = log.read_text().splitlines() if log.exists() else []
                lines = (old if keep is None else old[:keep]) + [json.dumps(r) for r in rows]
                log.write_text("".join(ln + "\n" for ln in lines))
                git("add", "-A")
                git("commit", "-q", "-m", msg)
            commit([row(lines=8 + i % 7) for i in range(60)], "seed")
            commit([row(lines=40, loop=12) for _ in range(8)], "bad night")
            commit([], "undo", keep=60)
            commit([row(lines=8 + i % 7) for i in range(5)] + [row(lines=3, kind="chat")], "good night")
            out = io.StringIO()
            with redirect_stdout(out):
                healthcheck.main([str(root)])
            normal_ = healthcheck.history_from(healthcheck.log_at(root, "HEAD^"), "note")   # at the undo
            self.assertEqual((len(normal_), sum(x["loop"] for x in normal_)), (60, 0))
        rows = [ln.split() for ln in out.getvalue().splitlines()[2:] if not ln.startswith(" ")]
        self.assertEqual([(r[1], r[2], r[3], r[4]) for r in rows],
                         [("note", "60", "0", "PROMOTE"), ("note", "8", "60", "HOLD"),
                          ("chat", "1", "0", "PROMOTE"), ("note", "5", "60", "PROMOTE")])
        self.assertIn("hold: loops in 8 of 8 pieces", out.getvalue())


if __name__ == "__main__":
    unittest.main()
