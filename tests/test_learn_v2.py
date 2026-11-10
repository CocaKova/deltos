"""Learning from working chats (11-06): who wrote a user turn decides what it grounds.
The first nightly learned 98 lines from 12 chats; 66 came from chats whose user turns
were another agent's briefs, and a harness's restated request carried a one-time code."""
import tempfile
import unittest
from pathlib import Path

from deltos import ground
from deltos.sources import chats, notes
from deltos.store import Store


class AuthorTest(unittest.TestCase):
    def kind(self, text, owner="Ava"):
        return chats.author(text, owner)[:2]

    def test_harness_tags_are_the_harness(self):
        for t in ("[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted",
                  "[STILL IN PROGRESS — this is the active request, restated]\nDo the thing",
                  "[SYSTEM NOTICE — iteration budget checkpoint] You have",
                  "[CONTEXT SUMMARY]: what happened",
                  "You've reached the maximum number of tool-calling iterations allowed."):
            self.assertEqual(self.kind(t), ("harness", ""), t)

    def test_someone_typing_brackets_is_the_owner(self):
        for t in ("[TODO] buy milk", "[URGENT] call the vet", "[A] the first plan", "Fix the gate [now]",
                  "[message from the user] actually it ends in 4471"):
            self.assertEqual(self.kind(t)[0], "owner", t)
        self.assertEqual(chats.author("[TODO] buy milk ", "Ava")[2], "[TODO] buy milk ")   # as typed

    def test_a_carrier_is_opened(self):
        t = "[OUT-OF-BAND USER MESSAGE — a direct message from the user]\nThe shed key is blue\n[/OUT-OF-BAND USER MESSAGE]"
        self.assertEqual(chats.author(t, "Ava"), ("owner", "Ava", "The shed key is blue"))
        t = "[OUT-OF-BAND USER MESSAGE — a direct message]\n[From Orin] Info for you: the port is 8080"
        self.assertEqual(chats.author(t, "Ava"), ("relay", "Orin", "Info for you: the port is 8080"))

    def test_a_turn_naming_its_writer_is_theirs(self):
        self.assertEqual(self.kind("[From Orin, for Ava — a client task.]\n\nRead the inbox"), ("relay", "Orin"))
        self.assertEqual(self.kind("[From Orin, grading for Ava] Strong work"), ("relay", "Orin"))
        # The owner's word passed on by an agent is the agent's writing about them.
        self.assertEqual(self.kind("[From Ava, relayed by Orin] Ava picked the blue one"), ("relay", "Orin"))
        self.assertEqual(self.kind("[From Ava] the blue one"), ("owner", "Ava"))

    def test_lines(self):
        self.assertEqual(chats._line("user", "[CONTEXT COMPACTION — REFERENCE ONLY] x", "Ava"), "")
        self.assertEqual(chats._line("user", "[From Orin] Read uid 162 and reply", "Ava"),
                         "relay (Orin): Read uid 162 and reply")
        self.assertEqual(chats._line("user", "My boat is Wren", "Ava"), "user: My boat is Wren")


class GroundTest(unittest.TestCase):
    text = ("Conversation on 2026-05-05:\n\nrelay (Orin): The dossier host is Netlify and the plan is gold\n"
            "user: Thanks. The shed key is blue\nassistant: Signed out of the registrar; status pending")

    def test_said_by_user_leaves_out_relays(self):
        self.assertNotIn("Netlify", ground.said_by_user(self.text))
        self.assertIn("Netlify", ground.relayed(self.text))
        self.assertIn("Signed out", ground.assistant_said(self.text))
        self.assertNotIn("shed", ground.assistant_said(self.text) + ground.relayed(self.text))

    def test_values_only_a_relay_or_the_assistant_said_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            Store(Path(d), today="2026-05-05").name("Halden", [], "machine")
            m = notes.Migrator(Path(d), owner="Ava", prompt=chats.chat_prompt("Ava"), chat=True)
            raw = ("Halden | dossier host | Netlify | 2026-05-05\n"
                   "Halden | registrar session | signed out | 2026-05-05\n"
                   "Ava | shed key | blue | 2026-05-05")
            st = m.session("chat:default/x", "2026-05-05", self.text, "2026-05-05", replay={"raw": raw})
            self.assertEqual((st.get("relayed"), st.get("echoed")), (1, 1))
            s = Store(Path(d))
            self.assertEqual(s.find("Ava").attrs["shed key"].value, "blue")
            self.assertNotIn("dossier host", s.find("Halden").attrs)


class PromptTest(unittest.TestCase):
    def test_v1_is_untouched_and_v2_adds_the_work_rules(self):
        v1, v2 = chats.chat_prompt("Ava"), chats.chat_prompt("Ava", "v2")
        self.assertEqual(chats.chat_prompt("Ava", "v1"), v1)
        self.assertTrue(v2.startswith(v1.split("\n\nWhat memory already holds")[0]))
        self.assertTrue(v2.endswith(v1[v1.index("\n\nWhat memory already holds"):]))
        self.assertIn('relay (<writer>)', v2)
        self.assertNotIn("<OWNER>", v2)
        with self.assertRaises(ValueError):
            chats.chat_prompt("Ava", "v9")


if __name__ == "__main__":
    unittest.main()
