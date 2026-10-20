"""Gate suites as modules (bench/suites): the registry finds them, --quick leaves the
SLOW ones out, and the night suite grades keep / trivia / wrong labels on a replay
that comes out the same every time."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bench.gate as gate
import bench.suites as suites
import bench.suites.night as night
from deltos.store import Store

DAY = "2026-10-04"


class Dummies:
    """Suite modules written to a temp dir that joins bench.suites' search path."""

    def __init__(self, case: unittest.TestCase, **mods: str):
        d = tempfile.TemporaryDirectory()
        case.addCleanup(d.cleanup)
        for name, src in mods.items():
            (Path(d.name) / f"{name}.py").write_text(src)
        suites.__path__.append(d.name)
        case.addCleanup(suites.__path__.remove, d.name)
        for name in mods:
            case.addCleanup(sys.modules.pop, f"bench.suites.{name}", None)


FAST = 'NAME, SLOW = "fast", False\ndef run():\n    return {"q1": {"s": "ok", "c": 1}}\n'
SLOWER = 'NAME, SLOW = "slower", True\ndef run():\n    return {"q1": {"s": "ok", "c": 1}}\n'


class RegistryTest(unittest.TestCase):
    def test_finds_every_module_with_name_slow_and_run(self):
        Dummies(self, zz_fast=FAST, zz_half='NAME = "half"\ndef run():\n    return {}\n')
        found = suites.load()
        self.assertIn("fast", found)
        self.assertEqual(found["fast"](), {"q1": {"s": "ok", "c": 1}})
        self.assertNotIn("half", found)        # no SLOW: not a suite
        self.assertIs(found["night"], night.run)
        self.assertTrue(suites.slow(found["night"]))
        self.assertFalse(suites.slow(found["fast"]))

    def test_the_gate_holds_them(self):
        self.assertIs(gate.SUITES["night"], night.run)
        self.assertTrue(gate.quick_skips("night"))
        self.assertTrue(gate.quick_skips("import"))
        self.assertFalse(gate.quick_skips("lme"))


class QuickTest(unittest.TestCase):
    def ran(self, *argv) -> list[str]:
        Dummies(self, zz_fast=FAST, zz_slower=SLOWER)
        found = suites.load()
        calls = []
        track = lambda n: (lambda: calls.append(n) or found[n]())
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(d.name)
        fake = {"fast": track("fast"), "slower": track("slower"), "import": lambda: calls.append("import") or {}}
        fake["slower"].__module__ = found["slower"].__module__
        with mock.patch.dict(gate.SUITES, fake, clear=True), mock.patch.object(sys, "argv", ["gate", *argv]), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            gate.main()
        return calls

    def test_quick_skips_slow_suites(self):
        self.assertEqual(self.ran("--quick"), ["fast"])

    def test_full_runs_them(self):
        self.assertEqual(self.ran(), ["fast", "slower", "import"])


class LandedTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.s = Store(Path(d.name))
        self.s.name("Nimbus", ["nb"], "app")
        self.s.remember("Nimbus", "host", "box-7", since="2026-09-01")
        self.s.remember("Nimbus", "host", "box-9", since="2026-10-01")
        self.s.remember("Nimbus", "Setting build host", "box-2", since="2026-10-01")

    def test_status_table(self):
        want = {("K", True): "ok", ("K", False): "miss", ("T", True): "conflict", ("T", False): "ok",
                ("W", True): "stale-current", ("W", False): "ok"}
        self.assertEqual(night.STATUS, want)
        for v in "KTW":
            for landed in (True, False):
                self.assertIn(night.STATUS[v, landed], gate.RANK)

    def test_landed_is_the_current_value_or_another_current_item(self):
        from deltos.store import Also
        t = self.s.find("Nimbus")
        t.attrs["host"].also = [Also("box-3", DAY)]
        self.s.save(t)
        s = Store(self.s.root)
        self.assertTrue(night.landed(s, "Nimbus | host | box-9"))
        self.assertTrue(night.landed(s, "nb | Host | BOX-9 "))           # alias, label and value folded
        self.assertTrue(night.landed(s, "Nimbus | host | box-3"))         # also current
        self.assertTrue(night.landed(s, "Nimbus | build host | box-2"))   # a setting, named as the store names it
        self.assertTrue(night.landed(s, "Nimbus | Setting build host | box-2"))
        self.assertFalse(night.landed(s, "Nimbus | host | box-7"))        # history, not current
        self.assertFalse(night.landed(s, "Nimbus | owner | box-9"))       # no such entry
        self.assertFalse(night.landed(s, "Orion | host | box-9"))         # no such thing
        self.assertFalse(night.landed(s, "Nimbus | host | box"))          # the whole value, not a part

    def test_a_names_line_landed_when_each_name_finds_the_thing(self):
        self.assertTrue(night.landed(self.s, "Nimbus | Nimbus; nb | service"))
        self.assertFalse(night.landed(self.s, "Nimbus | nb; nimbus-2 | service"))  # one name not registered


class ReplayTest(unittest.TestCase):
    """A tiny frozen night: a base store, one run of two rows (one with its text), four labels."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.d = Path(d.name) / "night"
        s = Store(self.d / "base")
        s.name("Nimbus", ["nb"], "app")
        s.remember("Nimbus", "host", "box-1", since="2026-09-01")
        (self.d / "base" / "deltos.toml").write_text('owner = "Sam"\n')
        (self.d / "base" / "items.json").write_text("{}")
        (self.d / "runs").mkdir()
        (self.d / "texts").mkdir()
        rows = [{"note": "memory@a", "day": DAY, "chunk": 0,
                 "raw": "Nimbus | host | box-7 | 2026-10-04\nNimbus | owner | Dana | -"},
                {"note": "memory@b", "day": DAY, "chunk": 0, "raw": "Vega | kind | service | -\nVega | port | 9100 | -"}]
        (self.d / "runs" / "r1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        (self.d / "texts" / "r1.json").write_text(json.dumps({"memory@a#0": "--- memory/a.md\n- Nimbus is on box-7 now\n"}))
        self.labels = [{"line": "Vega | port | 9100", "v": "K", "src": "t"},
                       {"line": "Orion | owner | Sam", "v": "K", "src": "t"},
                       {"line": "Nimbus | host | box-7", "v": "T", "src": "t"},
                       {"line": "Nimbus | owner | Dana", "v": "W", "src": "t"}]   # not in its text: never lands
        (self.d / "labels.jsonl").write_text("".join(json.dumps(x) + "\n" for x in self.labels))
        p = mock.patch.object(night, "D", self.d)
        p.start()
        self.addCleanup(p.stop)

    def test_replays_the_same_twice(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            a, b = night.run(), night.run()
        self.assertEqual(a, b)
        q = {x["line"]: night.qid("r1", x["line"]) for x in self.labels}
        self.assertEqual({line: a[k]["s"] for line, k in q.items()},
                         {"Vega | port | 9100": "ok", "Orion | owner | Sam": "miss",
                          "Nimbus | host | box-7": "conflict", "Nimbus | owner | Dana": "ok"})
        self.assertEqual({v["c"] for v in a.values()}, {0})
        self.assertTrue(all(k.startswith("night:r1:") and len(k) == len("night:r1:") + 10 for k in a))
        self.assertIn("r1: landed keep 1/2, trivia 1/1, wrong 0/1", out.getvalue())
        self.assertIn("new things 1", out.getvalue())
        self.assertFalse((self.d / "base" / "notes" / "vega.md").exists())   # the base is never written

    def test_no_frozen_night_is_skipped(self):
        shutil.rmtree(self.d / "base")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(night.run(), {})
        self.assertIn("night: skipped", out.getvalue())


class AssembleTest(unittest.TestCase):
    def test_sittings_merge_and_a_line_labelled_two_ways_is_left_out(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = Path(d.name)
        (p / "blind-key3.json").write_text(json.dumps([["a", "X | l | 1"], ["a", "Y | l | 2"], ["b", "Z | l | 3"]]))
        (p / "lab3.py").write_text("K = {1}\nW = {3}\n")
        (p / "blind-key4.json").write_text(json.dumps([["n", "X | l | 1"], ["n", "Y | l | 2"]]))
        (p / "lab4.py").write_text("K = {1, 2}\nW = set()\n")
        (p / "base").mkdir()
        (p / "base" / "entries.jsonl").write_text(json.dumps({"thing": "Q", "label": "l", "value": "4"}) + "\n")
        (p / "labels.py").write_text('L = "W"\n')
        rows, split = night.assemble(p)
        self.assertEqual(split, ["Y | l | 2"])
        self.assertEqual(rows, [{"line": "X | l | 1", "v": "K", "src": "blind3:a+blind4:n"},
                                {"line": "Z | l | 3", "v": "W", "src": "blind3:b"},
                                {"line": "Q | l | 4", "v": "W", "src": "seed19"}])


if __name__ == "__main__":
    unittest.main()
