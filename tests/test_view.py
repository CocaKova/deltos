"""deltos/view.py: the store as data for a person (a pane, /deltos, `deltos view`),
and deltos/demo.py, the made-up store it is shown on. The view reads only, and its
"not confirmed" and as-of readings are recall's and the store's own, entry for entry."""
import contextlib
import datetime
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deltos import Memory
from deltos import demo
from deltos.cli import main as cli
from deltos.doctor import things_of
from deltos.store import Store, _as_of_line, _fact_line
from deltos.view import View, ago, command, entry, on

TODAY = "2026-11-10"
DOUBT = 21


def files(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


class DemoStore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.d.cleanup)
        cls.root = Path(cls.d.name) / "store"
        cls.receipts = demo.seed(cls.root, TODAY)

    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for k in ("DELTOS_ROOT", "DELTOS_CONFIG", "DELTOS_DOUBT_DAYS", "DELTOS_CHECKS"):
            os.environ.pop(k, None)
        self.v = View(self.root, TODAY, DOUBT)


class DemoTest(DemoStore):
    def test_every_write_was_taken(self):
        bad = [r for r in self.receipts if "refused" in r.lower() or "not " in r.lower()]
        self.assertEqual(bad, [])

    def test_the_store_reads_as_current_on_any_day(self):
        # dates are counted back from today, so the same store a year on has the same shape
        with tempfile.TemporaryDirectory() as d:
            demo.seed(d, "2027-12-01")
            o = View(d, "2027-12-01", DOUBT).overview()
        mine = self.v.overview()
        for k in ("things", "entries", "past", "unsure", "checked", "disputed", "changed", "due"):
            self.assertEqual(o[k], mine[k], k)

    def test_main_makes_a_store_once(self):
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(demo.main([d]), 0)
            self.assertIn("demo store", out.getvalue())
            before = files(Path(d))
            self.assertEqual(demo.main([d]), 1)   # never on top of a store
            self.assertEqual(files(Path(d)), before)
            self.assertEqual(demo.main([]), 2)

    def test_main_turns_on_what_the_demo_shows(self):
        from deltos import Memory
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(demo.main([d]), 0)
            m = Memory(d)
            self.assertEqual((m.doubt_days, m.checks), (21, True))
            self.assertIn("not confirmed in", m.context("Which host is Orchard on?"))
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
            (Path(d) / "deltos.toml").write_text("owner = \"Sam\"\n")   # settings already there stay
            self.assertEqual(demo.main([d]), 0)
            self.assertEqual((Path(d) / "deltos.toml").read_text(), "owner = \"Sam\"\n")

    def test_nothing_in_it_is_a_real_address(self):
        text = "\n".join(p.read_text() for p in (self.root / "notes").glob("*.md"))
        for ip in __import__("re").findall(r"\b\d+\.\d+\.\d+\.\d+\b", text):
            self.assertTrue(ip.startswith("203.0.113."), ip)   # the documentation range
        self.assertNotIn("@", text)


class ViewTest(DemoStore):
    def test_overview(self):
        o = self.v.overview()
        self.assertEqual({k: o[k] for k in ("things", "entries", "past", "unsure", "checked", "disputed",
                                            "changed", "changed_things", "due")},
                         {"things": 7, "entries": 23, "past": 16, "unsure": 8, "checked": 4, "disputed": 1,
                          "changed": 3, "changed_things": 3, "due": 8})
        self.assertEqual(o["looks"]["looks"], 4)
        self.assertEqual(o["looks"]["outcomes"].get("same"), 3)
        # the totals are the per-thing rows added up
        rows = self.v.things()
        self.assertEqual(sum(r["unsure"] for r in rows), o["unsure"])
        self.assertEqual(sum(len(self.v.thing(r["slug"])["items"]) for r in rows), o["entries"])

    def test_no_doubt_days_marks_nothing(self):
        v = View(self.root, TODAY, 0)
        o = v.overview()
        self.assertEqual((o["unsure"], o["checked"], o["disputed"]), (0, 4, 1))
        self.assertTrue(all(not e["note"] for r in v.things() for e in v.thing(r["slug"])["items"]))
        self.assertNotIn("not confirmed", command(v, "") + command(v, "orchard"))

    def test_not_confirmed_is_recalls_own_rule(self):
        """Every entry: the view says "not confirmed in N" exactly when the line an agent
        reads does, in the same words."""
        edge = tempfile.TemporaryDirectory()
        self.addCleanup(edge.cleanup)
        Store(edge.name, today="2026-01-01").name("Edge box", [], "machine")
        # labels and ages around the rule's edges, next to the demo's own
        for label, value, days in (("setting model", "small", 90), ("db host", "vps-9", 90), ("owner", "Sam", 90),
                                   ("status", "retired", 90), ("port", "1", DOUBT), ("url", "http://x", DOUBT - 1),
                                   ("latest release", "v2", 13), ("branch", "main", 59), ("ip", "203.0.113.9", 60)):
            day = (datetime.date.fromisoformat(TODAY) - datetime.timedelta(days=days)).isoformat()
            got = Store(edge.name, today=day).remember("Edge box", label, value, since=day, source="test")
            self.assertNotIn("not recorded", got)
        with mock.patch.dict(os.environ, {"DELTOS_DOUBT_DAYS": str(DOUBT)}):
            r = Memory(self.root, today=TODAY).recall()
        noted = []
        for t in things_of(self.root) + things_of(Path(edge.name)):
            for a in t.attrs.values():
                line = _fact_line(a)
                said = r.unconfirmed(line)
                e = entry(a, t.kind, TODAY, DOUBT)
                self.assertEqual(bool(e["note"]), said != line, (t.name, a.name))
                self.assertEqual(e["state"] == "unsure" or (bool(e["note"]) and e["state"] == "disputed"),
                                 said != line, (t.name, a.name))
                if e["note"]:
                    self.assertIn("; " + e["note"], said)
                    noted.append((t.name, a.name))
        self.assertEqual(sorted(lab for name, lab in noted if name == "Edge box"), ["Branch", "Db host", "Ip", "Port"])
        # the overview counts each entry once: one a note disagrees with is "disputed", though it
        # reads "not confirmed" too (the demo's Local model url)
        o = self.v.overview()
        self.assertEqual(len([1 for name, _ in noted if name != "Edge box"]), o["unsure"] + o["disputed"])

    def test_a_day_is_the_stores_own_reading(self):
        start = datetime.date.fromisoformat(TODAY) - datetime.timedelta(days=230)
        n = {"current": 0, "past": 0, "before": 0, "gap": 0}
        for t in things_of(self.root):
            for a in t.attrs.values():
                for back in range(0, 231):
                    day = (start + datetime.timedelta(days=back)).isoformat()
                    line, got = _as_of_line(a, day), on(a, day)
                    n[got["state"]] += 1
                    if got["state"] == "current" and a.value.lower() == "retired":
                        self.assertIn(f"retired / decommissioned since {got['start']}", line)
                    elif got["state"] == "current":
                        self.assertIn(f"{got['value']} (since {got['start']}, still current)", line)
                    elif got["state"] == "past":
                        self.assertIn(f"{got['value']} ({got['start']} to {got['end']})", line)
                    elif got["state"] == "before":
                        self.assertIn(f"nothing known on that day (first value from {got['start']})", line)
                    else:
                        self.assertIn("a gap between known values", line)
        self.assertTrue(all(n.values()), n)   # every kind of answer was compared

    def test_history_is_kept_with_its_dates(self):
        t = self.v.thing("Orchard")
        version = next(e for e in t["items"] if e["label"] == "Version")
        self.assertEqual(version["value"], "3.5.0")
        self.assertEqual(version["state"], "checked")
        self.assertEqual([p["value"] for p in version["past"]], ["3.4.1", "3.4.0", "3.3.0", "3.2.4"])
        self.assertEqual([p["wrong"] for p in version["past"]], [False, True, False, False])
        host = next(e for e in t["items"] if e["label"] == "Host")
        self.assertEqual((host["state"], host["note"]), ("unsure", "not confirmed in 2 months"))
        url = next(e for e in self.v.thing("Local model")["items"] if e["label"] == "Url")
        self.assertEqual(url["state"], "disputed")
        self.assertEqual(url["other"]["value"], "http://workstation:8001/v1")

    def test_finding_a_thing(self):
        for name in ("orchard", "Orchard", "the shop", "ORCHARD APP", "orch"):
            self.assertEqual(self.v.thing(name)["name"], "Orchard", name)
        self.assertIsNone(self.v.thing("vps"))          # two things: not a guess
        self.assertEqual(sorted(self.v.near("vps")), ["vps-1", "vps-2"])
        self.assertIsNone(self.v.thing(""))
        self.assertIsNone(self.v.thing("nothing like it"))
        self.assertEqual([r["name"] for r in self.v.things("machine")], ["vps-2", "vps-1"])

    def test_changes(self):
        rows = self.v.changes(7)
        self.assertEqual([(r["thing"], r["label"], r["value"], r["was"]) for r in rows],
                         [("Newsletter", "Send day", "Tuesday", "Thursday"),
                          ("vps-2", "Size", "8 vCPU, 16 GB", "4 vCPU, 8 GB"),
                          ("Orchard", "Version", "3.5.0", "3.4.1")])
        self.assertEqual(self.v.changes(0), [r for r in rows if r["day"] == TODAY])
        self.assertGreater(len(self.v.changes(3650)), len(rows))

    def test_a_day(self):
        got = {e["label"]: e for e in self.v.on("the shop", "2026-08-15")["items"]}
        self.assertEqual((got["Host"]["value"], got["Host"]["state"]), ("vps-1", "past"))
        self.assertEqual((got["Port"]["value"], got["Version"]["value"]), ("8080", "3.3.0"))
        self.assertEqual(got["Owner"]["state"], "current")
        self.assertEqual(got["Deploy rule"]["state"], "current")
        self.assertEqual({e["state"] for e in self.v.on("orchard", "2026-01-01")["items"]}, {"before"})
        self.assertIsNone(self.v.on("orchard", "last week"))
        self.assertIsNone(self.v.on("no such thing", "2026-08-15"))

    def test_it_only_reads(self):
        before = files(self.root)
        v = View(self.root, TODAY, DOUBT)
        v.overview(); v.things(); v.things("vps"); v.changes(30); v.due(); v.looks(); v.near("x")
        for r in v.things():
            v.thing(r["slug"]); v.on(r["slug"], "2026-08-15")
        for raw in ("", "orchard", "on 2026-08-15 orchard", "changes 30", "due", "help", "zzz"):
            command(v, raw)
        self.assertEqual(files(self.root), before)

    def test_a_folder_that_is_no_store_yet(self):
        with tempfile.TemporaryDirectory() as d:
            v = View(Path(d) / "nothing-here", TODAY, DOUBT)
            self.assertEqual(v.overview()["things"], 0)
            self.assertEqual((v.things(), v.changes(), v.due(), v.thing("x")), ([], [], [], None))
            self.assertIn("Memory is empty", command(v, ""))
            self.assertFalse((Path(d) / "nothing-here").exists())   # looking makes nothing

    def test_view_of_a_memory(self):
        with tempfile.TemporaryDirectory() as d:
            demo.seed(d, TODAY)
            (Path(d) / "deltos.toml").write_text("[recall]\ndoubt_days = 30\n")
            v = View.of(Memory(d, today=TODAY))
            self.assertEqual((str(v.root), v.today, v.doubt_days), (d, TODAY, 30))
            with mock.patch.dict(os.environ, {"DELTOS_DOUBT_DAYS": "0"}):
                self.assertEqual(View.of(Memory(d, today=TODAY)).doubt_days, 0)

    def test_ages_read_as_recall_writes_them(self):
        self.assertEqual([ago(n) for n in (1, 13, 14, 59, 60, 210)],
                         ["1 days", "13 days", "2 weeks", "8 weeks", "2 months", "7 months"])


class CommandTest(DemoStore):
    def test_each_shape(self):
        v = self.v
        self.assertIn("7 things, 23 entries, 16 past values kept", command(v))
        self.assertIn("8 not confirmed in 3 weeks or more", command(v, "  "))
        self.assertIn("memory was right 3 of 4 times", command(v))
        one = command(v, "the shop")
        self.assertIn("**Orchard** (web app)", one)
        self.assertIn("- **Host:** vps-2 (since 2026-09-01; not confirmed in 2 months)", one)
        self.assertIn("  - was vps-1 (2026-04-14 to 2026-09-01)", one)
        self.assertIn("  - recorded as 3.4.0 by mistake", one)
        self.assertIn("  - check: curl -s https://orchard.example/version", one)
        self.assertIn("a meeting note said http://workstation:8001/v1", command(v, "local model"))
        self.assertIn("- **Host:** vps-1 (2026-04-14 to 2026-09-01)", command(v, "on 2026-08-15 orchard"))
        self.assertTrue(command(v, "changes").startswith("Changed in the last 7 days:"))
        self.assertIn("Orchard · Price", command(v, "changes 30"))
        self.assertEqual(command(v, "changes 1").count("\n- "), 1)
        due = command(v, "due 3")
        self.assertEqual(due.count("\n- "), 3)
        self.assertLess(due.index("Local model · Url"), due.index("Orchard · Status"))   # a note disagrees: first
        self.assertEqual(command(v, "unsure 3"), due)
        self.assertIn("on <YYYY-MM-DD> <thing>", command(v, "help"))

    def test_what_it_does_not_know(self):
        self.assertEqual(command(self.v, "the moon"), "Memory has no thing called 'the moon'.")
        self.assertIn("Closest: vps-2, vps-1.", command(self.v, "vps"))
        self.assertIn("no thing called 'the moon'", command(self.v, "on 2026-08-15 the moon"))

    def test_a_thing_named_like_a_command_is_still_found(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(d, today=TODAY)
            for name in ("Changes log", "On call rota", "Due diligence", "Help desk"):
                s.name(name, [], "document")
                self.assertNotIn("not recorded", s.remember(name, "owner", "Sam", source="test"))
            v = View(d, TODAY)
            for raw, name in (("changes log", "Changes log"), ("on call rota", "On call rota"),
                              ("due diligence", "Due diligence"), ("help desk", "Help desk")):
                self.assertTrue(command(v, raw).startswith(f"**{name}**"), raw)
            self.assertTrue(command(v, "changes").startswith("Changed in"))
            self.assertIn("**On call rota** on 2026-11-10", command(v, "on 2026-11-10 on call rota"))

    def test_deltos_view_in_a_terminal(self):
        (self.root / "deltos.toml").write_text(f"[recall]\ndoubt_days = {DOUBT}\n")
        self.addCleanup((self.root / "deltos.toml").unlink)
        for argv, want in ((["view"], "7 things, 23 entries"), (["view", "the", "shop"], "**Orchard** (web app)"),
                           (["view", "changes", "30"], "Changed in the last 30 days:")):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli(["--root", str(self.root), *argv]), 0)
            self.assertIn(want, out.getvalue())

    def test_deltos_demo_makes_its_own_store_and_opens_no_other(self):
        from deltos import cli
        with tempfile.TemporaryDirectory() as d:
            other, made = Path(d) / "other", Path(d) / "made"
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(["--root", str(other), "demo", str(made)]), 0)
                self.assertEqual(cli.main(["--root", str(made), "view"]), 0)
            self.assertFalse(other.exists())
            self.assertIn("7 things, 23 entries", out.getvalue())
            self.assertIn("8 not confirmed in 3 weeks or more", out.getvalue())   # its own settings


if __name__ == "__main__":
    unittest.main()
