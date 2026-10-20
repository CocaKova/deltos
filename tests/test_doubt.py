"""Phase 3, confidently wrong (bench/doubt.py): the label parse, and the score
counts wrong-and-sure answers from the cache without the brain."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bench.doubt as D


def row(qid, answer, correct, type_="multi-session"):
    return {"qid": qid, "type": type_, "answer": answer, "correct": correct}


class ParseTest(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(D.parse("SURE"), "sure")
        self.assertEqual(D.parse("unsure."), "unsure")
        self.assertEqual(D.parse("-> NO ANSWER"), "no answer")
        self.assertEqual(D.parse("NO_ANSWER"), "no answer")
        self.assertIsNone(D.parse("maybe"))

    def test_a_long_reply_keeps_its_end(self):
        r = "x" * 3000 + " but I may have missed one."
        self.assertIn("may have missed one", D.clip(r))
        self.assertLessEqual(len(D.clip(r)), 1510)


class RowLabelTest(unittest.TestCase):
    def test_a_row_label_wins_over_the_cache(self):
        r = dict(row("a", "3 bikes", False), doubt="unsure")
        s = D.score([r], {D.key("a", "3 bikes"): "sure"})
        self.assertEqual((s["confidently_wrong"], s["wrong_unsure"]), (0, 1))


class ScoreTest(unittest.TestCase):
    def test_counts(self):
        rs = [row("a", "3 bikes", False), row("b", "maybe 3", False), row("c", "no record", False),
              row("d", "4 bikes", True), row("e", "4, I think", True), row("f", "try tea", False, "single-session-preference"),
              row("g", "ERROR timeout", False)]
        cache = {D.key("a", "3 bikes"): "sure", D.key("b", "maybe 3"): "unsure", D.key("c", "no record"): "no answer",
                 D.key("d", "4 bikes"): "sure", D.key("e", "4, I think"): "unsure"}
        s = D.score(rs, cache)
        self.assertEqual((s["n"], s["right"], s["wrong"]), (5, 2, 3))
        self.assertEqual((s["confidently_wrong"], s["wrong_unsure"], s["wrong_no_answer"]), (1, 1, 1))
        self.assertEqual(s["says_so_when_wrong"], round(2 / 3, 3))
        self.assertEqual((s["hedged_when_right"], s["unlabeled"]), (1, 0))

    def test_label_asks_only_what_the_cache_lacks(self):
        asked = []

        def chat(messages, **kw):
            asked.append(messages[0]["content"])
            return {"content": "UNSURE"}, {}

        with tempfile.TemporaryDirectory() as d:
            res, cache = Path(d) / "r.jsonl", Path(d) / "doubt.json"
            res.write_text("\n".join(json.dumps(r) for r in [row("q1", "a", False), row("q2", "b", True)]) + "\n")
            cache.write_text(json.dumps({D.key("q1", "a"): "sure"}))
            import bench.lme as L
            with mock.patch.object(D, "CACHE", cache), mock.patch.object(D.llm, "chat", chat), \
                 mock.patch.object(L, "questions", lambda t, n: [{"question_id": "q1", "question": "Q1?"},
                                                                   {"question_id": "q2", "question": "Q2?"}]):
                D.label([str(res)], workers=1, say=lambda *a, **k: None)
            self.assertEqual(len(asked), 1)
            self.assertIn("Question: Q2?\nReply: b", asked[0])
            self.assertEqual(json.loads(cache.read_text())[D.key("q2", "b")], "unsure")
