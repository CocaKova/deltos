"""Seen (deltos/seen.py): memory reads what the agent's tools return. When a tool call
looked at a thing (named in the call, or the place the tools run) and its output shows
another number for an entry memory holds, the agent is told what memory holds, once
per chat. An output that agrees, is about another thing, or names things only in a
listing gets nothing. Off by default."""
import json
import tempfile
import time
import unittest
from pathlib import Path

from deltos import Memory
from deltos.config import Config
from deltos.recall import Recall
from deltos.seen import note, said, seen, shows
from deltos.store import Store

SUMMARY = "OS release: Ubuntu 24.04.1 LTS\nPython version: 3.12.3\nCPU count: 8\n"


class SeenTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        s = Store(self.root, today="2026-06-01")
        s.name("Box-9", ["This machine"], "machine")
        s.remember("Box-9", "OS", "Ubuntu 22.04", since="2026-03-01")
        s.remember("Box-9", "Python version", "3.9.2", since="2026-06-01")
        s.remember("Box-9", "owner", "Sam", since="2026-06-01")
        s.name("Box-4", [], "machine")
        s.remember("Box-4", "Python version", "3.11.4", since="2026-06-01")
        s.name("Orchard", [], "service")
        s.remember("Orchard", "version", "3.5.0", since="2026-06-01")
        s.remember("Orchard", "port", "8443", since="2026-06-01")
        s.remember("Orchard", "host", "vps-2", since="2026-06-01")
        s.remember("Orchard", "status", "live", since="2026-06-01", source="Ava", check="systemctl status orchard")
        s.remember("Orchard", "price", "12 dollars a seat", since="2026-06-01")
        s.remember("Orchard", "size", "3 replicas", since="2026-06-01")
        s.name("Lyrin", [], "app")
        s.remember("Lyrin", "version", "2.1.0", since="2026-06-01")

    def recall(self):
        r = Recall.open(self.root, "")
        r.today, r.doubt_days = "2026-11-11", 21
        return r

    def got(self, call, output, **kw):
        return [(h.thing, h.label.lower(), h.by) for h in seen(self.recall(), call, output, **kw)]

    # -- the place the tools run ---------------------------------------------------

    def test_a_summary_of_this_machine_shows_two_numbers_that_moved(self):
        got = self.got("python3 summary.py", SUMMARY, here="This machine")
        self.assertEqual(sorted(got), [("Box-9", "os", "value"), ("Box-9", "python version", "label")])

    def test_no_place_no_note(self):
        self.assertEqual(self.got("python3 summary.py", SUMMARY), [])

    def test_an_output_that_agrees_gets_nothing(self):
        same = "OS release: Ubuntu 22.04.5 LTS\nPython version: 3.9.2\n"
        self.assertEqual(self.got("python3 summary.py", same, here="Box-9"), [])
        # a number cut short is the same number
        self.assertEqual(self.got("python3 -V", "Python 3.9", here="Box-9"), [])

    def test_a_call_that_names_another_machine_is_not_about_this_one(self):
        self.assertEqual(self.got("ssh box-4 python3 --version", "Python 3.12.3", here="Box-9"),
                         [("Box-4", "python version", "label")])
        self.assertEqual(self.got("ssh box-4 python3 --version", "Python 3.11.4", here="Box-9"), [])

    def test_a_path_is_a_look_too(self):
        self.assertEqual(self.got("ls /usr/lib", "python3.12\npython3\n", here="Box-9"),
                         [("Box-9", "python version", "label")])

    def test_a_script_named_like_the_label_with_no_version_is_nothing(self):
        out = "2026-11-11 10:03:22 python script.py took 3.2 s\nPID 4411 python3 worker.py 12.5% cpu\n"
        self.assertEqual(self.got("ps aux", out, here="Box-9"), [])

    # -- a thing the call names ----------------------------------------------------

    def test_a_bare_answer_to_a_call_that_asked(self):
        self.assertEqual(self.got("orchard --version", "3.6.0\n"), [("Orchard", "version", "call")])
        self.assertEqual(self.got("orchard --version", "v3.5.0\n"), [])

    def test_the_label_on_the_line(self):
        out = json.dumps({"output": '{"version": "3.6.0", "port": 8443, "uptime": 86400}', "exit_code": 0})
        self.assertEqual(self.got({"command": "curl -s https://orchard.example/health"}, out),
                         [("Orchard", "version", "label")])
        self.assertEqual(self.got("journalctl -u orchard -n 3", "orchard[812]: listening on port 9000\n"),
                         [("Orchard", "port", "label")])

    def test_the_values_own_word_with_another_number(self):
        self.assertEqual(self.got("orchard status", "deployed on vps-3 since Monday\n"), [("Orchard", "host", "value")])
        self.assertEqual(self.got("orchard ps", "running 5 replicas\n"), [("Orchard", "size", "value")])

    def test_the_things_name_with_another_number_is_its_version(self):
        self.assertEqual(self.got("orchard status", "orchard 3.6.0\nworkers: 4 busy\n"), [("Orchard", "version", "name")])
        self.assertEqual(self.got("curl -sI https://orchard.example", "server: orchard/3.6.0\n"),
                         [("Orchard", "version", "name")])
        self.assertEqual(self.got("orchard status", "orchard v3.5.0\nlyrin 2.2.0\n"), [])   # agrees; Lyrin is not named
        self.assertEqual(self.got("orchard status", "orchard 4 workers, 12 queued\n"), [])   # not a version's shape
        self.assertEqual(self.got("ls /opt", "orchard 3.6.0\n", here="Box-9"), [])            # named only in the output

    def test_a_plain_word_name_in_a_command_names_the_thing(self):
        s = Store(self.root, today="2026-06-01")
        s.name("Gateway", [], "service")
        s.remember("Gateway", "port", "8787", since="2026-06-01")
        self.assertEqual(self.recall().name_spans("restart the gateway"), [])   # a sentence: only by its capital
        self.assertEqual(self.got("gateway status", "listening on port 9119\n"), [("Gateway", "port", "label")])
        self.assertEqual(self.got("tail -1 gateway.log", "listening on port 8787\n"), [])

    def test_a_decision_is_not_something_a_look_shows(self):
        self.assertEqual(self.got("orchard billing", "plan: 15 dollars per seat\n"), [])

    def test_numbers_beside_other_words_are_nothing(self):
        out = "2026-11-11 10:03:22 GET /health 200 12ms\n2026-11-11 10:03:25 GET /seats 200 9ms\n"
        self.assertEqual(self.got("tail -2 /var/log/orchard/app.log", out), [])

    def test_a_listing_names_things_only_in_its_output(self):
        out = "NAME     VERSION\norchard  version 3.6.0\nlyrin    version 2.2.0\n"
        self.assertEqual(self.got("docker ps", out, here="Box-9"), [])

    def test_the_tools_own_words_name_nothing(self):
        # "version" as an argument's key, "exit_code" as a result's
        out = json.dumps({"output": "ok", "exit_code": 3.6})
        self.assertEqual(self.got({"version": "orchard"}, out), [])

    # -- the entry's own check -----------------------------------------------------

    def test_a_call_that_is_the_entrys_check(self):
        self.assertEqual(self.got("systemctl status orchard", "orchard.service\n   Active: inactive (dead)\n"),
                         [("Orchard", "status", "check")])
        self.assertEqual(self.got("systemctl status orchard", "orchard.service\n   Active: live\n"), [])

    # -- told ----------------------------------------------------------------------

    def test_once_per_chat(self):
        shown: set = set()
        self.assertEqual(len(self.got("python3 summary.py", SUMMARY, here="Box-9", shown=shown)), 2)
        self.assertEqual(self.got("python3 summary.py", SUMMARY, here="Box-9", shown=shown), [])
        self.assertEqual(len(self.got("python3 summary.py", SUMMARY, here="Box-9", shown=set())), 2)

    def test_the_note_says_what_memory_holds_and_how_old(self):
        text = note(seen(self.recall(), "python3 summary.py", SUMMARY, here="Box-9"), checks=True)
        self.assertTrue(text.startswith("[memory] "))
        self.assertIn("- Box-9 · Python version: 3.9.2 (since 2026-06-01; not confirmed in 5 months)", text)
        self.assertIn("- Box-9 · OS: Ubuntu 22.04 (since 2026-03-01", text)
        self.assertIn("`remember` the new value, as short as the one above (check = how you looked)", text)
        self.assertEqual(note([]), "")

    def test_a_forgotten_or_retired_entry_is_not_held(self):
        s = Store(self.root, today="2026-11-01")
        s.forget("Box-9", "Python version", "test", source="Sam")
        s.remember("Orchard", "version", "retired", since="2026-11-01")
        self.assertEqual(self.got("python3 -V", "Python 3.12.3", here="Box-9"), [])
        self.assertEqual(self.got("orchard --version", "3.6.0"), [])

    # -- reading -------------------------------------------------------------------

    def test_said(self):
        self.assertEqual(said('{"output": "Python 3.12.3", "exit_code": 0, "error": null}'),
                         "output: Python 3.12.3\nexit_code: 0")
        self.assertEqual(said('{"output": "a\\nb"}'), "a\nb")
        self.assertEqual(said({"command": "python3 -V", "timeout": 30}, keys=False), "python3 -V")
        self.assertEqual(said("[not json]"), "[not json]")

    def test_shows(self):
        self.assertTrue(shows("python 3.9.2 on linux", "3.9.2"))
        self.assertTrue(shows("tag v3.5.0", "3.5.0"))
        self.assertFalse(shows("python 3.9.20", "3.9.2"))
        self.assertFalse(shows("python 13.9.2", "3.9.2"))

    def test_a_long_output_is_read_fast(self):
        out = "\n".join(f"line {i}: orchard worker {i} handled {i * 7} requests in {i}.{i} s" for i in range(400))
        r = self.recall()
        seen(r, "orchard stats", out)
        t = time.monotonic()
        seen(r, "orchard stats --all", out)
        self.assertLess(time.monotonic() - t, 0.25)

    # -- Memory: the switch, the note, the log, the instructions ---------------------

    def memory(self, **cfg):
        return Memory(config=Config(root=self.root, recall={"doubt_days": 21}, **cfg), today="2026-11-11")

    def test_off_by_default(self):
        m = self.memory()
        self.assertEqual(m.seen("python3 summary.py", SUMMARY, here="Box-9"), "")
        self.assertNotIn("[memory]", m.instructions())
        self.assertFalse((self.root / "seen").exists())

    def test_on_gives_the_note_and_logs_no_text(self):
        m = self.memory(seen={"on": True}, checks={"on": True})
        text = m.seen({"command": "python3 summary.py"}, json.dumps({"output": SUMMARY, "exit_code": 0}),
                      here="Box-9", shown=set(), session="chat-1")
        self.assertIn("- Box-9 · Python version: 3.9.2", text)
        (f,) = (self.root / "seen").glob("*.jsonl")
        (row,) = [json.loads(x) for x in f.read_text().splitlines()]
        self.assertEqual(sorted((h["thing"], h["label"]) for h in row["held"]),
                         [("Box-9", "OS"), ("Box-9", "Python version")])
        self.assertNotIn("3.9.2", f.read_text())
        self.assertNotIn("chat-1", f.read_text())
        self.assertIn("A tool result may end with a `[memory]` note", m.instructions())

    def test_here_from_the_settings_wins(self):
        m = self.memory(seen={"on": True, "here": "This machine"})
        self.assertIn("Box-9 · OS", m.seen("python3 summary.py", SUMMARY, here="some-host"))

    def test_check_limits_are_their_own_switch(self):
        self.assertNotIn("use it as it stands", self.memory(checks={"on": True}).instructions())
        self.assertIn("use it as it stands", self.memory(checks={"on": True, "limits": True}).instructions())
        self.assertNotIn("use it as it stands", self.memory(checks={"limits": True}).instructions())


if __name__ == "__main__":
    unittest.main()
