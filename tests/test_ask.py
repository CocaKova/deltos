"""One call for every model request (deltos/llm.py ask): an answer that loops is
stopped as it streams and asked again one rung hotter on a fixed ladder, an answer
out of the caller's format the same way, a cut-off answer goes straight back to
the caller (only it knows how to split its input), and the items check uses it."""
import io
import json
import re
import unittest
import urllib.request
from unittest import mock

import deltos.llm as llm
from deltos import items

LOOP = ["Nimbus | host | box-7 | -\n"] + ["Nimbus | port | 8000 | -\n", "Nimbus | port | 8001 | -\n"] * 6


def streamed(lines, stop, usage=None):
    """What chat() returns for a streamed answer: fed a line at a time to `stop`,
    ended where it says so (finish_reason "repeat"), else finished."""
    text = ""
    for n, ln in enumerate(lines, 1):
        text += ln
        why = stop(text) if stop and "\n" in ln else ""
        if why:
            return {"content": text}, {"finish_reason": why, "completion_tokens": n, "prompt_tokens": 7}
    return {"content": text}, {"finish_reason": "stop", "completion_tokens": len(lines), "prompt_tokens": 7,
                               **(usage or {})}


def done(text):
    return lambda stop: streamed([text], stop)


def looping(lines=LOOP):
    return lambda stop: streamed(lines, stop)


class Fake:
    """A transport (chat's signature) answering from a script, logging what it was asked with."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, messages, max_tokens=0, extra=None, stop=None, **kw):
        self.calls.append({"messages": messages, "extra": extra, "stop": stop, "max_tokens": max_tokens})
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a(stop) if callable(a) else a

    def temps(self):
        return [c["extra"]["temperature"] for c in self.calls]


def ask(fake, **kw):
    return llm.ask([{"role": "user", "content": "x"}], max_tokens=kw.pop("max_tokens", 100), transport=fake, **kw)


class LadderTest(unittest.TestCase):
    def test_the_rungs_start_where_configured_and_only_climb(self):
        self.assertEqual(llm.rungs(), [0.0, 0.5, 1.0])
        self.assertEqual(llm.rungs(None), [0.0, 0.5, 1.0])
        self.assertEqual(llm.rungs(0.6), [0.6, 1.0])
        self.assertEqual(llm.rungs(1.0), [1.0])
        self.assertEqual(llm.rungs(1.2), [1.2])

    def test_a_finished_answer_is_one_try(self):
        fake = Fake(done("Nimbus | port | 8000 | -"))
        a = ask(fake, valid=lambda t: "|" in t)
        self.assertEqual((a.text, a.cut, a.rung, a.temperature, a.recovered), ("Nimbus | port | 8000 | -", "", 0, 0.0, False))
        self.assertEqual(len(a.tries), 1)
        self.assertEqual(a.tries[0], {"t": 0.0, "finish": "stop", "out_tokens": 1, "reasoning": 0, "loop": 0, "loop_body": []})
        self.assertIs(fake.calls[0]["stop"], llm.repeating)        # streamed with the repeat guard on
        self.assertEqual(fake.calls[0]["extra"], {"temperature": 0.0})

    def test_a_loop_is_asked_again_one_rung_up(self):
        fake = Fake(looping(), done("Nimbus | port | 8000 | -"))
        a = ask(fake)
        self.assertEqual(fake.temps(), [0.0, 0.5])
        self.assertEqual((a.cut, a.rung, a.temperature, a.recovered), ("", 1, 0.5, True))
        self.assertEqual(a.text, "Nimbus | port | 8000 | -")    # the second answer, not the first
        first = a.tries[0]
        self.assertEqual((first["finish"], first["loop"]), ("repeat", llm.REPEAT_STOP))
        self.assertEqual(first["loop_body"], ["Nimbus | port | 8000 | -", "Nimbus | port | 8001 | -"])
        self.assertEqual((a.prompt_tokens, a.completion_tokens), (14, first["out_tokens"] + 1))

    def test_a_loop_on_every_rung_ends_repeat_with_the_lines_before_it(self):
        fake = Fake(looping(), looping(), looping())
        a = ask(fake, valid=lambda t: "|" in t)
        self.assertEqual(fake.temps(), [0.0, 0.5, 1.0])
        self.assertEqual((a.cut, a.rung, a.recovered, len(a.tries)), ("repeat", 2, False, 3))
        self.assertEqual(a.text, "Nimbus | host | box-7 | -")   # the last try, its loop left out

    def test_a_configured_first_rung_climbs_from_there(self):
        fake = Fake(looping(), looping())
        a = ask(fake, first=0.6)
        self.assertEqual(fake.temps(), [0.6, 1.0])
        self.assertEqual((a.cut, len(a.tries)), ("repeat", 2))

    def test_a_cut_off_answer_returns_at_once(self):
        fake = Fake(({"content": "a | b | c | -\nd | e"}, {"finish_reason": "length", "completion_tokens": 100}),
                    done("unused"))
        a = ask(fake, max_tokens=100, valid=lambda t: "|" in t)
        self.assertEqual((a.cut, len(a.tries), len(fake.calls)), ("length", 1, 1))
        self.assertEqual(a.text, "a | b | c | -\nd | e")         # the caller drops the fragment and splits

    def test_a_cut_off_answer_with_no_valid_line_climbs_like_a_format_failure(self):
        prose = ({"content": "Let me look at this note carefully. " * 20}, {"finish_reason": "length", "completion_tokens": 100})
        fake = Fake(prose, done("Nimbus | port | 8000 | -"))
        a = ask(fake, max_tokens=100, valid=lambda t: "|" in t)
        self.assertEqual((fake.temps(), a.cut, a.recovered), ([0.0, 0.5], "", True))

    def test_no_answer_after_reasoning_is_a_thinking_cut(self):
        fake = Fake(({"content": ""}, {"finish_reason": "stop", "completion_tokens": 60,
                                       "completion_tokens_details": {"reasoning_tokens": 60}}))
        a = ask(fake, valid=lambda t: "|" in t)
        self.assertEqual((a.cut, len(a.tries), a.reasoning_tokens, a.tries[0]["reasoning"]), ("thinking", 1, 60, 60))

    def test_an_answer_out_of_format_is_asked_again_hotter(self):
        prose = done("I could not find any facts worth keeping in this note.")
        fake = Fake(prose, prose, prose)
        a = ask(fake, valid=lambda t: "|" in t)
        self.assertEqual(fake.temps(), [0.0, 0.5, 1.0])
        self.assertEqual((a.cut, a.recovered, a.text), ("format", False, "I could not find any facts worth keeping in this note."))
        fake = Fake(prose, done("Nimbus | port | 8000 | -"))
        a = ask(fake, valid=lambda t: "|" in t)
        self.assertEqual((a.cut, a.recovered, len(a.tries)), ("", True, 2))

    def test_an_empty_answer_is_not_a_format_failure(self):
        fake = Fake(done(""))                                     # "if nothing is missing, write nothing"
        a = ask(fake, valid=lambda t: "|" in t)
        self.assertEqual((a.cut, a.text, len(a.tries)), ("", "", 1))

    def test_the_thought_is_left_out(self):
        a = ask(Fake(done("<think>which one?</think>\nNimbus | port | 8000 | -")), valid=lambda t: "|" in t)
        self.assertEqual((a.text.strip(), a.cut), ("Nimbus | port | 8000 | -", ""))

    def test_the_rung_goes_out_with_the_callers_fields_and_no_guard_means_no_stop(self):
        fake = Fake(done("ok"))
        ask(fake, extra={"reasoning_effort": "none", "temperature": 0.9}, guard=False)
        self.assertEqual(fake.calls[0]["extra"], {"reasoning_effort": "none", "temperature": 0.0})
        self.assertIsNone(fake.calls[0]["stop"])

    def test_a_request_error_propagates(self):
        with self.assertRaises(OSError):
            ask(Fake(OSError("down")))


class RepeatGuardTest(unittest.TestCase):
    def test_repeating_counts_finished_lines_only(self):
        self.assertEqual(llm.repeating("a | b | c | -\n" * 3 + "a | b | c"), "")
        self.assertEqual(llm.repeating("a | b | c | -\n" * 4), "repeat")
        self.assertEqual(llm.repeating(""), "")

    def test_unloop_drops_the_whole_cycle_and_keeps_the_rest(self):
        text = "x | y | z | -\n" + "a | 1 | 2 | -\n b | 3 | 4 | -\n" * 3 + "a | 1 | 2 | -\nb | 3"
        self.assertEqual(llm.unloop(text), ("x | y | z | -", 4, ["a | 1 | 2 | -", "b | 3 | 4 | -"]))
        self.assertEqual(llm.unloop("only a fragment"), ("", 0, []))


def sse(pieces, finish="stop"):
    out = [b"data: " + json.dumps({"choices": [{"delta": {"content": p}}]}).encode() + b"\n" for p in pieces]
    return out + [b"data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": finish}]}).encode() + b"\n",
                  b"data: [DONE]\n"]


class Stream(list):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class RequestTest(unittest.TestCase):
    """What actually goes out, through chat() and a stand-in server."""

    def setUp(self):
        self.saved = dict(llm.ENDPOINT)
        self.addCleanup(lambda: (llm.ENDPOINT.clear(), llm.ENDPOINT.update(self.saved)))
        llm.ENDPOINT.update(url="http://x/v1", model="m", extra={}, thinking="server", thinking_off={})
        self.real = urllib.request.urlopen
        self.addCleanup(setattr, urllib.request, "urlopen", self.real)
        self.bodies = []

    def serve(self, *responses):
        responses = list(responses)

        def fake(req, timeout=0):
            self.bodies.append(json.loads(req.data))
            r = responses.pop(0)
            return Stream(r) if isinstance(r, list) else io.BytesIO(json.dumps(r).encode())
        urllib.request.urlopen = fake

    def test_no_temperature_sends_none(self):
        self.serve({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}})
        llm.chat([{"role": "user", "content": "x"}], extra={"temperature": None})
        self.assertNotIn("temperature", self.bodies[0])

    def test_the_rung_wins_over_the_configured_temperature(self):
        llm.ENDPOINT["extra"] = {"temperature": 0.7, "top_k": 20}
        self.serve(sse(["a | b | c | -"]))
        llm.ask([{"role": "user", "content": "x"}], max_tokens=100)
        self.assertEqual((self.bodies[0]["temperature"], self.bodies[0]["top_k"], self.bodies[0]["stream"]), (0.0, 20, True))

    def test_a_streamed_loop_is_stopped_and_asked_again(self):
        self.serve(sse(LOOP + ["never read\n"] * 20), sse(["Nimbus | port | 8000 | -"]))
        a = llm.ask([{"role": "user", "content": "x"}], max_tokens=100, valid=lambda t: "|" in t)
        self.assertEqual([b["temperature"] for b in self.bodies], [0.0, 0.5])
        self.assertEqual((a.text, a.cut, a.recovered, a.tries[0]["finish"]), ("Nimbus | port | 8000 | -", "", True, "repeat"))

    def test_an_error_event_mid_stream_is_asked_again_not_read_as_an_answer(self):
        # 10-21: the brain restarted mid-import; the event after the 200 was read as a short answer.
        failed = [b"data: " + json.dumps({"choices": [{"delta": {"content": "Nimbus | port"}}]}).encode() + b"\n",
                  b"data: " + json.dumps({"error": {"message": "rank 0 failed; both ranks are restarting"}}).encode() + b"\n"]
        self.serve(failed, sse(["Nimbus | port | 8000 | -"]))
        with mock.patch.object(llm.time, "sleep"):
            msg, usage = llm.chat([{"role": "user", "content": "x"}], stop=llm.repeating)
        self.assertEqual((msg["content"], usage["finish_reason"], len(self.bodies)), ("Nimbus | port | 8000 | -", "stop", 2))

    def test_vllms_error_object_and_a_cut_stream_are_errors_too(self):
        vllm = [b"data: " + json.dumps({"object": "error", "message": "out of memory"}).encode() + b"\n"]
        cut = [b"data: " + json.dumps({"choices": [{"delta": {"content": "half an ans"}}]}).encode() + b"\n"]
        for bad in (vllm, cut):
            self.serve(bad, bad, bad)
            with mock.patch.object(llm.time, "sleep"), self.assertRaises(llm.StreamError):
                llm.chat([{"role": "user", "content": "x"}], stop=llm.repeating)

    def test_the_cli_asker_still_works_through_chat(self):
        self.serve(sse(["1. another\n", "2 same"]))
        got = items.ask(ItemsAskTest.EV, llm.chat)
        self.assertEqual(got, {items.key(ItemsAskTest.EV[0]): "another", items.key(ItemsAskTest.EV[1]): "same"})
        self.assertTrue(self.bodies[0]["stream"])


class ItemsAskTest(unittest.TestCase):
    EV = [{"thing": "Sam", "label": "Fundraising amount", "old": "$1,000", "old_since": "2023-03-10", "old_ev": "bake sale",
           "new": "$250", "new_since": "2023-03-20", "new_ev": "Run for Hunger"},
          {"thing": "Sam", "label": "Bike count", "old": "3", "old_since": "2023-03-10", "old_ev": "I have 3 bikes",
           "new": "4", "new_since": "2023-03-20", "new_ev": "my fourth bike"}]

    def setUp(self):
        p = mock.patch.dict(llm.ENDPOINT, {"thinking": "server", "thinking_off": {}})
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def size(call):
        return len(re.findall(r"(?m)^\d+\. Sam, ", call["messages"][0]["content"]))

    def test_a_looping_batch_is_asked_again_one_rung_up(self):
        stats = {}
        fake = Fake(looping(["1 same\n", "2 another\n"] * 5), done("1 another\n2 same"))
        got = items.ask(self.EV, fake, stats=stats)
        self.assertEqual(fake.temps(), [0.0, 0.5])                 # a classification: the ladder starts at 0
        self.assertEqual(got, {items.key(self.EV[0]): "another", items.key(self.EV[1]): "same"})
        self.assertEqual(stats, {"recovered": 1})

    def test_a_cut_off_batch_is_split_not_climbed(self):
        stats = {}
        cut = ({"content": ""}, {"completion_tokens": 8192, "finish_reason": "length"})
        fake = Fake(cut, done("1 another"), done("1 same"))
        got = items.ask(self.EV, fake, stats=stats)
        self.assertEqual(([self.size(c) for c in fake.calls], fake.temps()), ([2, 1, 1], [0.0, 0.0, 0.0]))
        self.assertEqual(got, {items.key(self.EV[0]): "another", items.key(self.EV[1]): "same"})
        self.assertEqual(stats, {"cut": 1})

    def test_unreadable_on_every_rung_splits_then_is_lost(self):
        stats = {}
        fake = Fake(*[done("Both look fine to me.")] * 9)
        self.assertEqual(items.ask(self.EV, fake, stats=stats), {})
        self.assertEqual([self.size(c) for c in fake.calls], [2] * 3 + [1] * 6)
        self.assertEqual(stats, {"format": 3, "lost": 2})

    def test_a_single_event_looping_on_every_rung_is_lost(self):
        stats = {}
        fake = Fake(*[looping(["1 same\n"] * 5)] * 3)
        self.assertEqual(items.ask(self.EV[:1], fake, stats=stats), {})
        self.assertEqual((fake.temps(), stats), ([0.0, 0.5, 1.0], {"repeat": 1, "lost": 1}))


if __name__ == "__main__":
    unittest.main()
