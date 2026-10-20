"""Another one, or the same one changed (deltos/items.py): a second item of
the same kind stays current beside the first; the decision is the model's,
cached, and an unknown event replaces as before."""
import json
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import deltos.llm as llm
from deltos import Memory, items
from deltos.config import Config
from deltos.sources import notes
from deltos.sources.notes import Migrator
from deltos.store import Store

S1 = ("user: I helped raise over $1,000 for the children's hospital at a charity bake sale.\n"
      "assistant: That's great!")
S2 = ("user: I just ran the Run for Hunger 5K and raised $250 for a local food bank.\n"
      "assistant: Well done.")
S3 = "user: I have 3 bikes now.\nassistant: Nice."
S4 = "user: I just bought my fourth bike, so 4 bikes now!\nassistant: Nice."


def session(m, sid, day, text, lines):
    return m.session(sid, day, text, day, replay={"raw": "\n".join(lines)})


class ItemsTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.root = Path(self.d.name)

    def build(self, decider):
        m = Migrator(self.root, owner="Sam", chat=True, items=decider)
        session(m, "s1", "2023-03-10", S1, ["Sam | fundraising amount | $1,000 | 2023-03-10"])
        session(m, "s2", "2023-03-20", S2, ["Sam | fundraising amount | $250 | 2023-03-20"])
        session(m, "s3", "2023-03-10", S3, ["Sam | bike count | 3 | 2023-03-10"])
        session(m, "s4", "2023-03-20", S4, ["Sam | bike count | 4 | 2023-03-20"])
        session(m, "s5", "2023-03-21", "user: I'm a nurse now.", ["Sam | status | nurse | 2023-03-21"])
        session(m, "s6", "2023-03-22", "user: I'm a teacher now.", ["Sam | status | teacher | 2023-03-22"])
        return Store(self.root).find("Sam")

    def test_unknown_events_replace_as_before_and_wait(self):
        d = items.Items({})
        t = self.build(d)
        a = t.attrs["fundraising amount"]
        self.assertEqual((a.value, [p.value for p in a.trail], a.also), ("$250", ["$1,000"], []))
        self.assertEqual(len(d.pending), 2)   # fundraising and bikes; "status" is never asked
        ev = next(e for e in d.pending.values() if e["label"].lower() == "fundraising amount")
        self.assertIn("bake sale", ev["old_ev"])
        self.assertIn("Run for Hunger", ev["new_ev"])
        self.assertEqual((ev["old_sid"], ev["new_sid"]), ("s1", "s2"))

    def test_a_label_is_asked_unless_it_holds_one_value_by_definition(self):
        d = items.Items({})
        m = Migrator(self.root, owner="Sam", chat=True, items=d)
        session(m, "s1", "2023-03-10", "user: My Pi runs Jellyfin on port 8096, theme dark, status up.",
                ["pi | service | Jellyfin | 2023-03-10", "pi | port | 8096 | 2023-03-10",
                 "pi | setting theme | dark | 2023-03-10", "pi | status | up | 2023-03-10"])
        session(m, "s2", "2023-03-20", "user: The Pi now also runs Pi-hole on port 53, theme light, status down.",
                ["pi | service | Pi-hole | 2023-03-20", "pi | port | 53 | 2023-03-20",
                 "pi | setting theme | light | 2023-03-20", "pi | status | down | 2023-03-20"])
        self.assertEqual(sorted(e["label"].lower() for e in d.pending.values()), ["port", "service"])

    def test_a_value_stored_before_the_check_was_on_is_asked_with_its_receipt(self):
        session(Migrator(self.root, owner="Sam", chat=True), "s1", "2023-03-10",
                "user: My Pi runs Jellyfin.", ["pi | service | Jellyfin | 2023-03-10"])
        d = items.Items({})
        session(Migrator(self.root, owner="Sam", chat=True, items=d), "s2", "2023-03-20",
                "user: The Pi now also runs Pi-hole.", ["pi | service | Pi-hole | 2023-03-20"])
        ev = next(iter(d.pending.values()))
        self.assertTrue(items.askable(ev))
        self.assertIn("Jellyfin", ev["old_ev"])

    def test_no_decider_is_byte_identical_to_an_empty_cache(self):
        self.build(None)
        before = {p.name: p.read_text() for p in (self.root / "notes").glob("*.md")}
        self.d.cleanup()
        self.d = tempfile.TemporaryDirectory()
        self.root = Path(self.d.name)
        self.build(items.Items({}))
        after = {p.name: p.read_text() for p in (self.root / "notes").glob("*.md")}
        self.assertEqual(before, after)

    def test_another_one_stays_current_and_replays_without_the_model(self):
        d = items.Items({})
        self.build(d)
        cache = {k: ("another" if "fund" in e["label"].lower() else "same") for k, e in d.pending.items()}
        self.d.cleanup()
        self.d = tempfile.TemporaryDirectory()
        self.root = Path(self.d.name)
        d2 = items.Items(cache)
        t = self.build(d2)
        self.assertEqual(d2.pending, {})
        a = t.attrs["fundraising amount"]
        self.assertEqual((a.value, [(x.value, x.since) for x in a.also], a.trail), ("$250", [("$1,000", "2023-03-10")], []))
        b = t.attrs["bike count"]
        self.assertEqual((b.value, [p.value for p in b.trail], b.also), ("4", ["3"], []))
        text = (self.root / "notes" / "sam.md").read_text()
        self.assertIn("  - also current: $1,000 (another one, from 2023-03-10)", text)
        # a count question sees both items in the block
        ctx = Memory(config=Config(root=self.root, pages_write=self.root / "pages", owner="Sam"),
                     today="2023-03-25").context("What is my total fundraising amount?")
        self.assertIn("$250", ctx)
        self.assertIn("also current: $1,000", ctx)


class StoreAlsoTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.s = Store(Path(self.d.name), today="2023-06-01")
        self.s.name("Sam", [], "person")
        self.s.remember("Sam", "new possession", "smoker", since="2023-03-15")

    def test_round_trip_and_repeats(self):
        r = self.s.remember("Sam", "new possession", "denim jacket", since="2023-03-20", another=True)
        self.assertIn("another one beside smoker", r)
        r = self.s.remember("Sam", "new possession", "smoker", since="2023-03-25", confirm=False)
        self.assertIn("still smoker (another one)", r)
        r = self.s.remember("Sam", "new possession", "Nike Air Force 1s", since="2023-02-22", another=True)
        self.assertIn("recorded as another one", r)
        t = Store(Path(self.d.name)).find("Sam")   # parsed back from the markdown
        a = t.attrs["new possession"]
        self.assertEqual(a.value, "denim jacket")
        self.assertEqual([(x.value, x.since) for x in a.also], [("smoker", "2023-03-15"), ("Nike Air Force 1s", "2023-02-22")])
        self.assertEqual(a.trail, [])

    def test_a_plain_update_after_items_still_replaces(self):
        self.s.remember("Sam", "new possession", "denim jacket", since="2023-03-20", another=True)
        self.s.remember("Sam", "new possession", "blue denim jacket", since="2023-03-30")
        a = Store(Path(self.d.name)).find("Sam").attrs["new possession"]
        self.assertEqual((a.value, [p.value for p in a.trail], [x.value for x in a.also]),
                         ("blue denim jacket", ["denim jacket"], ["smoker"]))

    def test_fold_keeps_both_sides_items(self):
        self.s.remember("Sam", "new possession", "denim jacket", since="2023-03-20", another=True)
        self.s.remember("Sam", "recent purchase", "lamp", since="2023-04-01")
        self.s.fold("Sam", "new possession", "recent purchase")
        a = Store(Path(self.d.name)).find("Sam").attrs["recent purchase"]
        self.assertEqual(a.value, "lamp")
        self.assertEqual([x.value for x in a.also], ["smoker"])


class AskTest(unittest.TestCase):
    EV = [{"thing": "Sam", "label": "Fundraising amount", "old": "$1,000", "old_since": "2023-03-10", "old_ev": "bake sale",
           "new": "$250", "new_since": "2023-03-20", "new_ev": "Run for Hunger"},
          {"thing": "Sam", "label": "Bike count", "old": "3", "old_since": "2023-03-10", "old_ev": "I have 3 bikes",
           "new": "4", "new_since": "2023-03-20", "new_ev": "my fourth bike"}]

    def test_no_words_behind_a_value_is_not_asked_and_replaces(self):
        ev = {**self.EV[1], "old_ev": ""}
        seen = []
        got = items.ask([ev], lambda *a, **k: (seen.append(1) or ({"content": "1 another"}, {})))
        self.assertEqual((got, seen), ({}, []))
        d = items.Items({})
        self.assertFalse(d.decide(ev))
        self.assertEqual((d.pending, d.events[-1]["decision"]), ({}, "no evidence"))

    def test_answers_are_read_and_the_model_reasons_the_servers_way(self):
        seen = []

        def chat(messages, max_tokens=0, extra=None, **kw):
            seen.append(extra)
            return {"content": "<think>hmm</think>\n1. Another\n**2** same"}, {}

        os.environ.pop("DELTOS_ITEMS_EXTRA", None)
        on = {"chat_template_kwargs": {"enable_thinking": True}}
        with mock.patch.dict(llm.ENDPOINT, {"thinking": "off", "thinking_on": None}):
            got = items.ask(self.EV, chat)
        with mock.patch.dict(llm.ENDPOINT, {"thinking": "server", "thinking_on": None}):
            items.ask(self.EV, chat)
        self.assertEqual(got, {items.key(self.EV[0]): "another", items.key(self.EV[1]): "same"})
        # "server": nothing server-specific is sent, only the ladder's first rung (a classification: 0)
        self.assertEqual(seen, [{**on, "temperature": 0.0}, {"temperature": 0.0}])

    def test_extra_fields_come_from_the_environment(self):
        seen = []
        chat = lambda messages, max_tokens=0, extra=None, **kw: (seen.append(extra) or ({"content": "1 same"}, {}))
        with mock.patch.dict(os.environ, {"DELTOS_ITEMS_EXTRA": json.dumps({"reasoning_effort": "none"})}), \
                mock.patch.dict(llm.ENDPOINT, {"thinking": "server", "thinking_on": None}):
            items.ask(self.EV[:1], chat)
        self.assertEqual(seen, [{"reasoning_effort": "none", "temperature": 0.0}])

    def test_a_cut_off_batch_is_asked_again_in_halves(self):
        sizes, stats = [], {}

        def chat(messages, max_tokens=0, extra=None, **kw):
            n = len(re.findall(r"(?m)^\d+\. Sam, ", messages[0]["content"]))
            sizes.append(n)
            if n > 1:   # thought the whole budget away
                return {"content": ""}, {"completion_tokens": max_tokens, "finish_reason": "length"}
            return {"content": "1 another"}, {"finish_reason": "stop"}

        with mock.patch.dict(llm.ENDPOINT, {"thinking": "server", "thinking_off": {}}):
            got = items.ask(self.EV, chat, stats=stats)
        self.assertEqual(sizes, [2, 1, 1])
        self.assertEqual(len(got), 2)
        self.assertEqual(stats, {"cut": 1})

    def test_a_failed_or_unreadable_answer_is_not_cached(self):
        def boom(*a, **k):
            raise OSError("down")
        self.assertEqual(items.ask(self.EV, boom), {})
        self.assertEqual(items.ask(self.EV, lambda *a, **k: ({"content": "I think both are fine"}, {})), {})



class NightByNightTest(unittest.TestCase):
    """The agent's store is built one night at a time (deltos import): no rebuild
    applies .pending later, so an unknown event is asked at once, and the words
    behind a value filed on an earlier night are kept for the next one."""

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.root = Path(self.d.name)
        self.asked = []

    def asker(self, said):
        def ask(evs):
            self.asked += evs
            return {items.key(ev): said for ev in evs}
        return ask

    def test_an_unknown_event_is_asked_once_and_applied(self):
        d = items.Items({}, asker=self.asker("another"))
        m = Migrator(self.root, owner="Sam", chat=True, items=d)
        session(m, "s1", "2023-03-10", S1, ["Sam | fundraising amount | $1,000 | 2023-03-10"])
        session(m, "s2", "2023-03-20", S2, ["Sam | fundraising amount | $250 | 2023-03-20"])
        a = Store(self.root).find("Sam").attrs["fundraising amount"]
        self.assertEqual((a.value, [x.value for x in a.also]), ("$250", ["$1,000"]))
        self.assertEqual((len(self.asked), d.asked, d.pending), (1, 1, {}))
        self.assertEqual(set(d.cache.values()), {"another"})

    def test_a_failed_ask_replaces_as_before(self):
        d = items.Items({}, asker=lambda evs: {})
        m = Migrator(self.root, owner="Sam", chat=True, items=d)
        session(m, "s1", "2023-03-10", S1, ["Sam | fundraising amount | $1,000 | 2023-03-10"])
        session(m, "s2", "2023-03-20", S2, ["Sam | fundraising amount | $250 | 2023-03-20"])
        a = Store(self.root).find("Sam").attrs["fundraising amount"]
        self.assertEqual((a.value, [p.value for p in a.trail], a.also), ("$250", ["$1,000"], []))
        self.assertEqual((d.cache, len(d.pending)), ({}, 1))

    def test_the_earlier_nights_words_are_kept_for_the_next(self):
        said = self.root / "items-said.json"
        m1 = Migrator(self.root, owner="Sam", chat=True, items=items.Items({}))
        session(m1, "s1", "2023-03-10", S1, ["Sam | fundraising amount | $1,000 | 2023-03-10"])
        session(m1, "s3", "2023-03-10", S3, ["Sam | bike count | 3 | 2023-03-10"])
        session(m1, "s4", "2023-03-20", S4, ["Sam | bike count | 4 | 2023-03-20"])
        notes.save_said(said, m1._said, m1.store)
        kept = {tuple(r[:3]) for r in json.loads(said.read_text())}
        self.assertIn(("sam", "fundraising amount", "$1,000"), kept)
        self.assertNotIn(("sam", "bike count", "3"), kept)   # no longer current: dropped
        m2 = Migrator(self.root, owner="Sam", chat=True, items=items.Items({}, asker=self.asker("another")))
        m2._said = notes.load_said(said)
        session(m2, "s2", "2023-03-20", S2, ["Sam | fundraising amount | $250 | 2023-03-20"])
        ev = next(e for e in self.asked if e["label"].lower() == "fundraising amount")
        self.assertIn("bake sale", ev["old_ev"])
        self.assertEqual(ev["old_sid"], "s1")

    def test_sessions_at_once_keep_their_own_words(self):
        """Two sessions in flight: each value's words come from its own text
        (the import runs [import] workers sessions at once)."""
        a_waiting, b_done = threading.Event(), threading.Event()
        lines = {"bake sale": "Sam | fundraising amount | $1,000 | 2023-03-10",
                 "Run for Hunger": "Sam | race | Run for Hunger 5K | 2023-03-20"}

        def chat(msgs, max_tokens=0, extra=None, **kw):
            text = msgs[-1]["content"]
            if "bake sale" in text:
                a_waiting.set()
                b_done.wait(5)
            raw = next(v for k, v in lines.items() if k in text)
            return {"content": raw}, {"prompt_tokens": 1, "completion_tokens": 1, "finish_reason": "stop"}

        m = Migrator(self.root, owner="Sam", prompt="Chat on {day}. Memory holds:\n{known}", items=items.Items({}))
        with mock.patch.object(notes, "chat", chat):
            t = threading.Thread(target=m.session, args=("s1", "2023-03-10", S1, "2023-03-10"))
            t.start()
            a_waiting.wait(5)
            m.session("s2", "2023-03-20", S2, "2023-03-20")
            b_done.set()
            t.join(5)
        self.assertEqual(m._said[("sam", "fundraising amount", "$1,000")][0], "s1")
        self.assertIn("bake sale", m._said[("sam", "fundraising amount", "$1,000")][1])



class ReceiptsTest(unittest.TestCase):
    """Receipts: the import keeps the sentence each current value came from
    under it; the block shows it unless DELTOS_RECEIPTS=0 (on since 10-17)."""

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.root = Path(self.d.name)

    def test_a_value_keeps_its_sentence_and_a_new_value_its_own(self):
        m = Migrator(self.root, owner="Sam", chat=True)
        session(m, "s3", "2023-03-10", S3, ["Sam | bike count | 3 | 2023-03-10"])
        a = Store(self.root).find("Sam").attrs["bike count"]
        self.assertEqual(a.said, ("I have 3 bikes now.", "2023-03-10"))
        self.assertIn('  - said: "I have 3 bikes now." (2023-03-10)', (self.root / "notes" / "sam.md").read_text())
        session(m, "s4", "2023-03-20", S4, ["Sam | bike count | 4 | 2023-03-20"])
        a = Store(self.root).find("Sam").attrs["bike count"]
        self.assertEqual((a.value, a.said[0]), ("4", "I just bought my fourth bike, so 4 bikes now!"))

    def test_a_value_said_again_keeps_its_receipt(self):
        m = Migrator(self.root, owner="Sam", chat=True)
        session(m, "s3", "2023-03-10", S3, ["Sam | bike count | 3 | 2023-03-10"])
        session(m, "s9", "2023-03-12", "user: Still 3 bikes, all good.", ["Sam | bike count | 3 | 2023-03-10"])
        self.assertEqual(Store(self.root).find("Sam").attrs["bike count"].said[0], "I have 3 bikes now.")

    def test_the_block_shows_receipts_unless_off(self):
        import deltos.recall as R
        m = Migrator(self.root, owner="Sam", chat=True)
        session(m, "s3", "2023-03-10", S3, ["Sam | bike count | 3 | 2023-03-10"])
        R.Recall._open.clear()
        mem = Memory(config=Config(root=self.root, owner="Sam"))
        self.assertIn('said: "I have 3 bikes now."', mem.context("How many bikes do I have?"))
        with mock.patch.object(R, "RECEIPTS", False):
            R.Recall._open.clear()
            self.assertNotIn("said:", Memory(config=Config(root=self.root, owner="Sam")).context("How many bikes do I have?"))
        R.Recall._open.clear()


if __name__ == "__main__":
    unittest.main()


class JudgeItemsKeepsTheFileTest(unittest.TestCase):
    """10-14: judge_items switched ITEMS off before loading it, so each `items`
    step started from an empty cache and overwrote the decisions of the steps
    before it (the Phase 2 run kept only knowledge-update's)."""

    def test_earlier_decisions_survive_a_new_step(self):
        import bench.lme as L
        ev = {"thing": "Sam", "label": "Hobby", "old": "chess", "new": "golf",
              "old_ev": "I play chess", "new_ev": "I took up golf"}

        def store(q, ex, rebuild=False, root=None, decider=None):
            decider.decide(ev)

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "items.json"
            p.write_text(json.dumps({"k1": "another"}))
            with mock.patch.object(L, "ITEMS", p), mock.patch.object(L, "_items_cache", None), \
                 mock.patch.object(L, "extracted", lambda: {}), mock.patch.object(L, "deltos_store", store), \
                 mock.patch.object(L, "OUT", Path(d)), \
                 mock.patch.object(L.items, "ask", lambda evs, chat, **k: {items.key(e): "same" for e in evs}):
                L.judge_items([{"question_id": "q1"}], 1, say=lambda *a, **k: None)
            self.assertEqual(json.loads(p.read_text()), {"k1": "another", items.key(ev): "same"})


class BenchItemsDefaultTest(unittest.TestCase):
    """10-14, Phase 2 landed for the bench: `run` applies out/lme/items.json when
    it exists, `items` fills it, `off` turns the check off."""

    def test_where_the_decisions_come_from(self):
        import bench.lme as L
        with tempfile.TemporaryDirectory() as d, mock.patch.object(L, "OUT", Path(d)):
            p = Path(d) / "items.json"
            self.assertIsNone(L.items_path("", "run"))       # no file yet: as before
            self.assertEqual(L.items_path("", "items"), p)   # items creates it
            p.write_text("{}")
            self.assertEqual(L.items_path("", "run"), p)
            self.assertIsNone(L.items_path("off", "run"))
            self.assertIsNone(L.items_path("", "extract"))
            self.assertEqual(L.items_path("x.json", "run"), Path("x.json"))


class RecallAlsoTest(unittest.TestCase):
    """Recall with also-lines: a question about several items gets every item of
    the kind it names (by label, value or item words); any other question gets
    the entry it names whole and at most two items of an entry it doesn't name."""

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        root = Path(self.d.name)
        s = Store(root, today="2023-06-01")
        s.name("Sam", [], "person")
        for i, w in enumerate("cooking hiking chess knitting cycling rowing baking pottery fishing surfing "
                              "climbing skating".split()):
            s.remember("Sam", f"{w} schedule", f"weekly session {i}", since="2023-01-05")
        s.remember("Sam", "fundraising amount", "$1,000", since="2023-02-10")
        for v, d in (("$250", "2023-03-05"), ("$500", "2023-03-12"), ("$2,000", "2023-03-20")):
            s.remember("Sam", "fundraising amount", v, since=d, another=True)
        s.remember("Sam", "possession", "antique tea set", since="2023-05-20")
        s.remember("Sam", "possession", "vintage typewriter", since="2023-05-25", another=True)
        s.name("Alex", [], "person")
        s.remember("Alex", "location", "Austin", since="2023-01-01")
        s.remember("Alex", "interest", "chess", since="2023-01-10")
        for v, d in (("golf", "2023-02-01"), ("piano", "2023-03-01"), ("hiking", "2023-04-01"), ("salsa", "2023-05-01")):
            s.remember("Alex", "interest", v, since=d, another=True)
        self.root = root

    def ctx(self, q):
        return Memory(config=Config(root=self.root, pages_write=self.root / "pages", owner="Sam"),
                      today="2023-06-01").context(q)

    def test_a_total_pulls_every_item_by_a_word_inside_the_label(self):
        ctx = self.ctx("How much money did I raise for charity in total?")   # "raise" in "Fundraising"
        for v in ("$1,000", "$250", "$500", "$2,000"):
            self.assertIn(v, ctx)
        self.assertIn("**Fundraising amount:**", ctx)

    def test_a_count_pulls_an_entry_by_an_item_word(self):
        ctx = self.ctx("How many antique items did I inherit from my family?")   # "antique" is in an item only
        self.assertIn("antique tea set", ctx)
        self.assertIn("vintage typewriter", ctx)

    def test_an_entry_the_question_names_keeps_every_item(self):
        ctx = self.ctx("Which interest did Alex pick up most recently?")
        for v in ("golf", "piano", "hiking", "salsa"):
            self.assertIn(v, ctx)
        self.assertNotIn("more also current", ctx)

    def test_an_entry_it_does_not_name_shows_two_items_newest_first(self):
        ctx = self.ctx("Where does Alex live?")
        self.assertIn("**Interest:** salsa", ctx)
        self.assertIn("also current: hiking", ctx)
        self.assertIn("also current: piano", ctx)
        self.assertNotIn("golf", ctx)
        self.assertIn("(+2 more also current: memory_show for every item)", ctx)

    def test_no_items_no_change(self):
        from deltos.recall import _cap_also
        b = ["- **Location:** Austin (since 2023-01-01)", "  - was: Dallas (2022-01-01 to 2023-01-01)"]
        self.assertIs(_cap_also(b, 2), b)

    def test_a_count_memory_already_keeps_pulls_no_items(self):
        s = Store(self.root, today="2023-10-17")
        s.remember("Sam", "H&M tops owned", "5", since="2023-09-30")
        s.remember("Sam", "recent purchase", "Three tops from H&M", since="2023-08-11")
        s.remember("Sam", "recent purchase", "ASOS order", since="2023-09-30", another=True)
        ctx = self.ctx("How many tops have I bought from H&M so far?")
        self.assertIn("**H&M tops owned:** 5", ctx)
        self.assertNotIn("also current: Three tops from H&M", ctx)
