"""Checks (deltos/checks.py): a writer who saw a value says how; the entry keeps how
to look, checks.jsonl logs each look and what it found, the due list ranks what
should be seen again, and with [checks] on a "check:" line shows under a "not
confirmed" line. Off by default: no check parameter, no memory_checks tool, no
check lines in blocks; the store keeps them either way."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deltos import Memory
from deltos.checks import due, render_due, render_stats, stats
from deltos.config import Config
from deltos.memory import TOOLS, tools_for
from deltos.recall import Recall
from deltos.store import Store, check_problem


class ChecksTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        s = Store(self.root, today="2026-08-01")
        s.name("Gateway", [], "service")
        s.remember("Gateway", "port", "8787", since="2026-08-01")
        s.remember("Gateway", "version", "1.2.0", since="2026-08-01")
        s.name("Notes app", [], "app")
        s.remember("Notes app", "host", "Box-7", since="2026-09-20")
        s.remember("Notes app", "owner", "Sam", since="2026-08-01")
        s.name("Old box", [], "machine")
        s.remember("Old box", "status", "retired", since="2026-08-01")
        s.remember("Old box", "version", "0.9", since="2026-08-01")

    def store(self, day="2026-10-26"):
        return Store(self.root, today=day)

    def log(self):
        f = self.root / "checks.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    # -- the store -----------------------------------------------------------------

    def test_same_value_confirms_and_keeps_how(self):
        out = self.store().remember("Gateway", "port", "8787", source="Ava", check="ss -ltnp on box-7")
        self.assertIn("confirmed 2026-10-26", out)
        self.assertIn("checked (same)", out)
        text = (self.root / "notes" / "gateway.md").read_text()
        self.assertIn("- **Port:** 8787 (since 2026-08-01; confirmed 2026-10-26)", text)
        self.assertIn("  - check: ss -ltnp on box-7", text)
        self.assertEqual([(r["outcome"], r["before"], r["seen"]) for r in self.log()], [("same", "8787", "8787")])

    def test_changed_value_replaces_and_how_survives_the_change(self):
        s = self.store()
        s.remember("Gateway", "version", "1.3.1", source="Ava", check="gateway --version")
        t = self.store().find("Gateway")
        a = t.attrs["version"]
        self.assertEqual((a.value, a.since, a.check), ("1.3.1", "2026-10-26", "gateway --version"))
        self.assertEqual(a.trail[0].value, "1.2.0")
        # a later plain write (no look) changes the value; how to look stays
        self.store("2026-10-27").remember("Gateway", "version", "1.4.0", source="Ava")
        self.assertEqual(self.store().find("Gateway").attrs["version"].check, "gateway --version")
        self.assertEqual([r["outcome"] for r in self.log()], ["changed"])

    def test_new_entry_and_import_never_checks(self):
        s = self.store()
        s.remember("Gateway", "url", "http://box-7:8787", source="Ava", check="curl -sI the url")
        self.assertEqual(self.log()[-1]["outcome"], "new")
        n = len(self.log())
        s.remember("Gateway", "port", "8787", confirm=False, check="a note can't look")
        self.assertEqual(len(self.log()), n)
        self.assertNotIn("a note can't look", (self.root / "notes" / "gateway.md").read_text())

    def test_a_look_confirms_today_whatever_day_the_value_started(self):
        s = self.store()
        # new entry, known to have held since an earlier day
        s.remember("Gateway", "status", "stopped", source="Ava", since="2026-09-11", check="systemctl is-active gw")
        # another value, backdated after the current one's start
        s.remember("Gateway", "version", "1.3.0", source="Ava", since="2026-09-01", check="gateway --version")
        # the same value, known from before its recorded start (backdates the start)
        s.remember("Gateway", "port", "8787", source="Ava", since="2026-07-01", check="ss -ltnp")
        text = (self.root / "notes" / "gateway.md").read_text()
        self.assertIn("- **Status:** stopped (since 2026-09-11; confirmed 2026-10-26; source: Ava)", text)
        self.assertIn("- **Version:** 1.3.0 (since 2026-09-01; confirmed 2026-10-26; source: Ava)", text)
        self.assertIn("- **Port:** 8787 (since 2026-07-01; confirmed 2026-10-26)", text)
        self.assertEqual([r["outcome"] for r in self.log()], ["new", "changed", "same"])
        got = [(r["thing"], r["attribute"]) for r in due(self.root, "2026-10-26")]
        for label in ("Status", "Version", "Port"):
            self.assertNotIn(("Gateway", label), got)
        r = Recall(self.root)
        r.today, r.doubt_days = "2026-10-26", 21
        self.assertNotIn("not confirmed", r.context("Tell me about Gateway")[0])
        # no look, no sighting: a plain backdated write stays dated as given
        s.remember("Notes app", "version", "2.0", source="Ava", since="2026-09-01")
        self.assertEqual(self.store().find("Notes app").attrs["version"].confirmed, "")

    def test_held_value_logs_held(self):
        s = self.store()
        s.remember("Gateway", "port", "9000", source="hand")          # a person set it today
        out = s.remember("Gateway", "port", "8787", source="Ava", since="2026-10-20", check="ss -ltnp")
        self.assertIn("current stays 9000", out)
        self.assertEqual(self.log()[-1]["outcome"], "held")
        out = s.remember("Gateway", "port", "8787", source="Ava", check="ss -ltnp")   # a person set it today
        self.assertTrue(out.startswith("not changed"), out)
        self.assertEqual(self.log()[-1]["outcome"], "held")

    def test_refused_how_keeps_the_fact(self):
        out = self.store().remember("Gateway", "port", "8790", source="Ava",
                                    check="curl -H 'Authorization: Bearer sk-abcdef1234567890' http://box-7")
        self.assertIn("8790", out)
        self.assertIn("how you checked not kept", out)
        self.assertEqual(self.store().find("Gateway").attrs["port"].value, "8790")
        self.assertEqual(self.store().find("Gateway").attrs["port"].check, "")
        self.assertEqual(self.log(), [])

    def test_check_problem(self):
        self.assertEqual(check_problem("hermes --version"), "")
        self.assertEqual(check_problem("config.yaml model.default on box-7"), "")
        self.assertEqual(check_problem("gh release list -R owner/repo"), "")
        self.assertTrue(check_problem("token=abcdef123456"))
        self.assertTrue(check_problem("x" * 3 + " " + "a1" * 20))
        self.assertTrue(check_problem("one\ntwo"))
        self.assertTrue(check_problem(" ".join(["word"] * 30)))

    def test_round_trip_and_merge(self):
        s = self.store()
        s.remember("Gateway", "port", "8787", source="Ava", check="ss -ltnp")
        before = (self.root / "notes" / "gateway.md").read_text()
        t = s.find("Gateway")
        s.save(t)
        self.assertEqual((self.root / "notes" / "gateway.md").read_text(), before)
        s.name("Gateway two", [], "service")
        s.remember("Gateway two", "port", "8787", since="2026-08-01")
        s.merge("Gateway", "Gateway two")
        self.assertEqual(self.store().find("Gateway").attrs["port"].check, "ss -ltnp")

    # -- the due list --------------------------------------------------------------

    def test_due_ranks_old_and_asked_and_skips_retired_and_fresh(self):
        reads = self.root / "reads"
        reads.mkdir()
        (reads / "2026-10.jsonl").write_text("".join(
            json.dumps({"at": f"2026-10-{d:02d}T10:00:00", "things": ["notes-app"]}) + "\n" for d in (20, 21, 22)))
        rows = due(self.root, "2026-10-26")
        got = [(r["thing"], r["attribute"]) for r in rows]
        self.assertIn(("Gateway", "Port"), got)
        self.assertIn(("Gateway", "Version"), got)
        self.assertIn(("Notes app", "Host"), got)               # 36 days, asked 3x -> first
        self.assertEqual(got[0], ("Notes app", "Host"))
        self.assertNotIn(("Notes app", "Owner"), got)           # owner doesn't drift
        self.assertFalse([g for g in got if g[0] == "Old box"])  # retired thing
        # seen today: not due
        self.store().remember("Gateway", "port", "8787", source="Ava", check="ss -ltnp")
        got = [(r["thing"], r["attribute"]) for r in due(self.root, "2026-10-26")]
        self.assertNotIn(("Gateway", "Port"), got)
        text = render_due(due(self.root, "2026-10-26"))
        self.assertIn("Notes app · Host: Box-7 · last seen 2026-09-20 (36 days), asked 3x in 30 days", text)
        self.assertIn("no known way to check yet", text)

    def test_a_setting_with_a_way_to_check_is_due(self):
        s = Store(self.root, today="2026-08-01")
        s.remember("Gateway", "setting context_length", "182000", since="2026-08-01")
        self.assertNotIn("Setting context_length", [r["attribute"] for r in due(self.root, "2026-10-26")])
        Store(self.root, today="2026-09-01").remember("Gateway", "setting context_length", "262144", source="Ava",
                                                       check="config.yaml model.context_length")
        rows = {r["attribute"]: r for r in due(self.root, "2026-10-26")}
        self.assertEqual(rows["Setting context_length"]["how"], "config.yaml model.context_length")

    def test_stats(self):
        s = self.store()
        s.remember("Gateway", "port", "8787", source="Ava", check="ss -ltnp")
        s.remember("Gateway", "version", "1.3.1", source="Ava", check="gateway --version")
        s.remember("Notes app", "host", "Box-7", source="Ava", check="ssh box-7 systemctl status notes")
        st = stats(self.root, "2026-10-26")
        self.assertEqual(st["outcomes"], {"same": 2, "changed": 1})
        self.assertEqual(st["by_label"]["version"], {"changed": 1})
        self.assertIn("memory was right 2, wrong 1 (33% of known values had changed)", render_stats(st))

    # -- the agent's surface ---------------------------------------------------------

    def memory(self, on):
        return Memory(config=Config(root=self.root, owner="Sam", recall={"doubt_days": 21},
                                    checks={"on": True} if on else {}), agent="Ava", today="2026-10-26")

    def test_off_by_default(self):
        with mock.patch.dict(os.environ, {"DELTOS_CHECKS": ""}):
            m = self.memory(False)
            self.assertIs(m.tools, TOOLS)
            self.assertNotIn("check", next(t for t in m.tools if t["name"] == "remember")["parameters"]["properties"])
            self.assertNotIn("memory_checks", m.instructions())
            # an agent's check is dropped when off (the fact still lands)
            m.call("remember", {"thing": "Gateway", "attribute": "port", "value": "8787", "check": "ss -ltnp"})
            self.assertEqual(self.log(), [])

    def test_on_tools_and_instructions(self):
        with mock.patch.dict(os.environ, {"DELTOS_CHECKS": ""}):
            m = self.memory(True)
            names = [t["name"] for t in m.tools]
            self.assertEqual(names, [t["name"] for t in TOOLS] + ["memory_checks"])
            self.assertIn("check", next(t for t in m.tools if t["name"] == "remember")["parameters"]["properties"])
            self.assertEqual(len(tools_for("when", True)), len(TOOLS) + 1)
            self.assertIn("`memory_checks` lists what is due", m.instructions())
            out = m.call("remember", {"thing": "Gateway", "attribute": "port", "value": "8787", "check": "ss -ltnp"})
            self.assertIn("checked (same)", out)
            self.assertIn("Due for a check", m.call("memory_checks", {"n": 5}))

    def test_block_shows_how_only_under_a_doubted_line_when_on(self):
        Store(self.root, today="2026-08-01").remember("Gateway", "port", "8787", source="Ava",
                                                       check="ss -ltnp on box-7")
        with mock.patch.dict(os.environ, {"DELTOS_CHECKS": "", "DELTOS_DOUBT_DAYS": ""}):
            on = self.memory(True).context("What port does the Gateway listen on?")
            off = self.memory(False).context("What port does the Gateway listen on?")
        self.assertIn("not confirmed in", on)
        self.assertIn("  - check: ss -ltnp on box-7", on)
        self.assertNotIn("check: ss -ltnp", off)
        # fresh: no doubt, so no check line even when on
        Store(self.root, today="2026-10-25").remember("Gateway", "port", "8787", source="Ava", check="ss -ltnp on box-7")
        with mock.patch.dict(os.environ, {"DELTOS_CHECKS": "", "DELTOS_DOUBT_DAYS": ""}):
            fresh = self.memory(True).context("What port does the Gateway listen on?")
        self.assertNotIn("check: ss -ltnp", fresh)
        # never a loose line of its own
        with mock.patch.dict(os.environ, {"DELTOS_CHECKS": "1"}):
            self.assertNotIn("check: ss", self.memory(True).context("what does ss -ltnp show?"))


if __name__ == "__main__":
    unittest.main()
