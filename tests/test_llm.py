"""deltos asks for plain answers on any server: the thinking posture is config,
the way to say it is probed once and remembered, and nothing else non-standard goes out."""
import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from pathlib import Path

import deltos.llm as llm
from deltos.config import Config


class RequestFieldsTest(unittest.TestCase):
    def setUp(self):
        self.saved = dict(llm.ENDPOINT)
        self.addCleanup(lambda: (llm.ENDPOINT.clear(), llm.ENDPOINT.update(self.saved)))
        self.real = urllib.request.urlopen
        self.addCleanup(setattr, urllib.request, "urlopen", self.real)
        self.real_cache = llm.PROBE_CACHE
        llm.PROBE_CACHE = Path(tempfile.mkdtemp()) / "llm-thinking.json"
        self.addCleanup(setattr, llm, "PROBE_CACHE", self.real_cache)
        llm._probed.clear()
        self.addCleanup(llm._probed.clear)
        for k in ("extra", "thinking_off", "thinking_on"):
            llm.ENDPOINT[k] = {}
        llm.ENDPOINT.update(thinking="off", url="http://x/v1", model="m")
        self.bodies = []
        self.reasons = []   # reasoning tokens per answer (None: not reported); "400": refuse the request

    def serve(self, reasons):
        self.reasons = list(reasons)

        def fake(req, timeout=0):
            body = json.loads(req.data)
            self.bodies.append(body)
            r = self.reasons.pop(0) if self.reasons else 0
            if r == "400":
                raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(b"{}"))
            usage = {} if r is None else {"completion_tokens_details": {"reasoning_tokens": r}}
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                                          "usage": usage}).encode())
        urllib.request.urlopen = fake

    def test_off_is_probed_once_and_remembered(self):
        self.serve([0, 0, 0])
        self.assertEqual(llm.thinking(False), llm.THINKING_OFF_STYLES[0])
        self.assertEqual(llm.thinking(False), llm.THINKING_OFF_STYLES[0])
        self.assertEqual(len(self.bodies), 1)                      # one probe request, then memory
        llm._probed.clear()
        self.assertEqual(llm.thinking(False), llm.THINKING_OFF_STYLES[0])
        self.assertEqual(len(self.bodies), 1)                      # the disk remembers across runs
        llm.chat([{"role": "user", "content": "x"}], extra=llm.thinking(False))
        self.assertEqual(self.bodies[-1]["chat_template_kwargs"], {"enable_thinking": False})

    def test_a_refused_or_ignored_style_falls_through(self):
        self.serve(["400", 812, 0])                                # refused, ignored (still thought), works
        self.assertEqual(llm.thinking(False), {"reasoning_effort": "none"})

    def test_no_style_works_said_once_and_budgeted(self):
        self.serve([500, 500, 500])
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(llm.thinking(False), {})
            self.assertEqual(llm.thinking(False), {})
        self.assertEqual(err.getvalue().count("kept thinking"), 1)
        self.assertTrue(llm.thinking_stays_on())

    def test_server_posture_sends_nothing(self):
        llm.ENDPOINT["thinking"] = "server"
        self.serve([])
        llm.chat([{"role": "user", "content": "x"}], extra=llm.thinking(False))
        self.assertEqual(set(self.bodies[0]), {"model", "messages", "temperature", "max_tokens"})

    def test_fields_come_from_deltos_toml(self):
        d = tempfile.mkdtemp()
        Path(d, "deltos.toml").write_text(
            '[llm]\nurl = "http://x/v1"\nmodel = "m"\nthinking = "off"\n'
            'thinking_off = { chat_template_kwargs = { enable_thinking = false } }\n'
            'thinking_on = { reasoning_effort = "high" }\nextra = { top_k = 20 }\n')
        os.environ.pop("DELTOS_CONFIG", None)
        llm.configure(**Config.load(d).llm)
        self.serve([])
        llm.chat([{"role": "user", "content": "x"}], extra=llm.thinking(False))
        llm.chat([{"role": "user", "content": "x"}], extra=llm.thinking(True))
        self.assertEqual(len(self.bodies), 2)                      # configured: no probe
        self.assertEqual(self.bodies[0]["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(self.bodies[0]["top_k"], 20)
        self.assertEqual(self.bodies[1]["reasoning_effort"], "high")
        self.assertNotIn("chat_template_kwargs", self.bodies[1])


if __name__ == "__main__":
    unittest.main()
