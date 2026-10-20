import unittest

from deltos.sources.notes import _plan


class PlanTest(unittest.TestCase):
    def test_since_word_is_read_as_a_date(self):
        lines, _ = _plan("A | v | 1 | since 2026-09-24\nA | w | 2 | since 2026-06\nA | x | 3 | -", False)
        self.assertEqual([p[3] for p in lines], ["2026-09-24", "-", "-"])

    def test_a_loop_is_not_applied(self):
        # Until 10-19 the plain value of a flip loop was applied once. The lines a model loops on are
        # where it got stuck (GLM 10-19: none of 16 loops repeated a fact worth keeping); a cut-off
        # loop is asked again in halves, which is where the note's facts come back from.
        loop = "K | rate | 75 KB/h | since 2026-09-24\nK | rate | 25 MB/h | since 2026-09-24 | correction\n" * 50
        lines, st = _plan(loop + "K | ra", True)
        self.assertEqual(lines, [])
        self.assertEqual(st["loop_dropped"], 2)

    def test_value_dump_is_dropped(self):
        dump = "".join(f"Resume | status | tag{i} | 2026-09-27\n" for i in range(9))
        lines, _ = _plan(dump + "Resume | owner | Alex | 2026-09-27", False)
        self.assertEqual(lines, [["Resume", "owner", "Alex", "2026-09-27"]])

    def test_registration_marker_on_a_fact_line(self):
        lines, _ = _plan("= vLLM | | service\n= vLLM | setting MTP | 3 | 2026-09-08", False)
        self.assertEqual(lines, [["= vLLM", "", "service"], ["vLLM", "setting MTP", "3", "2026-09-08"]])

    def test_a_fact_without_its_date_column_is_kept(self):
        lines, _ = _plan("Sam | fitness activity | spin classes at the gym\nSam | goal | \n= Sam | | person", False)
        self.assertEqual(lines, [["Sam", "fitness activity", "spin classes at the gym", "-"], ["Sam", "goal", ""],
                                 ["= Sam", "", "person"]])

    def test_a_registration_without_its_marker_is_a_registration(self):
        lines, _ = _plan("GLM-5.3 | GLM; glm53-head | model\nSam | fitness activity | spin classes", False)
        self.assertEqual(lines, [["= GLM-5.3", "GLM; glm53-head", "model"], ["Sam", "fitness activity", "spin classes", "-"]])

    def test_changes_in_order_survive(self):
        lines, _ = _plan("K | version | 2.17.2 | 2026-10-04\nK | version | 2.17.3 | 2026-10-06", False)
        self.assertEqual(len(lines), 2)

    def test_packed_line_is_split(self):
        lines, _ = _plan("sync-daemon|host: laptop; version: 0.29.1; status: running", False)
        self.assertEqual(lines, [["sync-daemon", "host", "laptop", "-"], ["sync-daemon", "version", "0.29.1", "-"],
                                 ["sync-daemon", "status", "running", "-"]])
        lines, _ = _plan("= Atlas | Acme Atlas | machine", False)   # registrations stay registrations
        self.assertEqual(lines, [["= Atlas", "Acme Atlas", "machine"]])

    def test_correction_on_the_old_value_is_not_trusted(self):
        lines, st = _plan("Hermes | setting verify_on_stop | false | since 2026-09-28\n"
                          "Hermes | setting verify_on_stop | true | since 2026-09-27 | correction", False)
        self.assertEqual(lines, [["Hermes", "setting verify_on_stop", "false", "2026-09-28"],
                                 ["Hermes", "setting verify_on_stop", "true", "2026-09-27"]])
        lines, _ = _plan("A | serial | SN-2 | 2026-01-05 | correction", False)   # a lone correction stays
        self.assertEqual(lines[0][-1], "correction")


if __name__ == "__main__":
    unittest.main()
