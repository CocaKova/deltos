"""Just-in-time cues ([recall] cues, deltos/cues.py) and WHEN tool descriptions
([tools] descriptions): both off by default. On, a message that tells a change to a
known thing ends the block with one "remember it" line naming the thing and entry;
a question, a request, a plan, a value memory already has, or a message with no
change says nothing. A block with "not confirmed" lines gets the stale line."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deltos import Memory
from deltos.config import Config
from deltos.cues import STALE, news_cue
from deltos.memory import TOOLS, WHEN, tools_for
from deltos.recall import Recall
from deltos.store import Store


class CuesTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        s = Store(self.root, today="2026-09-01")
        for thing, kind, entries in (
                ("Nextcloud", "service", (("version", "28.0.4"), ("host", "Box-7"), ("status", "running"))),
                ("Backup job", "service", (("host", "nas"), ("schedule", "daily 06:00"))),
                ("Gateway", "service", (("port", "8787"), ("url", "http://box-7:8787"))),
                ("Brain", "model", (("model", "DeepSeek-V4.1-Flash"), ("host", "Box-3"))),
                ("Lyre", "app", (("version", "1.1.0"),)),
                ("Courier", "app", (("latest release", "2.19.1"),)),
                ("Mail relay", "service", (("vendor", "SendLoop"),)),
                ("Box-7", "machine", (("ip", "192.168.1.20"),))):
            s.name(thing, [], kind)
            for label, value in entries:
                s.remember(thing, label, value, since="2026-09-01")

    def cue(self, msg: str) -> str:
        return news_cue(Recall(self.root), msg)

    def test_news_names_the_thing_and_entry(self):
        for msg, want in (
                ("Updated Nextcloud to 29.0.1 this morning.", "Nextcloud version"),
                ("Moved the backup job over to the new server, atlas.", "Backup job host"),
                ("The Gateway listens on 9119 now, not 8787.", "Gateway port"),
                ("The Brain is GLM-5.3-Flash now; DeepSeek is gone.", "Brain model"),
                ("Fixed the drift: the Lyre card now says v1.2.1.", "Lyre version"),
                ("Released Courier 2.20.0 this afternoon.", "Courier latest release"),
                ("We switched the mail relay from SendLoop to PostRelay.", "Mail relay vendor"),
                ("Box-7 got a new address after the router swap: 192.168.1.44.", "Box-7 ip"),
                ("I moved the backup job's schedule to 01:00 every night.", "Backup job schedule"),
                ("We shut Nextcloud down for good last night.", "Nextcloud status")):
            with self.subTest(msg=msg):
                self.assertEqual(self.cue(msg), f"This message may change {want}: if it does, `remember` it "
                                                "before answering.")

    def test_no_news_says_nothing(self):
        for msg in (
                "What version is Nextcloud on?",                        # a question
                "Is the Brain still DeepSeek?",
                "Restart the backup job on atlas.",                     # a request
                "Can you move the backup job to atlas tomorrow?",
                "Update Nextcloud to 29.0.1 please.",
                "I'm thinking about switching the mail relay to PostRelay next month.",   # a plan
                "The Lyre card says v1.1.0, which matches the release.",                  # no change word
                "The Gateway still listens on 8787.",                   # the value memory has
                "Nextcloud is on 28.0.4 now.",                          # the value memory has, with "now"
                "Box-7 is a little slow today.",                        # no change
                "Thanks, that worked!",
                "Write a haiku about servers.",
                "The backup job finished in 4 minutes tonight."):
            with self.subTest(msg=msg):
                self.assertEqual(self.cue(msg), "")

    def test_block_carries_cues_only_when_on(self):
        msg = "Updated Nextcloud to 29.0.1 this morning."
        off = Memory(config=Config(root=self.root), today="2026-09-02")
        self.assertFalse(off.cues)
        self.assertNotIn("`remember` it", off.context(msg))
        on = Memory(config=Config(root=self.root, recall={"cues": True}), today="2026-09-02")
        block = on.context(msg)
        self.assertTrue(block.startswith(off.context(msg)))               # the same block, one line more
        self.assertTrue(block.endswith("Nextcloud version: if it does, `remember` it before answering."))
        with mock.patch.dict(os.environ, {"DELTOS_CUES": "0"}):          # the environment wins
            self.assertFalse(Memory(config=Config(root=self.root, recall={"cues": True})).cues)
        with mock.patch.dict(os.environ, {"DELTOS_CUES": "1"}):
            self.assertTrue(Memory(config=Config(root=self.root)).cues)

    def test_stale_lines_get_the_stale_cue(self):
        m = Memory(config=Config(root=self.root, recall={"cues": True, "doubt_days": 21}), today="2026-10-15")
        block = m.context("Tell me about the Gateway")
        self.assertIn("not confirmed", block)
        self.assertTrue(block.endswith(STALE))
        fresh = Memory(config=Config(root=self.root, recall={"cues": True, "doubt_days": 21}), today="2026-09-05")
        self.assertNotIn(STALE, fresh.context("Tell me about the Gateway"))

    def test_when_descriptions(self):
        self.assertIs(tools_for(), TOOLS)
        self.assertIs(Memory(config=Config(root=self.root)).tools, TOOLS)              # plain by default
        when = {t["name"]: t["description"] for t in
                Memory(config=Config(root=self.root, tools={"descriptions": "when"})).tools}
        plain = {t["name"]: t["description"] for t in TOOLS}
        self.assertEqual(set(when), set(plain))
        for name in plain:
            self.assertEqual(when[name], WHEN.get(name, "") + plain[name])
        self.assertTrue(when["remember"].startswith("Use it the moment a value changes"))
        with mock.patch.dict(os.environ, {"DELTOS_TOOL_DESCRIPTIONS": "when"}):
            self.assertIn("Use it", Memory(config=Config(root=self.root)).tools[2]["description"])


if __name__ == "__main__":
    unittest.main()
