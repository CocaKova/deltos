"""Finished chats as units (deltos/sources/chats.py): which sessions give one, what a unit
reads (the user's turns whole, the assistant's openings), the per-session watermark, and
the chat prompt that bench/lme.py replays byte for byte."""
import datetime
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from deltos.sources import chats, hermes

NOW = datetime.datetime(2026, 10, 19, 12, 0).timestamp()
MIN = 60
KEY = "sk-" + "a1B2c3D4e5F6g7H8i9J0kLmN"
LONG_ASK = "We moved the photo archive to box-7 under /srv/photos, and " + "it has every album since 2019. " * 8
LONG_ANSWER = "Noted. " + "Here is a long explanation of how archives work and what to check next. " * 6


def make_db(path: Path, sessions: list[tuple], messages: list[tuple]) -> None:
    """sessions: (id, source, started_at, ended_at); messages: (session_id, role, content, tool_calls, timestamp).
    Only the columns the code reads, like an older Hermes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("create table sessions (id text primary key, source text, started_at real, ended_at real)")
    db.execute("create table messages (id integer primary key autoincrement, session_id text, role text, "
               "content text, tool_calls text, timestamp real)")
    db.executemany("insert into sessions values (?, ?, ?, ?)", sessions)
    db.executemany("insert into messages (session_id, role, content, tool_calls, timestamp) values (?, ?, ?, ?, ?)",
                   messages)
    db.commit()
    db.close()


def ids(path: Path, sid: str) -> list[int]:
    db = sqlite3.connect(path)
    out = [r[0] for r in db.execute("select id from messages where session_id = ? order by id", (sid,))]
    db.close()
    return out


class ChatUnitsTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.home = Path(d.name) / "hermes"
        self.db = self.home / "state.db"
        two_h, old = NOW - 120 * MIN, NOW - 3 * 86400
        make_db(self.db, [
            ("probe-1", "cli", NOW - 200 * MIN, NOW - 199 * MIN),
            ("job-1", "cron", NOW - 300 * MIN, NOW - 290 * MIN),
            ("quiet", "cli", NOW - 180 * MIN, None),
            ("active", "tui", NOW - 30 * MIN, None),
            ("done", "cli", NOW - 400 * MIN, NOW - 390 * MIN),
            ("resumed", "api_server", old, NOW - 50 * MIN),
        ], [
            ("probe-1", "user", "Reply OK", None, NOW - 200 * MIN),
            ("probe-1", "assistant", "OK", None, NOW - 199 * MIN),
            ("job-1", "user", "Write the morning report for Dana", None, NOW - 300 * MIN),
            ("job-1", "assistant", "Report written: Dana's port is 9100.", None, NOW - 291 * MIN),
            ("quiet", "user", LONG_ASK, None, two_h - 10 * MIN),
            ("quiet", "assistant", "", json.dumps([{"function": {"name": "terminal", "arguments": "{}"}}]), two_h - 9 * MIN),
            ("quiet", "tool", "drwxr-xr-x /srv/photos", None, two_h - 9 * MIN),
            ("quiet", "assistant", LONG_ANSWER, None, two_h - 8 * MIN),
            ("quiet", "user", f"Here is the key for the backup bucket: {KEY}", None, two_h),
            ("active", "user", "My sister Tamsin moves to Lisbon in May", None, NOW - 6 * MIN),
            ("active", "assistant", "Got it.", None, NOW - 5 * MIN),
            ("done", "user", "The garden shed is painted green now", None, NOW - 400 * MIN),
            ("done", "assistant", "Nice, green it is.", None, NOW - 399 * MIN),
            ("resumed", "user", "My bike is a 2018 Trek", None, old + MIN),
            ("resumed", "assistant", "A good bike.", None, old + 2 * MIN),
            ("resumed", "user", "I sold the Trek; I ride a 2024 Canyon now", None, NOW - 60 * MIN),
            ("resumed", "assistant", "Enjoy the Canyon!", None, NOW - 59 * MIN),
        ])
        self.done_ids, self.resumed_ids = ids(self.db, "done"), ids(self.db, "resumed")
        self.live = [{"note": "chat:default/done", "chunk": 0, "upto": self.done_ids[-1], "kind": "chat"},
                     {"note": "chat:default/resumed", "chunk": 0, "upto": self.resumed_ids[1], "kind": "chat"}]

    def test_only_finished_unlearned_turns_become_units(self):
        stats = {}
        us = chats.units(self.home, self.live, NOW, days=2, stats=stats)
        self.assertEqual([u["note"] for u in us], ["chat:default/quiet", "chat:default/resumed"])
        quiet, resumed = us
        day = datetime.date.fromtimestamp(NOW - 120 * MIN).isoformat()
        self.assertEqual(quiet["day"], day)
        self.assertEqual(quiet["upto"], ids(self.db, "quiet")[-1])
        self.assertEqual(quiet["of"], 1)
        self.assertEqual(quiet["pieces"], [
            f"Conversation on {day}:\n\n"
            f"user: {LONG_ASK}\n"
            f"assistant: {LONG_ANSWER[:200]} ...\n"
            "user: Here is the key for the backup bucket: [redacted]"])
        self.assertNotIn(KEY, quiet["pieces"][0])
        self.assertNotIn("drwxr-xr-x", quiet["pieces"][0])   # tool output is not a turn
        # The resumed chat: only the turns after its watermark, dated by its last message.
        self.assertEqual(resumed["upto"], self.resumed_ids[-1])
        self.assertEqual(resumed["pieces"], [
            f"Conversation on {datetime.date.fromtimestamp(NOW - 59 * MIN).isoformat()}:\n\n"
            "user: I sold the Trek; I ride a 2024 Canyon now\nassistant: Enjoy the Canyon!"])
        self.assertEqual({k: stats[k] for k in ("source", "trivial", "active", "learned", "units")},
                         {"source": 1, "trivial": 1, "active": 1, "learned": 1, "units": 2})
        self.assertEqual(stats["seen"], 6)

    def test_a_learned_chat_gives_nothing_until_it_is_resumed(self):
        rows = self.live + [{"note": u["note"], "chunk": 0, "upto": u["upto"], "kind": "chat"}
                            for u in chats.units(self.home, self.live, NOW)]
        self.assertEqual(chats.units(self.home, rows, NOW), [])
        db = sqlite3.connect(self.db)
        db.execute("insert into messages (session_id, role, content, tool_calls, timestamp) values "
                   "('quiet', 'user', 'The archive moved again, to box-9', null, ?)", (NOW - 40 * MIN,))
        db.commit()
        db.close()
        (u,) = chats.units(self.home, rows, NOW)
        self.assertEqual(u["note"], "chat:default/quiet")
        self.assertTrue(u["pieces"][0].endswith("\n\nuser: The archive moved again, to box-9"))

    def test_an_old_unlearned_chat_is_outside_the_window(self):
        us = chats.units(self.home, [], NOW, days=2)
        self.assertNotIn("chat:default/resumed", [u["note"] for u in us])   # started 3 days ago, no watermark
        self.assertIn("chat:default/resumed", [u["note"] for u in chats.units(self.home, [], NOW, days=4)])

    def test_profiles_skip_ids_long_chats_and_notices(self):
        prof = self.home / "profiles" / "work" / "state.db"
        long = "My grocery list for the week: " + "oat milk, rye bread, lentils, " * 100
        make_db(prof, [("w-1", "matrix", NOW - 100 * MIN, NOW - 40 * MIN), ("bench-7", "cli", NOW - 90 * MIN, NOW - 80 * MIN)],
                [("w-1", "user", long, None, NOW - 100 * MIN + i) for i in range(6)]
                + [("bench-7", "user", "What port does the wiki use? It was 8080 last week", None, NOW - 90 * MIN),
                   ("bench-7", "assistant", "The wiki is on port 8080, served by the reverse proxy.", None, NOW - 85 * MIN)])
        (self.home / "profiles" / "broken").mkdir()
        (self.home / "profiles" / "broken" / "state.db").write_text("not a database")
        stats = {}
        us = [u for u in chats.units(self.home, self.live, NOW, skip_ids="^bench-", stats=stats)
              if u["note"].startswith("chat:work/")]
        self.assertEqual([u["note"] for u in us], ["chat:work/w-1"])
        self.assertEqual((stats["skip_ids"], stats["unreadable"]), (1, 1))
        u = us[0]
        self.assertGreater(u["of"], 1)
        self.assertEqual(len(u["pieces"]), u["of"])
        self.assertTrue(all(p.startswith("--- chat w-1 (continued)\n") for p in u["pieces"][1:]))
        self.assertIn("user: " + long[:chats.MSG_CHARS] + "\n", u["pieces"][0])
        self.assertNotIn(long[:chats.MSG_CHARS + 1], "".join(u["pieces"]))   # a user turn is cut at MSG_CHARS

        # A newer Hermes marks the rows it writes itself (a compaction summary, a system notice);
        # they are not the owner's words. A steer is.
        new = self.home / "profiles" / "new" / "state.db"
        make_db(new, [("n-1", "tui", NOW - 100 * MIN, NOW - 50 * MIN)], [])
        db = sqlite3.connect(new)
        db.execute("alter table messages add column display_kind text")
        db.execute("alter table messages add column _compressed_summary integer")
        db.executemany("insert into messages (session_id, role, content, timestamp, display_kind, _compressed_summary) "
                       "values ('n-1', ?, ?, ?, ?, ?)",
                       [("user", "Earlier turns summarised: the server runs on port 7000", NOW - 99 * MIN, None, 1),
                        ("user", "Background process finished (exit code 0)", NOW - 98 * MIN, "process_complete", 0),
                        ("user", "My new phone number ends in 4417", NOW - 97 * MIN, None, 0),
                        ("user", "[message from the user] actually it ends in 4471", NOW - 96 * MIN, "steer", 0),
                        ("assistant", "Saved, ends in 4471.", NOW - 95 * MIN, None, 0)])
        db.commit()
        db.close()
        (n,) = [u for u in chats.units(self.home, self.live, NOW) if u["note"] == "chat:new/n-1"]
        self.assertNotIn("port 7000", n["pieces"][0])
        self.assertNotIn("Background process", n["pieces"][0])
        self.assertIn("user: My new phone number ends in 4417\nuser: [message from the user] actually it ends in 4471",
                      n["pieces"][0])


def make_new_db(path: Path, sessions: list[tuple], messages: list[tuple]) -> None:
    """A current Hermes: sessions (id, source, started_at, ended_at, parent_session_id); messages
    (session_id, role, content, timestamp, active, compacted, _compressed_summary, display_kind)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("create table sessions (id text primary key, source text, started_at real, ended_at real, "
               "parent_session_id text)")
    db.execute("create table messages (id integer primary key autoincrement, session_id text, role text, content text, "
               "tool_calls text, timestamp real, active integer not null default 1, compacted integer not null default 0, "
               "_compressed_summary integer not null default 0, display_kind text)")
    db.executemany("insert into sessions values (?, ?, ?, ?, ?)", sessions)
    add(db, messages)
    db.close()


def add(db, messages: list[tuple]) -> None:
    db.executemany("insert into messages (session_id, role, content, timestamp, active, compacted, _compressed_summary, "
                   "display_kind) values (?, ?, ?, ?, ?, ?, ?, ?)", messages)
    db.commit()


def said_by_user(text: str) -> str:
    """The lines a reader of whose line it is takes for the owner's (the chat grounding rule's
    reading): a line opening 'assistant:' starts the assistant's, 'user:', '--- ' or
    'Conversation on ' the user's, and every other line is the previous speaker's."""
    out, user = [], True
    for ln in text.splitlines():
        if ln.startswith("assistant:"):
            user = False
        elif ln.startswith(("user:", "--- ", "Conversation on ")):
            user = True
        if user:
            out.append(ln)
    return "\n".join(out)


class HermesRowsTest(unittest.TestCase):
    """The row states a current Hermes writes: a turn taken back, a compaction's carried copies."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.home = Path(d.name)
        self.db = self.home / "state.db"

    def test_a_turn_taken_back_is_not_read(self):
        t = NOW - 120 * MIN
        make_new_db(self.db, [("r-1", "tui", t, t + 600, None)], [
            ("r-1", "user", "My locker code is 1234", t, 0, 0, 0, None),          # rewound: the user took it back
            ("r-1", "assistant", "Saved: locker code 1234.", t + 10, 0, 0, 0, None),
            ("r-1", "user", "My locker code is 4321", t + 60, 1, 0, 0, None),
            ("r-1", "assistant", "Saved: locker code 4321.", t + 70, 1, 0, 0, None)])
        (u,) = chats.units(self.home, [], NOW)
        self.assertNotIn("1234", u["pieces"][0])
        self.assertIn("user: My locker code is 4321", u["pieces"][0])

    def test_an_in_place_compaction_after_learning_gives_nothing_new(self):
        t = NOW - 300 * MIN
        turns = [("user", "The garage door code is 5512", t), ("assistant", "Noted the garage code.", t + 10),
                 ("user", "And the shed is painted green", t + 60), ("assistant", "Green shed, noted.", t + 70)]
        make_new_db(self.db, [("c-1", "tui", t, None, None)], [("c-1", r, c, ts, 1, 0, 0, None) for r, c, ts in turns])
        (u,) = chats.units(self.home, [], NOW)
        live = [{"note": u["note"], "chunk": 0, "upto": u["upto"], "kind": "chat"}]
        # Compacted in place: the first turns summarized away (compacted = 1, still the owner's words),
        # the rest archived (active = 0) and re-inserted as the carried tail with new ids.
        db = sqlite3.connect(self.db)
        db.execute("update messages set active = 0, compacted = 1 where id in (1, 2)")
        db.execute("update messages set active = 0, compacted = 0 where id in (3, 4)")
        add(db, [("c-1", "user", "Summary of earlier turns: garage code 5512", t + 80, 1, 0, 1, None)]
            + [("c-1", r, c, ts, 1, 0, 0, None) for r, c, ts in turns[2:]])
        stats = {}
        self.assertEqual(chats.units(self.home, live, NOW, stats=stats), [])   # nothing was said since
        self.assertEqual(stats["learned"], 1)
        add(db, [("c-1", "user", "The shed door sticks in the rain", NOW - 60 * MIN, 1, 0, 0, None)])
        db.close()
        (u,) = chats.units(self.home, live, NOW)
        self.assertTrue(u["pieces"][0].endswith("\n\nuser: The shed door sticks in the rain"))

    def test_a_compaction_child_does_not_repeat_its_parents_turns(self):
        t = NOW - 300 * MIN
        make_new_db(self.db, [("p-1", "tui", t, t + 100, None), ("k-1", "tui", t + 100, t + 400, "p-1"),
                              ("k-2", "tui", t + 100, t + 400, "p-1")], [
            ("p-1", "user", "The boat is called Wren", t, 1, 0, 0, None),
            ("p-1", "assistant", "Wren is a fine name for a boat.", t + 10, 1, 0, 0, None),
            ("k-1", "user", "Summary: the boat is called Wren", t + 100, 1, 0, 1, None),
            ("k-1", "user", "The boat is called Wren", t, 1, 0, 0, None),          # the carried copy
            ("k-1", "assistant", "Wren is a fine name for a boat.", t + 10, 1, 0, 0, None),
            ("k-1", "user", "Her mooring is slip 14", t + 300, 1, 0, 0, None),
            ("k-1", "assistant", "Slip 14, noted.", t + 310, 1, 0, 0, None),
            ("k-2", "user", "The boat is called Wren", t, 1, 0, 0, None),          # copies only
            ("k-2", "assistant", "Wren is a fine name for a boat.", t + 10, 1, 0, 0, None)])
        stats = {}
        us = {u["note"]: u for u in chats.units(self.home, [], NOW, stats=stats)}
        self.assertEqual(sorted(us), ["chat:default/k-1", "chat:default/p-1"])
        self.assertIn("Wren", us["chat:default/p-1"]["pieces"][0])
        self.assertNotIn("Wren", us["chat:default/k-1"]["pieces"][0])
        self.assertIn("user: Her mooring is slip 14", us["chat:default/k-1"]["pieces"][0])
        self.assertEqual(stats["copies"], 1)

    def test_an_ask_whose_only_reply_failed_is_a_unit(self):
        t = NOW - 120 * MIN
        make_new_db(self.db, [("f-1", "tui", t, t + 600, None), ("e-1", "tui", t, None, None)], [
            ("f-1", "user", "We moved the photo archive to box-7", t, 1, 0, 0, None),
            ("f-1", "assistant", "The turn failed: the model did not answer.", t + 10, 1, 0, 0, "error")])
        stats = {}
        (u,) = chats.units(self.home, [], NOW, stats=stats)
        self.assertEqual(u["pieces"][0].split("\n\n", 1)[1], "user: We moved the photo archive to box-7")
        self.assertEqual((stats["empty"], stats.get("active", 0)), (1, 0))   # no messages: not an active chat


class LongChatTest(unittest.TestCase):
    def test_a_long_chat_is_cut_between_turns(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        home = Path(d.name)
        t = NOW - 7200
        msgs = []
        for i in range(4):   # four 2,000-char user turns with one-line replies
            msgs += [("user", f"Turn {i}: " + "we talked about the garden and the shed and the paths. " * 40),
                     ("assistant", "Sounds like a good plan for the garden this year.")]
        # A multi-line assistant opening that a cut at any line end puts across the 9,000-char mark.
        head = "Conversation on 2026-10-19:\n\n" + "\n".join(chats._line(r, c) for r, c in msgs) + "\n"
        pad = 9000 - len(head) - len("user: \n") - len("assistant: Sure.\n") - 5
        msgs += [("user", "x" * pad), ("assistant", "Sure.\nThe staging server listens on port 8443 now.\nAnything else?")]
        make_db(home / "state.db", [("L-1", "cli", t, t + 3000)],
                [("L-1", r, c, None, t + i) for i, (r, c) in enumerate(msgs)])
        (u,) = chats.units(home, [], NOW)
        self.assertGreater(u["of"], 1)
        self.assertTrue(all(len(p) <= chats.MAX_CHARS for p in u["pieces"]))
        for p in u["pieces"][1:]:
            self.assertRegex(p, r"^--- chat L-1 \(continued\)\n(user|assistant): ")
        self.assertNotIn("8443", "\n".join(said_by_user(p) for p in u["pieces"]))
        # Every turn is whole in one piece.
        self.assertEqual(sum(p.count("\nuser: ") + p.startswith("user: ") for p in u["pieces"]), 5)

    def test_a_turn_longer_than_a_piece_keeps_its_speaker(self):
        turn = "assistant: " + "the port is 8443 and the path is long. " * 400
        pieces = chats._pack("Conversation on 2026-10-19:\n\n", ["user: hi", turn, "user: bye"],
                             "--- chat X (continued)\n")
        self.assertGreater(len(pieces), 2)
        self.assertTrue(all(len(p) <= chats.MAX_CHARS for p in pieces))
        for p in pieces[1:]:
            self.assertRegex(p, r"^--- chat X \(continued\)\n(user|assistant): ")
        self.assertEqual(said_by_user("\n".join(pieces)).count("8443"), 0)
        self.assertEqual("".join(p.split("\n", 1)[1] if i else p for i, p in enumerate(pieces)).count("8443"),
                         turn.count("8443"))


class ChatPromptTest(unittest.TestCase):
    def test_the_benchmark_owner_gets_the_benchmark_prompt_byte_for_byte(self):
        p = chats.chat_prompt("Sam")
        self.assertEqual(hashlib.sha1(p.encode()).hexdigest(), "445190dd373f19dffe885eb5b2fc3fdbbf9398a9")
        self.assertEqual(len(p), 1667)

    def test_another_owner(self):
        p = chats.chat_prompt("Ava")
        self.assertNotIn("Sam", p)
        self.assertNotIn("<OWNER>", p)
        self.assertEqual(p.count("Ava"), 13)
        self.assertIn("{day}", p)
        self.assertIn("{known}", p)


class TrivialTest(unittest.TestCase):
    def test_shape(self):
        write = json.dumps([{"function": {"name": "write_file", "arguments": json.dumps({"path": "/tmp/x.md"})}}])
        self.assertTrue(hermes.trivial([("user", "Reply OK", None), ("assistant", "OK", None)]))
        self.assertFalse(hermes.trivial([("user", "ping", None)]))   # no answer: its reply failed, not a probe
        self.assertFalse(hermes.trivial([("user", "Reply OK", None), ("assistant", "OK", None), ("user", "and?", None)]))
        self.assertFalse(hermes.trivial([("user", "Save it", None), ("assistant", "", write), ("assistant", "Done", None)]))
        self.assertFalse(hermes.trivial([("user", "Port?", None), ("assistant", "The wiki listens on 8080 now.", None)]))

    def test_digest_skips_trivial_sessions_and_takes_skip_ids(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        db = Path(d.name) / "state.db"
        day = datetime.date(2026, 10, 19)
        t = datetime.datetime(2026, 10, 19, 9, 0).timestamp()
        make_db(db, [("probe-1", "cli", t, t + 60), ("chat-1", "cli", t + 100, t + 900), ("x-1", "cli", t + 200, t + 900)],
                [("probe-1", "user", "Reply OK", None, t), ("probe-1", "assistant", "OK", None, t + 5),
                 ("chat-1", "user", "Plan the garden beds for spring", None, t + 100),
                 ("chat-1", "assistant", "Here is a plan with three beds and a path.", None, t + 200),
                 ("x-1", "user", "Plan the shed", None, t + 200),
                 ("x-1", "assistant", "Here is a plan for the shed and its shelves.", None, t + 300)])
        c = sqlite3.connect(db)
        c.execute("alter table sessions add column title text")   # the digest prints a session's title
        c.commit()
        c.close()
        self.assertEqual([e.split("- session: ")[1] for _, e in hermes.digest(db, "default", day)], ["chat-1", "x-1"])
        self.assertEqual([e.split("- session: ")[1] for _, e in hermes.digest(db, "default", day, skip_ids="^x-")],
                         ["chat-1"])


if __name__ == "__main__":
    unittest.main()
