"""Volatile entries say how long they've gone unconfirmed ([recall] doubt_days):
off by default; on, a status/host/port/model... line whose newest date is old
enough reads "(since ...; not confirmed in N weeks)", counted from the store's
today (a replayed world's date in benchmarks). A thing's moved-out lines
(<thing>-extra.md) show under its own profile, not as another thing."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deltos import Memory
from deltos.config import Config
from deltos.recall import Recall
from deltos.store import Store


class UnconfirmedTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        s = Store(self.root, today="2026-06-01")
        s.name("Box-9", [], "machine")
        s.remember("Box-9", "host", "the Annex", since="2026-04-01")
        s.remember("Box-9", "port", "8080", since="2026-04-01")
        s.remember("Box-9", "owner", "Ada", since="2026-04-01")
        s.remember("Box-9", "location", "the server closet", since="2026-04-01")
        s.remember("Box-9", "setting build host", "Shed", since="2026-04-01")
        s.remember("Box-9", "tailnet ip", "100.1.2.3", since="2026-04-01")
        s.remember("Box-9", "status", "up", since="2026-05-08")
        Store(self.root, today="2026-05-01").remember("Box-9", "port", "8080")   # confirmed 05-01

    def ctx(self, days: int, today: str = "2026-05-10") -> str:
        r = Recall(self.root)
        r.today, r.doubt_days = today, days
        return r.context("Tell me about Box-9")[0]

    def line(self, ctx: str, label: str) -> str:
        return next(ln for ln in ctx.splitlines() if ln.startswith(f"- **{label}:**"))

    def test_off_by_default_changes_nothing(self):
        r = Recall(self.root)
        r.today = "2026-05-10"
        self.assertEqual(r.doubt_days, 0)
        self.assertEqual(r.context("Tell me about Box-9")[0], self.ctx(0))
        self.assertNotIn("not confirmed", self.ctx(0))

    def test_old_volatile_entries_say_so(self):
        ctx = self.ctx(21)
        self.assertEqual(self.line(ctx, "Host"), "- **Host:** the Annex (since 2026-04-01; not confirmed in 5 weeks)")
        self.assertIn("not confirmed in 5 weeks)", self.line(ctx, "Tailnet ip"))   # a label ending in a volatile word

    def test_what_is_not_live_state_or_is_fresh_says_nothing(self):
        ctx = self.ctx(21)
        self.assertNotIn("not confirmed", self.line(ctx, "Owner"))               # not live state
        self.assertNotIn("not confirmed", self.line(ctx, "Location"))            # a place or a path, not where it runs
        self.assertNotIn("not confirmed", self.line(ctx, "Setting build host"))  # a chosen value
        self.assertNotIn("not confirmed", self.line(ctx, "Status"))              # 2 days old
        self.assertEqual(self.line(ctx, "Port"), "- **Port:** 8080 (since 2026-04-01; confirmed 2026-05-01)")

    def test_counts_from_the_stores_today(self):
        self.assertNotIn("not confirmed", self.line(self.ctx(21, today="2026-04-15"), "Host"))   # 14 days then
        self.assertIn("not confirmed in 3 weeks", self.line(self.ctx(21, today="2026-04-22"), "Host"))   # 21 days
        self.assertIn("not confirmed in 3 months", self.line(self.ctx(21, today="2026-07-15"), "Host"))

    def test_config_and_environment(self):
        (self.root / "deltos.toml").write_text("[recall]\ndoubt_days = 21\n")
        self.assertEqual(Config.load(self.root).recall, {"doubt_days": 21})
        m = Memory(self.root, today="2026-05-10")
        with mock.patch.dict(os.environ, {"DELTOS_DOUBT_DAYS": ""}):
            self.assertEqual(m.recall().doubt_days, 21)
            self.assertIn("not confirmed in 5 weeks", m.context("Tell me about Box-9"))
        with mock.patch.dict(os.environ, {"DELTOS_DOUBT_DAYS": "0"}):   # the variable wins, 0 = off
            self.assertNotIn("not confirmed", m.context("Tell me about Box-9"))
        (self.root / "deltos.toml").write_text("")
        with mock.patch.dict(os.environ, {"DELTOS_DOUBT_DAYS": ""}):
            self.assertEqual(Memory(self.root).recall().doubt_days, 0)


class ExtraUnderItsThingTest(unittest.TestCase):
    def test_moved_out_lines_stay_in_the_profile_block(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            s = Store(root, today="2026-06-01")
            s.name("Brain", ["LLM"], "model")
            s.remember("Brain", "model", "GLM", since="2026-05-30")
            (root / "notes" / "brain-extra.md").write_text(
                '---\ntitle: "Brain extra (moved settings)"\n---\n\n'
                "- status (event): restarted after the model swap (2026-05-02 to 2026-05-02)\n")
            ctx = Recall(root).context("what model is the brain running after the swap?")[0]
            self.assertIn("restarted after the model swap", ctx)
            self.assertNotIn("[brain-extra", ctx)
            self.assertEqual(ctx.count("Profile entries ["), 1)


if __name__ == "__main__":
    unittest.main()
