"""Block hygiene (10-19): the per-turn block is replayed on every later request of
a session, so it carries no detached receipts, no line twice, no thing a plain word
at the start of a sentence names, no thing whose name breaks the name rules, and
the owner only as the subject."""
import tempfile
import unittest
from pathlib import Path

from deltos.recall import Recall, _sentence_start
from deltos.store import Store


def other_notes(ctx: str) -> list[str]:
    """The lines under "Other notes", their [note · date] tags kept."""
    if "Other notes (" not in ctx:
        return []
    return ctx.split("Other notes (", 1)[1].split("\n\n")[0].splitlines()[1:]


class HygieneTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.root = Path(self.d.name)
        s = self.s = Store(self.root, today="2026-06-01")
        s.name("Kit", [], "person")                       # whose memory this is
        s.remember("Kit", "favorite show", "Slow Horses")
        s.remember("Kit", "port", "2222")
        s.name("box-7", [], "machine")
        s.remember("box-7", "location", "rack 2")
        s.name("Run", [], "process")                      # a plain word, two entries
        s.remember("Run", "schedule", "weekly")
        s.remember("Run", "host", "box-7")
        s.name("Korrin", [], "machine")
        s.remember("Korrin", "host", "rack 4")
        s.name("Quill", [], "app")
        s.remember("Quill", "runs on", "Korrin")
        s.receipt("Quill", "runs on", "Korrin", "we moved Quill onto Korrin today", "2026-06-01")

    def recall(self, owner: str = "Kit") -> Recall:
        return Recall(self.root, owner=owner)

    def test_a_plain_word_starting_a_sentence_names_nothing(self):
        r = self.recall()
        self.assertEqual(r.entities("Run the weekly report"), [])
        self.assertEqual(r.entities("Done. Run it again"), [])
        self.assertEqual(r.entities("Steps:\n  Run the report"), [])
        self.assertEqual([g[0] for g in r.entities("what does Run do")], ["Run"])
        self.assertEqual([g[0] for g in r.entities("Run the report, then tell me what Run does")], ["Run"])
        self.assertEqual(r.entities("can you run the report"), [])            # lowercase: a verb, as before
        self.assertEqual([g[0] for g in r.entities("Korrin is down")], ["Korrin"])     # not a plain word
        self.assertTrue(_sentence_start("x. Run", 3) and _sentence_start("Run", 0))
        self.assertFalse(_sentence_start("so Run", 3))

    def test_a_name_that_breaks_the_name_rules_is_not_indexed(self):
        (self.root / "notes" / "15-min.md").write_text(   # a name made before the name rules
            '---\ntitle: "15 min"\nkind: ""\ndate: "2026-06-01"\nupdated: "2026-06-01"\n---\n\n# 15 min\n\n'
            "15 min.\n\n## Details\n\n- **Status:** done (since 2026-06-01)\n")
        self.assertEqual(self.recall().entities("is 15 min enough?"), [])
        s = Store(self.root, today="2026-06-01")
        self.assertIsNotNone(s.find("15 min"))            # the store still has it
        self.assertIn("done", s.show("15 min"))

    def test_a_kind_only_thing_is_still_named(self):
        # Leaving things with no fact but their Kind out of the index was measured
        # (10-19 gate: 2 fixed, 2 broken) and dropped: their names are still search words.
        self.s.name("Oskarel", [], "tool")
        self.s.remember("Oskarel", "kind", "tool")
        self.assertEqual([g[0] for g in self.recall().entities("Where is Oskarel?")], ["Oskarel"])
        self.assertIsNotNone(Store(self.root, today="2026-06-01").find("Oskarel"))

    def test_a_thing_with_no_entry_at_all_is_still_named(self):
        # The other half of the same dropped rule: a name and nothing else.
        self.s.name("Zentavo", [], "app")
        self.assertEqual([g[0] for g in self.recall().entities("Is Zentavo up?")], ["Zentavo"])

    def test_the_hygiene_report_counts_the_still_indexed_junk_apart(self):
        # Kind-only things are indexed again (pending sign-off), so the report shows
        # how many blocks still pin one, apart from the name rules and plain words.
        from bench.hygienecheck import JUNK_REASONS, KIND_ONLY, PLAIN, junk_of, measure
        self.s.name("Oskarel", [], "tool")
        self.s.remember("Oskarel", "kind", "tool")
        r = self.recall()
        qs = {1: "Where is Oskarel?", 2: "what does Run do", 3: "Where is Korrin?"}
        rows = [{"id": i, "block": r.context(q)[0], "ms": 1} for i, q in qs.items()]
        why, names, plain = junk_of(self.root)
        self.assertEqual(why, {"oskarel": KIND_ONLY})
        res = measure(rows, qs, why, names, plain, {})
        self.assertEqual(set(res["junk_by"]), set(JUNK_REASONS))
        self.assertEqual((res["junk"], res["junk_by"][KIND_ONLY], res["junk_by"][PLAIN]), (1, 1, 0))

    def test_the_hygiene_report_says_why_a_criterion_is_not_met(self):
        # 10-19 review: 'Run' blocks (64 -> 64) and owner pins (21 -> 21) stayed, the
        # report only said FAIL. A thing with 3 or more entries has no plain-word
        # name, so the sentence-start rule never applies to it; the report says so.
        from bench.hygienecheck import mark, owner_cause, thing_cause
        self.s.remember("Run", "kind", "process")
        self.s.remember("Run", "owner team", "ops")
        r = self.recall()
        self.assertEqual([g[0] for g in r.entities("Run the weekly report")], ["Run"])   # rule 3 can't apply
        qs = ["Run the weekly report", "can you run it", "so what does Run do", "is box-7 up"]
        cause = thing_cause(r, "Run", ["Run"], [], qs)
        self.assertTrue(cause.startswith("Run: NOT MET. Its profile has 4 entries (1 of them its Kind)"), cause)
        self.assertIn("never applies", cause)
        self.assertIn("mid-sentence 1, at a sentence start only 1, in another case only 1, not at all 1", cause)
        here = {1: "what is Kit's port", 2: "what is my port", 3: "where is Korrin"}
        base = {**here, 4: "show me the logs"}
        cause = owner_cause(r, here, base)
        self.assertIn("names the owner 1, has i/my words naming an owner entry 1, "
                      "reaches the owner's profile by another path 1", cause)
        self.assertIn('1 of the 4 base pins had "me" as their only first-person word, and 1 blocks changed', cause)
        self.assertEqual((mark(True), mark(False)), ("PASS", "NOT MET"))

    def test_me_does_not_make_the_owner_the_subject(self):
        r = self.recall()
        self.assertEqual([g[0] for g in r.entities("show me where box-7 is")], ["box-7"])
        self.assertNotIn("whose memory this is", r.context("show me where box-7 is")[0])
        self.assertEqual([g[0] for g in r.entities("what is my port")], ["Kit"])
        self.assertIn("whose memory this is", r.context("what is my port")[0])
        self.assertEqual([g[0] for g in r.entities("which show do I like")], ["Kit"])

    def test_a_receipt_never_shows_under_other_notes(self):
        r = self.recall()
        ctx = r.context("Where is Korrin?")[0]
        lines = other_notes(ctx)
        self.assertTrue(any("**Runs on:** Korrin" in x for x in lines), ctx)   # the fact line stays
        self.assertFalse(any("said:" in x for x in lines), ctx)
        # under its own entry, in the pinned profile, the receipt still shows
        self.assertIn('said: "we moved Quill onto Korrin today"', r.context("What does Quill run on?")[0])

    def test_the_same_loose_line_shows_once(self):
        (self.root / "notes" / "maintenance.md").write_text(
            '---\ntitle: "Maintenance"\ndate: "2026-05-20"\nupdated: "2026-05-20"\n---\n\n'
            "- Korrin fan replaced after the  power cut\n")
        (self.root / "notes" / "incidents.md").write_text(
            '---\ntitle: "Incidents"\ndate: "2026-05-19"\nupdated: "2026-05-19"\n---\n\n'
            "- korrin fan replaced after the power cut\n")
        self.s.note("Korrin rebooted", "2026-05-22")
        lines = other_notes(self.recall().context("Where is Korrin?")[0])
        fan = [x for x in lines if "fan replaced" in x]
        self.assertEqual(len(fan), 1, lines)
        self.assertTrue(fan[0].startswith("[maintenance · "), fan)   # the best-ranked copy: the newer note
        self.assertTrue(any("Korrin rebooted" in x for x in lines), lines)

    def test_one_event_on_several_days_is_counted_each_day(self):
        for day in ("2026-05-20", "2026-05-24", "2026-05-28"):
            self.s.note("went to the gym", day)
        self.s.note("went to the dentist", "2026-05-25")
        lines = other_notes(self.recall().context("How many times did I go to the gym?")[0])
        gym = [x for x in lines if "went to the gym" in x]
        self.assertEqual(len(gym), 3, lines)
        for day in ("2026-05-20", "2026-05-24", "2026-05-28"):
            self.assertTrue(any(f"[log-{day} · " in x for x in gym), gym)

    def test_a_history_keeps_every_date_of_one_event(self):
        self.s.note("Korrin rebooted", "2026-05-22")
        self.s.note("Korrin rebooted", "2026-05-27")
        lines = other_notes(self.recall().context("When did Korrin reboot?")[0])
        rebooted = [x for x in lines if "Korrin rebooted" in x]
        self.assertEqual(len(rebooted), 2, lines)
        self.assertTrue(any("[log-2026-05-22 · " in x for x in rebooted), rebooted)
        self.assertTrue(any("[log-2026-05-27 · " in x for x in rebooted), rebooted)


if __name__ == "__main__":
    unittest.main()
