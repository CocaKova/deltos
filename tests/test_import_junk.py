"""GLM's 10-19 nightly import (undone): the store and the importer let a model's guesses become
things and facts. Each rule here is one way that happened, kept out at the format level."""
import json
import tempfile
import unittest
from pathlib import Path

import deltos.sources.notes as notes
from deltos.store import Store, attribute_problem, junk_status, name_problem


class NamesTest(unittest.TestCase):
    def test_a_measurement_time_or_count_is_not_a_name(self):
        for n in ("0.4 s", "1 s", "511 tests", "pace 114 vs 137", "silences capped at 1 s", "60–1000 Hz",
                  "0.05–0.08 st median", "17:40 UTC", "10-16", "16:9 card", "35.9%"):
            self.assertIn("measurement", name_problem(n), n)
        for n in ("Qwen 27B", "GLM 1B", "Rack 1", "3070 desktop", "24 Hour Fitness", "7-Eleven", "Node 2",
                  "M3 slice 3", "1Password"):
            self.assertEqual(name_problem(n), "", n)

    def test_setting_needs_its_name(self):
        self.assertIn("which setting", attribute_problem("setting"))
        self.assertEqual(attribute_problem("setting thinking"), "")

    def test_a_status_is_a_whole_phrase_not_a_file(self):
        for v in ("silences capped at", "end slopes within", "worker.py"):
            self.assertTrue(junk_status(v), v)
        for v in ("running", "on hold", "in progress", "released"):
            self.assertFalse(junk_status(v), v)


# The note states every value the stubbed answers write, so each rule below is what keeps a line
# out (an import takes only what its text states: tests/test_ground_apply.py).
NOTE = ("--- memory/a.md\n- Nimbus 4.1 is on box-7 in the rack by the door, moved there after the long outage; "
        "port 9100.\n- deployed on box-9; tonal noise at one recording spot.\n- Widget reduced from 1154 to 263 lines.\n")


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.root = Path(self.d.name)
        Store(self.root).name("Nimbus", ["nb"], "app")
        self.real = notes.chat
        self.addCleanup(setattr, notes, "chat", self.real)

    def run_with(self, *replies):
        replies = list(replies)
        notes.chat = lambda messages, max_tokens=0, **kw: ({"content": replies.pop(0)},
                                                           {"prompt_tokens": 10, "completion_tokens": 50})
        return notes.Migrator(self.root).session("memory@x", "2026-10-04", NOTE, git_mode=True)

    def test_no_empty_thing_when_its_only_fact_is_refused(self):
        self.run_with("= Widget | | app\nWidget | status | reduced from 1154 to 263 lines | 2026-10-04", "")
        self.assertIsNone(Store(self.root).find("Widget"))

    def test_the_line_a_loop_repeats_is_not_applied(self):
        st = self.run_with("Nimbus | version | 4.1 | 2026-10-04\n" + "Nimbus | setting result | 7.3 s | 2026-10-04\n" * 12)
        attrs = Store(self.root).find("Nimbus").attrs
        self.assertEqual(attrs["version"].value, "4.1")
        self.assertNotIn("result", " ".join(attrs))
        self.assertEqual(st["loop_dropped"], 1)

    def test_a_repair_cannot_bring_in_a_new_thing(self):
        st = self.run_with("Nimbus | host | box-7 in the rack by the door, moved there after the long outage | 2026-10-04",
                           "Outage | status | over | 2026-10-04\nNimbus | host | box-7 | 2026-10-04")
        s = Store(self.root)
        self.assertIsNone(s.find("Outage"))
        self.assertEqual(s.find("Nimbus").attrs["host"].value, "box-7")
        self.assertEqual(st["repair_new_dropped"], 1)

    def test_a_thing_registered_as_a_kind_of_value_is_not_made(self):
        self.run_with("= deployed | | status\ndeployed | host | box-9 | 2026-10-04\n"
                      "= tonal noise | | measurement\ntonal noise | location | one recording spot | 2026-10-04\n"
                      "Nimbus | port | 9100 | 2026-10-04", "")
        s = Store(self.root)
        self.assertIsNone(s.find("deployed"))
        self.assertIsNone(s.find("tonal noise"))
        self.assertEqual(s.find("Nimbus").attrs["port"].value, "9100")

    def test_a_known_thing_registered_with_a_value_kind_still_takes_facts(self):
        self.run_with("= Nimbus | | status\nNimbus | port | 9100 | 2026-10-04", "")
        self.assertEqual(Store(self.root).find("Nimbus").attrs["port"].value, "9100")


class TemperatureTest(unittest.TestCase):
    def test_the_import_temperature_goes_with_the_import_calls_only_when_set(self):
        real = notes.chat
        self.addCleanup(setattr, notes, "chat", real)
        sent = []
        notes.chat = lambda messages, max_tokens=0, extra=None, **kw: (
            sent.append(extra or {}) or ({"content": "Nimbus | port | 9100 | 2026-10-04"}, {"completion_tokens": 9}))
        with tempfile.TemporaryDirectory() as d:
            notes.Migrator(Path(d), temperature=0.6).session("memory@x", "2026-10-04", "- a note\n", git_mode=True)
            notes.Migrator(Path(d)).session("memory@y", "2026-10-04", "- a note\n", git_mode=True)
        self.assertEqual(sent[0].get("temperature"), 0.6)
        self.assertNotIn("temperature", sent[1])


class ChunksTest(unittest.TestCase):
    def test_pieces_end_at_line_ends_and_name_their_note(self):
        head = "--- memory/big.md\n"
        body = "".join(f"- {i:03d} " + "word " * 120 + "\n" for i in range(40))
        pieces = notes._chunks(head + body, "--- memory/big.md (continued)\n")
        self.assertGreater(len(pieces), 1)
        self.assertTrue(pieces[0].startswith(head))
        for p in pieces[1:]:
            self.assertTrue(p.startswith("--- memory/big.md (continued)\n"))
        for p in pieces:
            self.assertTrue(p.endswith("\n"))
        rest = "".join(p.split("\n", 1)[1] if i else p[len(head):] for i, p in enumerate(pieces))
        self.assertEqual(rest, body)

    def test_one_line_longer_than_a_piece_is_cut_at_a_sentence_end(self):
        line = "- " + "This is one sentence of the note. " * 400 + "\n"
        pieces = notes._chunks(line)
        self.assertGreater(len(pieces), 1)
        for p in pieces[:-1]:
            self.assertTrue(p.endswith(". "), p[-20:])
        self.assertEqual("".join(pieces), line)

    def test_old_notes_stay_done_when_cutting_changes(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "migrate-log.jsonl"
            log.write_text("\n".join(json.dumps(r) for r in [
                {"note": "memory@old", "chunk": 0},                       # before "of": the note is done
                {"note": "memory@new", "chunk": 0, "of": 2},              # one of two pieces
                {"note": "memory@all", "chunk": 0, "of": 1}]) + "\n")
            done = notes.done_sessions(Path(d))
            self.assertIn(("memory@old", None), done)
            self.assertNotIn(("memory@new", None), done)
            self.assertIn(("memory@new", 0), done)
            self.assertIn(("memory@all", None), done)


if __name__ == "__main__":
    unittest.main()
