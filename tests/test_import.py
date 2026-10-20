"""The importer, with a stub model: refused lines get one repair call, and the
next session's prompt shows the entries the text talks about."""
import tempfile
import unittest
from pathlib import Path

import deltos.sources.notes as notes
from deltos.store import Store

PROMPT = "Chat on {day}. Memory holds:\n{known}"


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        Store(Path(self.d.name)).name("Sam", [], "person")
        self.calls = []
        self.real = notes.chat
        self.addCleanup(setattr, notes, "chat", self.real)

    def stub(self, replies):
        def chat(messages, max_tokens=0, **kw):
            self.calls.append(messages)
            return {"content": replies.pop(0)}, {"prompt_tokens": 10, "completion_tokens": 5}
        notes.chat = chat

    def test_refused_lines_are_repaired(self):
        self.stub(["Sam | short stories written | 4 | 2023-05-23\nSam's car | color | red | 2023-05-23",
                   "Sam | car color | red | 2023-05-23"])
        m = notes.Migrator(Path(self.d.name), owner="Sam", prompt=PROMPT)
        st = m.session("s1", "2023-05-23", "user: I wrote four short stories. My car is red.")
        self.assertEqual(st["repaired"], 1)
        self.assertIn("phrase", self.calls[1][0]["content"])          # the store's reason went back
        attrs = Store(Path(self.d.name)).find("Sam").attrs
        self.assertEqual(attrs["car color"].value, "red")

    def test_known_shows_the_entry_the_text_talks_about(self):
        self.stub(["Sam | short stories written | 4 | 2023-05-23", "Sam | short stories written | 7 | 2023-05-30"])
        m = notes.Migrator(Path(self.d.name), owner="Sam", prompt=PROMPT)
        m.session("s1", "2023-05-23", "user: I have written four short stories.")
        m.session("s2", "2023-05-30", "user: I've completed seven short stories now!")
        self.assertIn("Short stories written: 4", self.calls[1][0]["content"])   # first person -> the owner
        self.assertEqual(Store(Path(self.d.name)).find("Sam").attrs["short stories written"].value, "7")

    def test_replay_applies_the_logged_repair(self):
        m = notes.Migrator(Path(self.d.name), owner="Sam")
        st = m.session("s1", "2023-05-23", "", replay={"raw": "Sam's car | color | red | 2023-05-23",
                                                         "repair_raw": "Sam | car color | red | 2023-05-23"})
        self.assertEqual(st["repaired"], 1)

    def test_facts_packed_into_a_name_line(self):
        m = notes.Migrator(Path(self.d.name), owner="Sam")
        m.session("s1", "2023-09-30", "", replay={"raw": "= Alex | Al; Location: Germany; Times met: 2 | person"})
        t = Store(Path(self.d.name)).find("Alex")
        self.assertEqual(t.attrs["times met"].value, "2")
        self.assertEqual(t.attrs["location"].value, "Germany")
        self.assertEqual(t.aliases, ["Al"])   # a name before the facts is still a name

    def test_a_list_in_a_packed_name_line_is_one_value(self):
        """A piece after a fact continues that fact's list (95 of 126 leftover pieces
        in LongMemEval's extraction); read as names they made the user "also called
        Delta", "Facebook", "twigs"."""
        m = notes.Migrator(Path(self.d.name), owner="Sam")
        m.session("s1", "2023-02-10", "", replay={"raw": "= Sam | Shopping platforms: Etsy; Zazzle; "
                                                         "Airline consideration: American Airlines; Delta | person"})
        t = Store(Path(self.d.name)).find("Sam")
        self.assertEqual(t.aliases, [])
        self.assertEqual((t.attrs["shopping platforms"].value, t.attrs["airline consideration"].value),
                         ("Etsy; Zazzle", "American Airlines; Delta"))

    def test_events_go_to_the_daily_log(self):
        m = notes.Migrator(Path(self.d.name), owner="Sam", chat=True)
        st = m.session("s1", "2023-05-25", "", replay={"raw": "+ 2023-05-23 | baked sourdough bread\n"
                                                              "+ 2023-06-30 | a date after the chat\n+ - | no date"})
        self.assertEqual(st["events"], 3)
        logs = {p.name: p.read_text() for p in (Path(self.d.name) / "notes").glob("log-*.md")}
        self.assertIn("baked sourdough bread", logs["log-2023-05-23.md"])
        self.assertIn("a date after the chat", logs["log-2023-05-25.md"])   # not in the future
        self.assertIn("no date", logs["log-2023-05-25.md"])

    def known_mistake(self):
        """Sam's city: Paris from an import, corrected to London by hand on 2023-02-01."""
        m = notes.Migrator(Path(self.d.name), owner="Sam")
        m.session("s1", "2023-01-10", "", replay={"raw": "Sam | city | Paris | 2023-01-01"})
        s = Store(Path(self.d.name))
        s.today = "2023-02-01"
        s.remember("Sam", "city", "London", correction=True)
        a = Store(Path(self.d.name)).find("Sam").attrs["city"]
        self.assertEqual((a.value, a.trail[0].value, a.trail[0].wrong, a.trail[0].by), ("London", "Paris", True, "hand"))
        self.assertIn("(corrected 2023-02-01 by hand; never true)", (Path(self.d.name) / "notes" / "sam.md").read_text())
        return notes.Migrator(Path(self.d.name), owner="Sam")

    def test_a_known_mistake_said_again_by_a_note_is_not_taken(self):
        m = self.known_mistake()
        for i, line in enumerate(("Sam | city | Paris | -", "Sam | city | Paris | 2023-01-01")):
            st = m.session(f"s{3 + i}", "2023-03-01", "", replay={"raw": line})
            self.assertEqual(st.get("known_mistakes"), 1, line)
            self.assertIn("recorded by mistake (corrected 2023-02-01)", st["refusals"][0])
        self.assertEqual(Store(Path(self.d.name)).find("Sam").attrs["city"].value, "London")

    def test_a_known_mistake_dated_after_the_correction_is_news(self):
        m = self.known_mistake()
        st = m.session("s3", "2023-06-01", "", replay={"raw": "Sam | city | Paris | 2023-05-20"})
        self.assertNotIn("known_mistakes", st)
        a = Store(Path(self.d.name)).find("Sam").attrs["city"]
        self.assertEqual((a.value, a.since), ("Paris", "2023-05-20"))

    def test_an_imports_own_correction_does_not_hold(self):
        """Replaying the agent's imports: some of their corrections were wrong (gbrain
        "active" in August), so a later note may say the value again."""
        m = notes.Migrator(Path(self.d.name), owner="Sam")
        m.session("s1", "2023-01-10", "", replay={"raw": "Sam | city | Paris | 2023-01-01"})
        m.session("s2", "2023-02-01", "", replay={"raw": "Sam | city | London | 2023-01-01 | correction"})
        self.assertEqual(Store(Path(self.d.name)).find("Sam").attrs["city"].trail[0].by, "")
        st = m.session("s3", "2023-03-01", "", replay={"raw": "Sam | city | Paris | -"})
        self.assertNotIn("known_mistakes", st)
        self.assertEqual(Store(Path(self.d.name)).find("Sam").attrs["city"].value, "Paris")

    def test_a_direct_save_of_a_known_mistake_goes_through(self):
        self.known_mistake()
        s = Store(Path(self.d.name))
        s.remember("Sam", "city", "Paris")
        self.assertEqual(Store(Path(self.d.name)).find("Sam").attrs["city"].value, "Paris")

    def test_single_correction_on_one_date_ends_current(self):
        m = notes.Migrator(Path(self.d.name), owner="Sam")
        m.session("s1", "2023-11-30", "", replay={"raw": "Sam | pre-approval | $350,000 | 2023-11-30\n"
                                                        "Sam | pre-approval | $400,000 | 2023-11-30 | correction"})
        a = Store(Path(self.d.name)).find("Sam").attrs["pre-approval"]
        self.assertEqual(a.value, "$400,000")

    def test_later_dated_correction_is_an_update(self):
        m = notes.Migrator(Path(self.d.name), owner="Sam")
        m.session("s1", "2023-05-11", "", replay={"raw": "Sam | team size | 4 engineers | 2023-05-11"})
        m.session("s2", "2023-10-24", "", replay={"raw": "Sam | team size | 5 engineers | 2023-10-24 | correction"})
        a = Store(Path(self.d.name)).find("Sam").attrs["team size"]
        self.assertEqual(a.value, "5 engineers")
        self.assertFalse(a.trail[0].wrong)     # 4 was true until 10-24, not "never true"

    def test_repair_sees_the_line_as_written(self):
        self.stub(["Rachel | attribute | current employer | TechCorp", ""])
        Store(Path(self.d.name)).name("Rachel", [], "person")
        notes.Migrator(Path(self.d.name), prompt=PROMPT).session("s1", "2023-05-26", "chat")
        self.assertIn("TechCorp", self.calls[1][0]["content"])


if __name__ == "__main__":
    unittest.main()
