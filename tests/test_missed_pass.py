"""The missed pass: an import answer that finished gets one more turn for the facts it left out
(GraphRAG's gleaning). 10-19: GLM at its own temperature dropped exact details, ports and paths."""
import json
import tempfile
import unittest
from pathlib import Path

import deltos.sources.notes as notes
from deltos.store import Store


class MissedPassTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.root = Path(self.d.name)
        Store(self.root).name("Nimbus", ["nb"], "app")
        self.real = notes.chat
        self.addCleanup(setattr, notes, "chat", self.real)
        self.asked = []

    def serve(self, *answers):
        answers = list(answers)

        def chat(messages, max_tokens=0, **kw):
            self.asked.append(messages)
            a = answers.pop(0)
            return {"content": a[0]}, {"finish_reason": a[1], "completion_tokens": 10}
        notes.chat = chat

    def session(self, missed=True, root=None, replay=None):
        return notes.Migrator(root or self.root, missed=missed).session(
            "memory@x", "2026-10-04", "--- memory/a.md\n- Nimbus runs on box-7, port 8890\n", git_mode=True, replay=replay)

    def test_what_was_left_out_is_asked_for_and_kept(self):
        self.serve(("Nimbus | host | box-7 | 2026-10-04", "stop"), ("Nimbus | port | 8890 | 2026-10-04", "stop"))
        st = self.session()
        self.assertEqual(self.asked[1][-1]["content"], notes.MISSED)
        self.assertEqual(self.asked[1][-2], {"role": "assistant", "content": "Nimbus | host | box-7 | 2026-10-04"})
        attrs = Store(self.root).find("Nimbus").attrs
        self.assertEqual((attrs["host"].value, attrs["port"].value), ("box-7", "8890"))
        self.assertEqual(st["missed_added"], 1)
        d2 = Path(tempfile.mkdtemp())   # a replay applies the logged pass, with no model
        Store(d2).name("Nimbus", ["nb"], "app")
        notes.chat = None
        self.session(root=d2, replay=json.loads(json.dumps(st)))
        self.assertEqual(Store(d2).find("Nimbus").attrs["port"].value, "8890")

    def test_off_by_default_and_never_after_a_cut_off_answer(self):
        self.serve(("Nimbus | host | box-7 | 2026-10-04", "stop"))
        self.session(missed=False)
        self.assertEqual(len(self.asked), 1)
        self.asked.clear()
        self.serve(("Nimbus | host | box-7 | 2026-10-04\nNimbus | po", "length"))
        st = self.session()
        self.assertNotIn(notes.MISSED, [m[-1]["content"] for m in self.asked])
        self.assertTrue(st["capped"])


if __name__ == "__main__":
    unittest.main()
