"""Model-agnostic import: a cut-off output is asked again in halves (and replays
apply the halves), a loop is flagged, the reference list never sits in the
instructions, and its "Name (aliases)" form names the thing."""
import tempfile
import unittest
from pathlib import Path

import deltos.sources.notes as notes
from deltos.store import Store

# The note states what the stubbed answers say (an import takes only what its text states):
# running, and 4.1 in the first half, box-7 in the second.
TEXT = ("--- memory/a.md\n- Nimbus 4.1 shipped, running\n" + "".join(f"- line {i}: Nimbus port {9000 + i}\n" for i in range(120))
        + "- Nimbus moved to box-7\n")


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.root = Path(self.d.name)
        Store(self.root).name("Nimbus", ["nb"], "app")
        self.calls = []
        self.real = notes.chat
        self.addCleanup(setattr, notes, "chat", self.real)

    def stub(self, replies):
        def chat(messages, max_tokens=0, **kw):
            self.calls.append(messages)
            content, out_tokens = replies.pop(0)
            return {"content": content}, {"prompt_tokens": 10, "completion_tokens": out_tokens}
        notes.chat = chat

    def test_cut_off_is_asked_again_in_halves_and_replayed(self):
        loop = "Nimbus | status | running | 2026-10-04\n" + "Nimbus | port | 9000 | 2026-10-04\n" * 300
        self.stub([(loop, notes.MAX_TOKENS),
                   ("Nimbus | version | 4.1 | 2026-10-04", 50),
                   ("Nimbus | host | box-7 | 2026-10-04", 50)])
        st = notes.Migrator(self.root, owner="Sam").session("memory@x", "2026-10-04", TEXT, git_mode=True)
        self.assertEqual(st["loop"], 300)
        self.assertEqual(len(st["subs"]), 2)
        self.assertEqual(st["lost"], 0)
        self.assertTrue(self.calls[2][1]["content"].count("--- memory/a.md"))   # the half keeps its file header
        attrs = Store(self.root).find("Nimbus").attrs
        self.assertEqual((attrs["version"].value, attrs["host"].value), ("4.1", "box-7"))
        # The line it looped on is where it got stuck, not a fact said 300 times (10-19); the
        # halves are what recovers the note's facts.
        self.assertNotIn("port", attrs)
        self.assertEqual(st["loop_dropped"], 1)
        # A replay of the logged session applies the halves without a model.
        other = Path(tempfile.mkdtemp())
        Store(other).name("Nimbus", ["nb"], "app")
        notes.chat = None
        rst = notes.Migrator(other).session("memory@x", "2026-10-04", "", git_mode=True, replay=st)
        self.assertEqual(Store(other).find("Nimbus").attrs["host"].value, "box-7")
        self.assertEqual(rst["facts"], st["facts"])

    def test_cut_off_too_small_to_split_is_logged_lost(self):
        self.stub([("Nimbus | port | 9000 | 2026-10-04\n" * 50, notes.MAX_TOKENS)])
        st = notes.Migrator(self.root).session("memory@y", "2026-10-04", "- short note\n", git_mode=True)
        self.assertEqual(st["lost"], 1)
        self.assertIn("LOST", notes._flags(st))
        self.assertIn("WARNING", notes.summary([st]))

    def test_empty_answer_after_thinking_is_a_cut_off_retried_with_more_budget(self):
        seen = []

        def chat(messages, max_tokens=0, **kw):
            seen.append(max_tokens)
            if len(seen) == 1:   # thought the whole budget away: no answer, finish "length" not reported
                return {"content": ""}, {"completion_tokens": 7000, "completion_tokens_details": {"reasoning_tokens": 7000},
                                         "finish_reason": "stop"}
            return {"content": "Nimbus | version | 4.1 | 2026-10-04"}, {"completion_tokens": 900, "finish_reason": "stop"}
        notes.chat = chat
        st = notes.Migrator(self.root).session("memory@t", "2026-10-04", TEXT, git_mode=True)
        self.assertEqual(st["cut_off"], "thinking")
        self.assertEqual(seen, [notes.MAX_TOKENS, 2 * notes.MAX_TOKENS, 2 * notes.MAX_TOKENS])
        self.assertEqual(st["lost"], 0)
        self.assertIn("CUT-OFF thinking", notes._flags(st))
        self.assertIn("cut-offs 1 (thinking 1)", notes.summary([st]))
        # the replay sees the same cut-off and applies the halves
        other = Path(tempfile.mkdtemp())
        Store(other).name("Nimbus", ["nb"], "app")
        rst = notes.Migrator(other).session("memory@t", "2026-10-04", "", git_mode=True, replay=st)
        self.assertEqual((rst["cut_off"], len(rst["subs"])), ("thinking", 2))

    def test_finish_reason_length_is_a_cut_off(self):
        self.stub([("Nimbus | port | 9000 | 2026-10-04\nNimbus | ho", 4000)])
        notes.chat = lambda m, max_tokens=0, **kw: ({"content": "Nimbus | port | 9000 | 2026-10-04\nNimbus | ho"},
                                                    {"completion_tokens": 4000, "finish_reason": "length"})
        st = notes.Migrator(self.root, split=False).session("memory@u", "2026-10-04", "- short\n", git_mode=True)
        self.assertEqual((st["cut_off"], st["lost"]), ("length", 1))

    def test_names_only_answer_gets_one_more_turn_for_the_facts(self):
        self.stub([("= Nimbus | nb | app\n= box-7 | | machine\n= Dana | | person", 30),
                   ("Nimbus | host | box-7 | 2026-10-04", 12)])
        st = notes.Migrator(self.root).session("memory@n", "2026-10-04", "- Nimbus runs on box-7 now\n", git_mode=True)
        self.assertEqual((st["names_only"], st["followed_up"]), (3, 1))
        self.assertEqual(self.calls[1][-2]["role"], "assistant")          # the follow-up continues the turn
        self.assertIn("wrote no facts", self.calls[1][-1]["content"])
        self.assertIn("NAMES-ONLY +1", notes._flags(st))
        other = Path(tempfile.mkdtemp())
        Store(other).name("Nimbus", ["nb"], "app")
        notes.chat = None
        notes.Migrator(other).session("memory@n", "2026-10-04", "", git_mode=True, replay=st)
        self.assertEqual(Store(other).find("Nimbus").attrs["host"].value, "box-7")

    def test_reference_list_is_fenced_and_marked_reference(self):
        self.stub([("Nimbus | port | 9100 | 2026-10-04", 20)])
        notes.Migrator(self.root, notes_prompt="v3").session("memory@z", "2026-10-04", "- Nimbus moved to port 9100\n", git_mode=True)
        system, user = self.calls[0][0]["content"], self.calls[0][1]["content"]
        held = system[system.index("<memory>"):system.index("</memory>")]
        self.assertIn("Nimbus (nb)", held)
        self.assertIn("reference, not part of the note", system)
        self.assertTrue(user.startswith("Note memory@z:"))
        self.assertNotIn("Nimbus (nb)", user)

    def test_reference_display_form_names_the_thing(self):
        Store(self.root).remember("Nimbus", "port", "9000", since="2026-10-01")
        self.stub([("Nimbus (nb) | port | 9000 | -\nNimbus (nb) | host | box-7 | 2026-10-04", 20)])
        st = notes.Migrator(self.root).session("memory@w", "2026-10-04", "- nb runs on box-7, port 9000\n", git_mode=True)
        self.assertEqual(st["display_names"], 2)
        self.assertEqual(st["restated"], 1)                     # the port only repeated what memory had
        self.assertEqual(Store(self.root).find("Nimbus").attrs["host"].value, "box-7")

    def test_v1_prompt_kept_for_comparison(self):
        self.stub([("", 5)])
        notes.Migrator(self.root, notes_prompt="v1").session("memory@v", "2026-10-04", "- Nimbus\n", git_mode=True)
        self.assertIn("Nimbus (nb)", self.calls[0][0]["content"])
        self.assertNotIn("<memory>", self.calls[0][0]["content"])
        self.assertTrue(self.calls[0][1]["content"].startswith("Note memory@v:"))

    def test_summary_warns_on_refusals(self):
        rows = [{"facts": 56, "refused": 44}, {"facts": 10, "refused": 0}]
        self.assertIn("WARNING: refused", notes.summary(rows))
        self.assertNotIn("WARNING", notes.summary([{"facts": 95, "refused": 5}]))


if __name__ == "__main__":
    unittest.main()
