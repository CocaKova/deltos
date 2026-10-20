"""Follow-up carry (10-19): a short message, or one that names nothing, is read
with the previous message's things (facts) and words (pages)."""
import shutil
import tempfile
import unittest
from pathlib import Path

from deltos import Memory
from deltos.config import Config


def _page(d: Path, name: str, day: str, line: str) -> None:
    (d / name).write_text(f"---\nupdated: {day}\n---\n\n{line}\n")


class FollowUp(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        wiki = self.d / "wiki"
        wiki.mkdir()
        # The backup page is the oldest of five that say "write to disk".
        _page(wiki, "box7-backup.md", "2026-08-01", "the box-7 backup job will write to disk sdb2 nightly")
        for k, what in enumerate(("scratch task", "photo sync", "cache layer", "log shipper")):
            _page(wiki, f"other{k}.md", f"2026-10-1{k}", f"the {what} will write to disk sd{k}")
        _page(wiki, "misc.md", "2026-10-01", "the port list is long")
        self.m = Memory(config=Config(root=self.d / "store", owner="Ada", pages_write=wiki), today="2026-10-19")
        self.m.call("name_thing", {"thing": "box-7", "kind": "machine"})
        self.m.call("remember", {"thing": "box-7", "attribute": "host", "value": "rack 2"})
        self.m.call("name_thing", {"thing": "Ada", "kind": "person"})
        self.m.call("remember", {"thing": "Ada", "attribute": "port", "value": "8080"})
        self.m.call("name_thing", {"thing": "box-9", "kind": "machine"})
        self.m.call("remember", {"thing": "box-9", "attribute": "host", "value": "rack 5"})

    def test_short_follow_up_carries_the_previous_message(self):
        first, then = "Where does box-7's backup job run?", "which disk does it write to?"
        alone = self.m.context(then)
        self.assertNotIn("box7-backup.md", alone)
        carried = self.m.context(then, previous=first)
        pages = carried.split("Pages (", 1)[1]
        self.assertTrue(pages.split("\n", 1)[1].startswith("[box7-backup.md · 2026-08-01"))
        self.assertIn("Profile entries [box-7", carried)                 # the thing it was about
        # A message that names its own thing and is long is read alone.
        long = "what is the host of box-7 and which disk does the scratch task write to"
        self.assertEqual(self.m.context(long, previous="tell me about the photo sync"), self.m.context(long))

    def test_a_short_message_naming_its_own_thing_carries_no_facts(self):
        # "restart box-9 now" after a question about box-7 is about box-9: box-7's
        # profile would be the wrong subject. Its words still rank pages.
        first, then = "what is the host of box-7?", "restart box-9 now"
        carried = self.m.context(then, previous=first)
        self.assertIn("Profile entries [box-9", carried)
        self.assertNotIn("Profile entries [box-7", carried)
        facts = lambda b: b.split("\n\nPages (", 1)[0]
        self.assertEqual(facts(carried), facts(self.m.context(then)))
        # a message that names nothing still carries the previous message's things
        self.assertIn("Profile entries [box-7", self.m.context("and which rack?", previous=first))

    def test_a_plain_named_thing_carries_after_a_question_mark(self):
        # A thing named by a plain word ("Ledger") counts only where a name is
        # written, not at a sentence start: the carried names must not follow a "?".
        self.m.call("name_thing", {"thing": "Ledger", "kind": "app"})
        self.m.call("remember", {"thing": "Ledger", "attribute": "export disk", "value": "sdc1"})
        r = self.m.recall()
        first = "where does Ledger export to?"
        self.assertEqual([g[0] for g in r.entities(first)], ["Ledger"])
        carried = self.m.context("which disk?", previous=first)
        self.assertIn("Profile entries [ledger", carried)
        self.assertIn("sdc1", carried)

    def test_no_previous_message_changes_nothing(self):
        for q in ("which disk does it write to?", "Where does box-7's backup job run?", "what is my port",
                  "What did we work on recently?", "thanks!"):
            self.assertEqual(self.m.context(q, previous=""), self.m.context(q))
            self.assertEqual(self.m.context(q, previous="   "), self.m.context(q))

    def test_the_owner_alone_names_nothing(self):
        r = self.m.recall()
        self.assertEqual([g[0] for g in r.entities("what is my port")], ["Ada"])   # only the owner, by "my"
        block = self.m.context("what is my port")
        self.assertIn("8080", block)                       # the owner's entry still shows
        self.assertNotIn("misc.md", block)                 # but one loose word is no page


if __name__ == "__main__":
    unittest.main()
