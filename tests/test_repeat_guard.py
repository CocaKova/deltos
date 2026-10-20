"""The repeat guard: an import answer is streamed and stopped as soon as it says the same lines
again (GLM at temperature 0, 10-19: answers looped on to the 8,192-token cap, minutes each),
its loop is left out, and the text is asked again in halves like any cut-off answer."""
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import deltos.llm as llm
import deltos.sources.notes as notes
from deltos.store import Store


def sse(pieces, finish="stop", usage=None):
    out = [b"data: " + json.dumps({"choices": [{"delta": {"content": p}}]}).encode() + b"\n" for p in pieces]
    out.append(b"data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": finish}]}).encode() + b"\n")
    if usage:
        out.append(b"data: " + json.dumps({"choices": [], "usage": usage}).encode() + b"\n")
    return out + [b"data: [DONE]\n"]


class Stream:
    """A streamed response that counts how much of it was read."""
    def __init__(self, lines):
        self.lines, self.read = lines, 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        for ln in self.lines:
            self.read += 1
            yield ln


class StreamTest(unittest.TestCase):
    def setUp(self):
        self.saved = dict(llm.ENDPOINT)
        self.addCleanup(lambda: (llm.ENDPOINT.clear(), llm.ENDPOINT.update(self.saved)))
        llm.ENDPOINT.update(url="http://x/v1", model="m", extra={})
        self.real = urllib.request.urlopen
        self.addCleanup(setattr, urllib.request, "urlopen", self.real)
        self.bodies, self.streams = [], []

    def serve(self, *responses):
        responses = list(responses)

        def fake(req, timeout=0):
            self.bodies.append(json.loads(req.data))
            r = responses.pop(0)
            if r == "400":
                raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(b"{}"))
            self.streams.append(Stream(r))
            return self.streams[-1]
        urllib.request.urlopen = fake

    def test_a_stop_ends_the_answer_and_reads_no_further(self):
        loop = ["Nimbus | port | 8000 | -\n"] * 6
        self.serve(sse(["Nimbus | host | box-7 | -\n"] + loop + ["x\n"] * 50))
        msg, usage = llm.chat([{"role": "user", "content": "x"}], stop=notes._repeating)
        self.assertEqual(usage["finish_reason"], "repeat")
        self.assertEqual(msg["content"].count("8000"), notes.REPEAT_STOP)
        self.assertLess(self.streams[0].read, 10)                  # the rest was never read
        self.assertTrue(self.bodies[0]["stream"])
        self.assertEqual(llm.cut_off(msg, usage, 8192), "repeat")

    def test_a_finished_stream_is_the_whole_answer_with_the_server_usage(self):
        self.serve(sse(["a | b ", "| c | -\n", "d | e | f | -"], usage={"completion_tokens": 9, "prompt_tokens": 3}))
        msg, usage = llm.chat([{"role": "user", "content": "x"}], stop=notes._repeating)
        self.assertEqual(msg["content"], "a | b | c | -\nd | e | f | -")
        self.assertEqual((usage["finish_reason"], usage["completion_tokens"]), ("stop", 9))

    def test_a_server_that_refuses_stream_options_is_asked_without(self):
        self.serve("400", sse(["a | b | c | -\n"], finish="length"))
        msg, usage = llm.chat([{"role": "user", "content": "x"}], stop=notes._repeating)
        self.assertNotIn("stream_options", self.bodies[1])
        self.assertEqual((usage["finish_reason"], usage["completion_tokens"]), ("length", 1))

    def test_no_stop_no_stream(self):
        urllib.request.urlopen = lambda req, timeout=0: (self.bodies.append(json.loads(req.data)) or io.BytesIO(
            b'{"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}'))
        llm.chat([{"role": "user", "content": "x"}])
        self.assertNotIn("stream", self.bodies[0])


class GuardTest(unittest.TestCase):
    def test_repeating_counts_finished_lines_only(self):
        self.assertEqual(notes._repeating("a | b | c | -\n" * 3 + "a | b | c"), "")
        self.assertEqual(notes._repeating("a | b | c | -\n" * 4), "repeat")

    def test_unloop_drops_the_whole_cycle_and_keeps_the_rest(self):
        text = "N | host | box-7 | -\n" + "N | a | 1 | -\nN | b | 2 | -\n" * 3 + "N | a | 1 | -\nN | b"
        usage = {"finish_reason": "repeat"}
        msg = notes._unloop({"content": text}, usage)
        self.assertEqual(msg["content"], "N | host | box-7 | -")
        self.assertEqual((usage["loop"], usage["loop_body"]), (4, ["N | a | 1 | -", "N | b | 2 | -"]))
        self.assertEqual(notes._unloop({"content": "x"}, {"finish_reason": "stop"}), {"content": "x"})


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.root = Path(self.d.name)
        Store(self.root).name("Nimbus", ["nb"], "app")
        self.real = notes.chat
        self.addCleanup(setattr, notes, "chat", self.real)

    def test_a_stopped_answer_keeps_what_came_before_and_is_asked_again_in_halves(self):
        text = "--- memory/a.md\n" + "".join(f"- Nimbus line {i}: Dana moved it to box-7 today\n" for i in range(80))
        calls = []

        def chat(messages, max_tokens=0, stop=None, **kw):
            calls.append(messages[-1]["content"])
            if len(calls) == 1:   # the whole text: stuck on one line, stopped by the guard
                out = "Nimbus | host | box-7 | 2026-10-04\n" + "Nimbus | setting result | 7.3 s | 2026-10-04\n" * 6
                return self._stop(out, stop)
            return {"content": f"Nimbus | owner{len(calls)} | Dana | 2026-10-04"}, {"finish_reason": "stop"}
        notes.chat = chat
        st = notes.Migrator(self.root).session("memory@x", "2026-10-04", text, git_mode=True)
        self.assertEqual((st["cut_off"], st["loop"]), ("repeat", notes.REPEAT_STOP))
        self.assertEqual(st["loop_body"], ["Nimbus | setting result | 7.3 s | 2026-10-04"])
        self.assertEqual(len(st["subs"]), 2)                       # asked again in halves
        attrs = Store(self.root).find("Nimbus").attrs
        self.assertEqual(attrs["host"].value, "box-7")              # the line before the loop counts
        self.assertNotIn("result", " ".join(attrs))
        self.assertIn("owner2", attrs)
        self.assertEqual(st.get("lost", 0), 0)
        # a replay of the logged session applies the same, with no model
        d2 = Path(tempfile.mkdtemp())
        Store(d2).name("Nimbus", ["nb"], "app")
        rp = notes.Migrator(d2).session("memory@x", "2026-10-04", "", git_mode=True, replay=json.loads(json.dumps(st)))
        self.assertEqual((rp["cut_off"], rp["loop"], rp["facts"]), ("repeat", st["loop"], st["facts"]))

    @staticmethod
    def _stop(out, stop):
        """What llm.chat gives back when the guard stops a streamed answer."""
        so_far = ""
        for ln in out.splitlines(keepends=True):
            so_far += ln
            if stop(so_far):
                return {"content": so_far}, {"finish_reason": stop(so_far), "completion_tokens": len(so_far) // 4}
        return {"content": so_far}, {"finish_reason": "stop"}


if __name__ == "__main__":
    unittest.main()
