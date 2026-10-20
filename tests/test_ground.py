"""The grounding rules (deltos/ground.py): a note must state a value, a chat must say
every number, path and date of it in the user's own lines."""
import unittest

from deltos import ground
from deltos.store import SAID_CHARS


class StatedTest(unittest.TestCase):
    def test_the_ways_a_note_states_a_value(self):
        for value, text in (("box-7", "Nimbus moved to box-7 today"),          # exact
                            ("Nimbus API gateway", "the gateway of the Nimbus API"),   # its words, any order
                            ("128GB", "a box with 128 GB of RAM"),
                            ("128 GB", "a box with 128GB of RAM"),
                            ("$2,000", "it cost 2000 dollars"),
                            ("2000", "it cost **$2,000**"),
                            ("3", "three workers"),
                            ("three", "3 workers"),
                            ("v2.17.2", "shipped 2.17.2 on Friday"),
                            ("2.17.2", "shipped `v2.17.2` on Friday"),
                            ("R&D", "the R&amp;D lab"),
                            ("2026-09-27", "moved on 09-27"),
                            ("2026-09-27", "moved on Sept 27"),
                            ("2026-09-27", "moved on the 27th of September"),
                            ("2026-09-27", "moved 9/27")):
            self.assertTrue(ground.stated(value, text), (value, text))

    def test_a_value_only_in_a_reference_string_is_not_stated(self):
        note = "- Nimbus moved to box-7 today"
        reference = "Nimbus (nb) | Port: 9100; Owner: Dana"
        self.assertTrue(ground.stated("9100", reference))
        self.assertFalse(ground.stated("9100", note))
        self.assertFalse(ground.stated("Dana", note))

    def test_nothing_and_a_dash_are_never_stated(self):
        self.assertFalse(ground.stated("", "anything - at all"))
        self.assertFalse(ground.stated("-", "anything - at all"))

    def test_a_date_is_the_units_own_day(self):
        self.assertFalse(ground.stated("2026-10-04", "no date here"))
        self.assertTrue(ground.stated("2026-10-04", "no date here", day="2026-10-04"))
        self.assertFalse(ground.stated("2026-09-27", "on Sept 2"))

    def test_named(self):
        self.assertTrue(ground.named("Brain (LLM)", "the brain moved"))
        self.assertFalse(ground.named("Zephyr", "the gateway moved"))


class ChatTest(unittest.TestCase):
    def test_words_may_be_paraphrased_identifiers_may_not(self):
        self.assertTrue(ground.idents_stated("7 bass", "user: I caught seven bass"))
        self.assertFalse(ground.idents_stated("10.0.0.5", "user: the box is at 10.0.0.6"))
        self.assertTrue(ground.idents_stated("10.0.0.6", "user: the box is at 10.0.0.6."))
        self.assertTrue(ground.idents_stated("crimson bicycle", "user: I got a red bike"))   # no digit: passes
        self.assertTrue(ground.idents_stated("met on 2023-05-20", "user: hi", day="2023-05-20"))
        self.assertFalse(ground.idents_stated("met on 2023-05-20", "user: hi", day="2023-05-21"))
        self.assertTrue(ground.idents_stated("2023-05-20", "user: we met May 20th"))
        self.assertTrue(ground.idents_stated("~/srv/app2", "user: it lives in ~/srv/app2 now"))
        self.assertFalse(ground.idents_stated("~/srv/app3", "user: it lives in ~/srv/app2 now"))

    def test_only_the_users_lines_count(self):
        text = ("user: which port should the blog use?\nassistant: Use port 8080 for the blog,\n"
                "or 8081 when that is taken.\nuser: ok, done. It runs on box-7.")
        self.assertNotIn("8081", ground.said_by_user(text))
        self.assertFalse(ground.grounded("8080", text, "chat"))
        self.assertFalse(ground.grounded("8081", text, "chat"))
        self.assertTrue(ground.grounded("box-7", text, "chat"))
        self.assertTrue(ground.grounded("8080", text, "note"))   # the note rule reads the whole text
        self.assertTrue(ground.grounded("8080", text, None))     # no rule


class ReceiptTest(unittest.TestCase):
    def test_a_window_around_the_value(self):
        line = "- " + "early words " * 30 + "the Nimbus port is 9100 now, " + "later words " * 30
        r = ground.receipt("--- memory/a.md\n" + line + "\n- 9100 again\n", "9100")
        self.assertLessEqual(len(r), SAID_CHARS)
        self.assertIn("9100", r)
        self.assertTrue(r.startswith("…") and r.endswith("…"))
        self.assertNotIn("memory/a.md", r)
        self.assertEqual(ground.receipt("- Nimbus moved\n- port 9100\n", "9100"), "- port 9100")
        self.assertEqual(ground.receipt("user: on 9100", "9100"), "on 9100")

    def test_the_value_is_a_word_of_the_receipt(self):
        self.assertTrue(ground.stated("off", "- the offset was big"))           # the note rule is loose here,
        self.assertEqual(ground.receipt("- the offset was big\n", "off"), "")   # a receipt isn't
        self.assertEqual(ground.receipt("- the offset was big\n- compaction off now\n", "off"), "- compaction off now")
        line = "- " + "x " * 150 + "it cost $2,000 in all, three times " + "y " * 150
        self.assertIn("$2,000", ground.receipt(line, "2000"))
        self.assertIn("three times", ground.receipt(line, "3"))

    def test_no_line_states_it(self):
        self.assertEqual(ground.receipt("- Nimbus moved to box-7\n", "9100"), "")
        self.assertEqual(ground.receipt("", "9100"), "")


class NumberTest(unittest.TestCase):
    """A number is that number: said with a suffix or unit it counts, inside a longer one it doesn't."""
    def test_a_number_said_with_a_suffix_or_unit_is_said(self):
        self.assertTrue(ground.idents_stated("5", "I just finished my 5th project"))
        self.assertTrue(ground.idents_stated("July 15", "my birthday is on July 15th"))
        for value, text in (("8 GB", "a box with 8gb"), ("16GB", "a box with 16 GB"), ("10 ms", "about 10ms")):
            self.assertTrue(ground.stated(value, text), (value, text))

    def test_a_number_inside_a_longer_one_is_not_said(self):
        for value, text in (("3000", "run it with --port 30001"), ("0.6", "the temperature is 0.65"),
                            ("8000", "338000/2808000 rows")):
            self.assertFalse(ground.stated(value, text), (value, text))
        for value, text in (("$300", "it cost $3,000"), ("10.0.0.5", "at 10.0.0.50"), ("1200", "about $12,000")):
            self.assertFalse(ground.idents_stated(value, text), (value, text))
        self.assertTrue(ground.idents_stated("$3,000", "it cost $3,000."))


class GroundcheckTest(unittest.TestCase):
    def test_a_gold_answer_is_read_with_its_label(self):
        from bench.groundcheck import gold_hit
        key = [r"(painting|project).*\b5\b"]
        self.assertTrue(gold_hit(["Sam", "Completed painting projects", "5", "2023-10-09"], key))
        self.assertFalse(gold_hit(["Sam", "cats", "5", "-"], key))

    def test_a_run_outside_the_scratch_band_is_marked(self):
        from bench.groundcheck import SCRATCH, verdict, within
        self.assertEqual(SCRATCH["glm-v3-g0"][1:4:2], (1390, 60))
        self.assertFalse(within(1349, 60, "glm-v3-g0"))   # -3.0% lines, same facts
        self.assertTrue(within(1363, 60, "glm-v3-g0"))    # -1.9%
        self.assertFalse(within(1390, 58, "glm-v3-g0"))   # 2 facts fewer
        self.assertTrue(within(556, 61, "glm-v3-t06"))
        self.assertEqual(verdict("W still written 12/20 (≤ 10)", 12 <= 10), "W still written 12/20 (≤ 10): FAIL")


if __name__ == "__main__":
    unittest.main()
