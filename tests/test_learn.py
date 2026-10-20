"""Learning as it goes (deltos/learn.py): when it is on, the idle gate, a sweep's units
into a staged run (stubbed here: deltos.runs and deltos.health are their own packages),
errors and a busy agent stopping it, and the watermark that makes a second sweep empty."""
import contextlib
import json
import shutil
import sqlite3
import sys
import tempfile
import time
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import deltos
import deltos.llm as llm
import deltos.sources.notes as notes
from deltos import learn
from deltos.config import Config
from deltos.store import Store

LLM = {"url": "http://127.0.0.1:9/v1", "model": "stub", "thinking": "server"}   # never reached: notes.chat is stubbed


def make_home(home: Path, chats: dict[str, list[tuple]], ended: float | None = None) -> Path:
    """chats: session id -> [(role, content, timestamp)]."""
    home.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(home / "state.db")
    db.execute("create table if not exists sessions (id text primary key, source text, started_at real, ended_at real)")
    db.execute("create table if not exists messages (id integer primary key autoincrement, session_id text, role text, "
               "content text, tool_calls text, timestamp real)")
    for sid, msgs in chats.items():
        db.execute("insert into sessions values (?, 'cli', ?, ?)", (sid, msgs[0][2], ended or msgs[-1][2] + 1))
        db.executemany("insert into messages (session_id, role, content, tool_calls, timestamp) values (?, ?, ?, null, ?)",
                       [(sid, r, c, t) for r, c, t in msgs])
    db.commit()
    db.close()
    return home


def chat_of(color: str, t: float) -> list[tuple]:
    return [("user", f"I painted the front door {color} today, and the shed too", t),
            ("assistant", f"A {color} door sounds great.", t + 30)]


def jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines()]


def stub_runs(calls: list, tmp: Path):
    """deltos.runs as W4 specifies it, reduced to what a sweep needs: stage a copy, run the work
    on it, and promote by appending the stage's new log lines to live."""
    def execute(root, kind, work, *, label, say=print, owner="", fingerprint=""):
        root = Path(root)
        stage = Path(tempfile.mkdtemp(dir=tmp)) / "stage"
        stage.mkdir()
        if (root / "notes").is_dir():
            shutil.copytree(root / "notes", stage / "notes")
        counts = {}
        for f in ("migrate-log.jsonl", "migrate-errors.jsonl", "migrate-texts.jsonl"):
            if (root / f).exists():
                shutil.copy(root / f, stage / f)
            counts[f] = len((stage / f).read_text().splitlines()) if (stage / f).exists() else 0
        work(stage)
        calls.append({"root": root, "kind": kind, "label": label, "owner": owner, "fingerprint": fingerprint,
                      "stage": stage})
        for f, n in counts.items():
            new = (stage / f).read_text().splitlines()[n:] if (stage / f).exists() else []
            if new:
                with open(root / f, "a") as fh:
                    fh.write("".join(x + "\n" for x in new))
        return 0
    return types.SimpleNamespace(execute=execute)


def stubbed(runs) -> contextlib.ExitStack:
    """deltos.runs and deltos.health stubbed, as modules and as the package's attributes: once
    the real ones are imported, `from . import health, runs` reads the attribute."""
    health = types.SimpleNamespace(fingerprint=lambda p: "fp:" + json.dumps(p, sort_keys=True))
    st = contextlib.ExitStack()
    st.enter_context(mock.patch.dict(sys.modules, {"deltos.runs": runs, "deltos.health": health}))
    for name, mod in (("runs", runs), ("health", health)):
        st.enter_context(mock.patch.object(deltos, name, mod, create=True))
    return st


class LearnTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.tmp = Path(d.name)
        self.root = self.tmp / "store"
        self.root.mkdir()
        Store(self.root).name("Ava", [], "person")
        self.home = self.tmp / "hermes"
        self.now = time.time()
        self.addCleanup(llm.ENDPOINT.update, dict(llm.ENDPOINT))
        self.real = notes.chat
        self.addCleanup(setattr, notes, "chat", self.real)
        self.asked = []
        self.calls = []

    def cfg(self, **kw) -> Config:
        base = dict(root=self.root, owner="Ava", llm=dict(LLM), sessions={"hermes": self.home})
        return Config(**{**base, **kw})

    def write_toml(self, extra: str = "") -> None:
        (self.root / "deltos.toml").write_text(
            f'owner = "Ava"\n[llm]\nurl = "{LLM["url"]}"\nmodel = "stub"\nthinking = "server"\n'
            f'[sessions]\nhermes = "{self.home}"\n{extra}')

    def serve(self, fail_on: int = 0, error=None):
        """Each request answers with the door colour its chat states; request number fail_on raises
        (error, by default the model being down)."""
        def chat(messages, max_tokens=0, **kw):
            self.asked.append(messages)
            if len(self.asked) == fail_on:
                raise error or ConnectionRefusedError("model down")
            text = messages[-1]["content"]
            color = text.split("front door ")[1].split()[0]
            return {"content": f"Ava | front door colour | {color} | 2026-10-19"}, \
                   {"prompt_tokens": 10, "completion_tokens": 5, "finish_reason": "stop"}
        notes.chat = chat

    def units(self, n=2):
        make_home(self.home, {f"s{i}": chat_of(c, self.now - (100 - i) * 60) for i, c in
                              enumerate(["teal", "ochre", "plum"][:n])})
        from deltos.sources import chats
        return chats.units(self.home, [], self.now + 3600)

    def test_enabled(self):
        on = {"auto": True}
        self.assertIs(learn.enabled(self.cfg()), learn.AUTO_DEFAULT)
        self.assertIs(learn.enabled(self.cfg(learn=on)), True)
        self.assertIs(learn.enabled(self.cfg(learn=on, llm={"url": ""})), False)
        self.assertIs(learn.enabled(self.cfg(learn=on, owner="")), False)
        self.assertIs(learn.enabled(self.cfg(learn=on, sessions={})), False)
        self.assertIs(learn.enabled(self.cfg(learn={"auto": False})), False)
        self.assertEqual(learn.why_not(self.cfg(owner="")), "set owner = <your name> in deltos.toml")

    def test_idle_follows_timestamps(self):
        make_home(self.home, {"s0": [("user", "hello there", time.time() - 600)]})
        self.assertTrue(learn.idle(self.home))
        self.assertFalse(learn.idle(self.home, secs=900))
        (self.home / "profiles" / "work").mkdir(parents=True)
        make_home(self.home / "profiles" / "work", {"w0": [("user", "a new message", time.time() - 5)]})
        self.assertFalse(learn.idle(self.home))
        self.assertTrue(learn.idle(self.tmp / "nowhere"))

    def test_apply_units_logs_chat_rows_and_texts(self):
        us = self.units(2)
        self.serve()
        stage = self.tmp / "stage"
        shutil.copytree(self.root, stage)
        self.assertEqual(learn.apply_units(stage, us, self.cfg(), say=lambda s: None), 2)
        rows = jsonl(stage / "migrate-log.jsonl")
        self.assertEqual([r["note"] for r in rows], ["chat:default/s0", "chat:default/s1"])
        self.assertEqual({r["kind"] for r in rows}, {"chat"})
        self.assertEqual([r["upto"] for r in rows], [u["upto"] for u in us])
        self.assertEqual([(r["chunk"], r["of"]) for r in rows], [(0, 1), (0, 1)])
        texts = jsonl(stage / "migrate-texts.jsonl")
        self.assertEqual(texts, [{"note": u["note"], "chunk": 0, "text": u["pieces"][0], "upto": u["upto"]} for u in us])
        # The chat prompt with the owner's name, one request per piece, no temperature sent.
        self.assertEqual(len(self.asked), 2)
        self.assertTrue(self.asked[0][0]["content"].startswith("You are adding a conversation to a structured "
                                                               "memory about the user, Ava."))
        self.assertEqual(Store(stage).find("Ava").attrs["front door colour"].value, "ochre")
        self.assertFalse((self.root / "migrate-log.jsonl").exists())   # live untouched

    def test_items_cache_and_said_are_saved_in_the_stage(self):
        us = self.units(1)
        self.serve()
        stage = self.tmp / "stage"
        shutil.copytree(self.root, stage)
        learn.apply_units(stage, us, self.cfg(import_items=True), say=lambda s: None)
        self.assertTrue((stage / "items.json").exists())
        self.assertTrue((stage / "items-said.json").exists())

    def test_a_request_error_stops_the_sweep(self):
        us = self.units(3)
        self.serve(fail_on=2)
        stage = self.tmp / "stage"
        shutil.copytree(self.root, stage)
        self.assertEqual(learn.apply_units(stage, us, self.cfg(), say=lambda s: None), 1)
        self.assertEqual(len(self.asked), 2)   # nothing asked after the error
        self.assertEqual([r["note"] for r in jsonl(stage / "migrate-log.jsonl")], ["chat:default/s0"])
        (err,) = jsonl(stage / "migrate-errors.jsonl")
        self.assertEqual((err["note"], err["kind"]), ("chat:default/s1", "chat"))
        self.assertIn("model down", err["error"])

    def test_a_busy_agent_stops_it_after_the_current_unit(self):
        us = self.units(3)
        self.serve()
        stage = self.tmp / "stage"
        shutil.copytree(self.root, stage)
        self.assertEqual(learn.apply_units(stage, us, self.cfg(), say=lambda s: None, busy=lambda: True), 1)
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(len((stage / "migrate-log.jsonl").read_text().splitlines()), 1)

    def test_a_unit_is_logged_only_once_all_its_pieces_are_in(self):
        long = "I painted the front door teal today. " + "We talked the whole colour chart through. " * 46
        make_home(self.home, {"s0": [("user", long, self.now - 100 * 60 + i) for i in range(6)]})
        from deltos.sources import chats
        (u,) = chats.units(self.home, [], self.now + 3600)
        self.assertEqual(u["of"], 2)
        self.serve(fail_on=2)   # the unit's second piece
        stage = self.tmp / "stage"
        shutil.copytree(self.root, stage)
        self.assertEqual(learn.apply_units(stage, [u], self.cfg(), say=lambda s: None), 0)
        self.assertFalse((stage / "migrate-log.jsonl").exists())     # no row: the watermark stays
        self.assertFalse((stage / "migrate-texts.jsonl").exists())
        (err,) = jsonl(stage / "migrate-errors.jsonl")
        self.assertEqual((err["note"], err["chunk"]), ("chat:default/s0", 1))
        self.assertEqual(chats.units(self.home, learn._live_rows(stage), self.now + 3600), [u])   # read whole next time

    def test_a_refused_chat_is_logged_and_the_sweep_goes_on(self):
        us = self.units(3)
        self.serve(fail_on=1, error=urllib.error.HTTPError("http://x", 400, "Bad Request", {}, None))
        stage = self.tmp / "stage"
        shutil.copytree(self.root, stage)
        self.assertEqual(learn.apply_units(stage, us, self.cfg(), say=lambda s: None), 2)
        self.assertEqual([r["note"] for r in jsonl(stage / "migrate-log.jsonl")], ["chat:default/s1", "chat:default/s2"])
        self.assertEqual([e["note"] for e in jsonl(stage / "migrate-errors.jsonl")], ["chat:default/s0"])
        # A rate limit is about the server, not the chat: it stops the sweep like a down model.
        self.asked.clear()
        self.serve(fail_on=1, error=urllib.error.HTTPError("http://x", 429, "Too Many Requests", {}, None))
        shutil.rmtree(stage)
        shutil.copytree(self.root, stage)
        self.assertEqual(learn.apply_units(stage, us, self.cfg(), say=lambda s: None), 0)
        self.assertEqual(len(self.asked), 1)

    def test_a_chat_another_run_promoted_meanwhile_is_not_learned_twice(self):
        make_home(self.home, {f"s{i}": chat_of(c, time.time() - (100 - i) * 60) for i, c in enumerate(["teal", "ochre"])})
        self.write_toml()
        self.serve()
        from deltos.sources import chats
        (first, _) = chats.units(self.home, [], time.time())
        runs = stub_runs(self.calls, self.tmp)
        inner = runs.execute

        def execute(root, kind, work, **kw):   # another trigger's run promotes s0 before this one gets the lock
            with open(Path(root) / "migrate-log.jsonl", "a") as fh:
                fh.write(json.dumps({"note": first["note"], "chunk": 0, "of": 1, "upto": first["upto"],
                                     "kind": "chat"}) + "\n")
            return inner(root, kind, work, **kw)
        runs.execute = execute
        out = []
        with stubbed(runs):
            self.assertEqual(learn.sweep(self.root, no_idle=True, say=out.append), 0)
        self.assertTrue(out[0].startswith("2 chats to learn"))
        self.assertIn("1 of these chats were learned by another run meanwhile", out)
        self.assertEqual(len(self.asked), 1)
        self.assertEqual([r["note"] for r in jsonl(self.root / "migrate-log.jsonl")],
                         ["chat:default/s0", "chat:default/s1"])

    def sweep(self, **kw):
        out = []
        with stubbed(stub_runs(self.calls, self.tmp)):
            rc = learn.sweep(self.root, say=out.append, **kw)
        return rc, out

    def test_sweep_runs_staged_and_a_second_sweep_finds_nothing_new(self):
        make_home(self.home, {f"s{i}": chat_of(c, time.time() - (100 - i) * 60) for i, c in enumerate(["teal", "ochre"])})
        self.write_toml()
        self.serve()
        rc, out = self.sweep(no_idle=True)
        self.assertEqual(rc, 0)
        (call,) = self.calls
        self.assertEqual((call["kind"], call["label"], call["owner"]), ("chat", "learn", "Ava"))
        self.assertIn(LLM["url"], call["fingerprint"])
        live = jsonl(self.root / "migrate-log.jsonl")
        self.assertEqual([r["note"] for r in live], ["chat:default/s0", "chat:default/s1"])
        rc, out = self.sweep(no_idle=True)
        self.assertEqual((rc, len(self.calls), len(self.asked)), (0, 1, 2))
        self.assertTrue(out[0].startswith("0 chats to learn"))
        self.assertIn("learned 2", out[0])

    def test_dry_run_writes_nothing(self):
        make_home(self.home, {"s0": chat_of("teal", time.time() - 6000)})
        self.write_toml()
        before = sorted((p.relative_to(self.tmp), p.stat().st_mtime_ns) for p in self.tmp.rglob("*"))
        notes.chat = None
        rc, out = self.sweep(dry_run=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls, [])
        self.assertIn("chat:default/s0", out[1])
        self.assertEqual(sorted((p.relative_to(self.tmp), p.stat().st_mtime_ns) for p in self.tmp.rglob("*")), before)

    def test_auto_off_keeps_the_trigger_out_but_not_a_run_by_hand(self):
        make_home(self.home, {"s0": chat_of("teal", time.time() - 6000)})
        self.write_toml("[learn]\nauto = false\n")
        self.serve()
        rc, out = self.sweep(wait=True)
        self.assertEqual((rc, self.calls), (0, []))
        self.assertIn("auto = false", out[0])
        rc, out = self.sweep(no_idle=True)
        self.assertEqual((rc, len(self.calls)), (0, 1))

    def test_wait_polls_until_idle_and_gives_up(self):
        make_home(self.home, {"s0": chat_of("teal", time.time() - 6000)})
        self.write_toml("[learn]\nauto = true\n")
        self.serve()
        with mock.patch.object(learn, "idle", side_effect=[False, False, True, True]), \
                mock.patch.object(learn.time, "sleep") as sleep:
            rc, _ = self.sweep(wait=True)
        self.assertEqual((rc, sleep.call_count, len(self.calls)), (0, 2, 1))
        clock = iter(range(0, 10 ** 6, 600))
        with mock.patch.object(learn, "idle", return_value=False), mock.patch.object(learn.time, "sleep"), \
                mock.patch.object(learn.time, "time", side_effect=lambda: float(next(clock))):
            rc, out = self.sweep(wait=True)
        self.assertEqual((rc, len(self.calls)), (0, 1))
        self.assertIn("busy for 20 min", out[-1])

    def test_not_set_up_says_why(self):
        rc, out = self.sweep()
        self.assertEqual((rc, out), (0, ["learning needs a model: set [llm] url in deltos.toml"]))

    def test_main_parses_its_flags(self):
        with mock.patch.object(learn, "sweep", return_value=3) as sw:
            self.assertEqual(learn.main(["--root", "R", "--wait", "--days", "7", "--max", "5", "--dry-run", "--no-idle"]), 3)
        args, kw = sw.call_args
        self.assertEqual((args, {k: v for k, v in kw.items() if k != "say"}),
                         (("R",), {"wait": True, "days": 7, "max_units": 5, "no_idle": True, "dry_run": True}))


if __name__ == "__main__":
    unittest.main()
