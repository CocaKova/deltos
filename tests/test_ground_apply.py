"""The grounding gate in the importer, with a stub model: a value, a name or a new
thing enters memory only when the unit's own text states it, a kind alone makes
no thing, and a replay with the unit's text applies exactly what the run did."""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import deltos.sources.notes as notes
from deltos.store import Store

DAY = "2026-10-04"
CHAT_PROMPT = "Chat on {day}. Memory holds:\n{known}"


def total(st: dict, k: str) -> int:
    """A count over a session and its halves (SUMMED counts are already the parent's total)."""
    return st.get(k, 0) if k in notes.SUMMED else st.get(k, 0) + sum(total(x, k) for x in st.get("subs") or [])


def tree(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted((root / "notes").rglob("*")) if p.is_file()}


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.root = Path(self.d.name)
        s = Store(self.root)
        s.name("Nimbus", ["nb"], "app")
        s.name("box-7", [], "machine")
        s.remember("box-7", "owner", "Dana", since="2026-09-01")
        self.calls = []
        self.real = notes.chat
        self.addCleanup(setattr, notes, "chat", self.real)

    def stub(self, *replies, tokens=50):
        replies = list(replies)

        def chat(messages, max_tokens=0, **kw):
            self.calls.append(messages)
            r = replies.pop(0)
            content, out = r if isinstance(r, tuple) else (r, tokens)
            return {"content": content}, {"prompt_tokens": 10, "completion_tokens": out}
        notes.chat = chat

    def note(self, text, *replies, root=None, **kw):
        self.stub(*replies)
        return notes.Migrator(root or self.root, **kw).session("memory@x", DAY, text, git_mode=True)

    def test_a_value_only_in_the_reference_list_is_not_taken(self):
        st = self.note("--- memory/a.md\n- Nimbus is on box-7 now\n",
                       "Nimbus | host | box-7 | 2026-10-04\nNimbus | owner | Dana | -\nOrion | owner | Dana | -\n"
                       "Nimbus | status | moved there after the long outage while nobody was watching | -")
        self.assertIn("box-7 | Owner: Dana", self.calls[0][0]["content"])   # memory showed it, the note doesn't say it
        s = Store(self.root)
        self.assertEqual(s.find("Nimbus").attrs["host"].value, "box-7")
        self.assertNotIn("owner", s.find("Nimbus").attrs)
        self.assertIsNone(s.find("Orion"))
        self.assertEqual(st["ungrounded"], 3)
        self.assertEqual(sum("-> not recorded: not in the text" in r for r in st["refusals"]), 3)
        self.assertEqual(len(self.calls), 1)   # nothing went to a repair call
        self.assertEqual(st["refused"], 0)
        self.assertFalse((self.root / "notes" / "nimbus-extra.md").exists())   # nor set aside

    def test_a_kind_alone_makes_no_thing(self):
        st = self.note("--- memory/a.md\n- Vega is a service\n", "= Vega | | service\nVega | kind | service | -\n"
                                                              "Vega | port | 9999 | -")
        self.assertIsNone(Store(self.root).find("Vega"))
        self.assertEqual((st["kind_only"], st["ungrounded"], st["things"]), (1, 1, 0))
        self.assertIn("a kind alone doesn't make a thing", " ".join(st["refusals"]))

    def test_a_kind_before_a_stated_fact_counts_with_it(self):
        self.note("--- memory/a.md\n- Vega is a service on port 9100\n",
                  "Vega | kind | service | -\nVega | port | 9100 | -")
        attrs = Store(self.root).find("Vega").attrs
        self.assertEqual((attrs["kind"].value, attrs["port"].value), ("service", "9100"))

    def test_a_name_the_note_does_not_say_is_not_added(self):
        st = self.note("--- memory/a.md\n- Vega (vg) serves on port 9100\n",
                       "= Vega | vg; Vega Prime | service\nVega | port | 9100 | -")
        self.assertEqual(Store(self.root).find("Vega").aliases, ["vg"])
        self.assertEqual(st["alias_ungrounded"], 1)
        # a name the thing already has is no news, said or not
        st = self.note("--- memory/b.md\n- Nimbus moved to port 9100\n", "= Nimbus | nb | app\nNimbus | port | 9100 | -")
        self.assertNotIn("alias_ungrounded", st)
        self.assertEqual(Store(self.root).find("Nimbus").aliases, ["nb"])

    def test_another_kind_under_a_nickname_is_another_thing(self):
        # Nimbus (an app) is also called nb; "= nb | nb.astro | component" is something else
        st = self.note("--- memory/a.md\n- nb (src/nb.astro) is deployed on the site\n",
                       "= nb | nb.astro | component\nnb | status | deployed | 2026-10-04")
        s = Store(self.root)
        self.assertNotIn("status", s.find("Nimbus").attrs)
        self.assertEqual(s.find("nb.astro").attrs["status"].value, "deployed")
        self.assertEqual(s.find("nb.astro").kind, "component")
        self.assertEqual(s.find("nb").name, "Nimbus")   # the nickname stays Nimbus's
        self.assertEqual(st["nickname_split"], 1)
        # the same kind, or no name of its own: still Nimbus
        for i, decl in enumerate(["= nb | | component", "= nb | nimbus-app | app"]):
            self.note(f"--- memory/b{i}.md\n- nb (nimbus-app) uses port 91{i}0\n",
                      f"{decl}\nnb | port | 91{i}0 | -")
            self.assertEqual(Store(self.root).find("Nimbus").attrs["port"].value, f"91{i}0")

    def test_a_new_thing_the_note_never_names_is_not_made(self):
        st = self.note("--- memory/a.md\n- the gateway listens on port 9100\n",
                       "Zephyr | port | 9100 | -\n= Orion | | service\nOrion | port | 9100 | -")
        s = Store(self.root)
        self.assertIsNone(s.find("Zephyr"))
        self.assertIsNone(s.find("Orion"))
        self.assertEqual(st["unnamed"], 2)
        self.assertIn("the text never names Zephyr", " ".join(st["refusals"]))
        # a thing memory already has needs no naming (here: known only by its other name)
        self.note("--- memory/b.md\n- nb listens on port 9100\n", "Nimbus | port | 9100 | -")
        self.assertEqual(Store(self.root).find("Nimbus").attrs["port"].value, "9100")

    def test_a_chat_needs_its_numbers_from_the_user(self):
        self.stub("Sam | blog port | 8080 | -\nSam | bike | crimson bicycle | -")
        Store(self.root).name("Sam", [], "person")
        st = notes.Migrator(self.root, owner="Sam", prompt=CHAT_PROMPT).session(
            "s1", "2023-05-23", "user: which port should my blog use?\nassistant: Use port 8080 for the blog.\n"
                                "user: ok. I also got a red bike.", "2023-05-23")
        attrs = Store(self.root).find("Sam").attrs
        self.assertNotIn("blog port", attrs)
        self.assertEqual(attrs["bike"].value, "crimson bicycle")
        self.assertEqual(st["ungrounded"], 1)

    def test_a_chat_does_not_take_memorys_own_words_back(self):
        # What memory shows the model about box-7 ("owner: Dana") is reference, not news: copied
        # onto another thing it is refused; what the chat itself says is taken.
        self.stub("Nimbus | owner | Dana | -\nbox-7 | state | slow | -")
        st = notes.Migrator(self.root, owner="Sam", prompt=CHAT_PROMPT).session(
            "s1", DAY, "user: box-7 is getting slow lately", DAY)
        s = Store(self.root)
        self.assertNotIn("owner", s.find("Nimbus").attrs)
        self.assertEqual(s.find("box-7").attrs["state"].value, "slow")
        self.assertEqual(st["copied"], 1)

    def test_without_the_text_or_with_grounding_off_nothing_changes(self):
        raw = ("= Orion | | service\nOrion | kind | service | -\nNimbus | owner | Dana | -\n"
               "Nimbus | host | box-7 | 2026-10-04")
        roots = []
        for kw, text in (({}, ""), ({"ground": None}, ""), ({"ground": None}, "--- memory/a.md\n- unrelated\n")):
            root = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, root, True)
            s = Store(root)
            s.name("Nimbus", ["nb"], "app")
            st = notes.Migrator(root, **kw).session("memory@x", DAY, text, git_mode=True, replay={"raw": raw})
            self.assertNotIn("ungrounded", st)
            roots.append(root)
        self.assertEqual(Store(roots[0]).find("Nimbus").attrs["owner"].value, "Dana")   # applied as logged
        self.assertIsNotNone(Store(roots[0]).find("Orion"))
        self.assertEqual(tree(roots[0]), tree(roots[1]))

    def test_halves_replayed_with_their_text_apply_what_the_run_did(self):
        text = ("--- memory/a.md\n- Nimbus 4.1 is running\n" + "".join(f"- line {i}: Nimbus port {9000 + i}\n"
                                                                       for i in range(120))
                + "- Nimbus moved to box-7, Vega (vg) is a service on port 9300\n")
        loop = "Nimbus | status | running | 2026-10-04\n" + "Nimbus | port | 9000 | 2026-10-04\n" * 300
        st = self.note(text, (loop, notes.MAX_TOKENS),
                       ("Nimbus | version | 4.1 | 2026-10-04\nNimbus | owner | Dana | -\nZephyr | port | 9001 | -", 50),
                       ("Nimbus | host | box-7 | 2026-10-04\n= Vega | vg; Vega Prime | service\nVega | kind | service | -\n"
                        "Vega | port | 9300 | -\nVega | host | box-9 | -", 50))
        self.assertEqual(len(st["subs"]), 2)
        self.assertEqual([total(st, k) for k in ("ungrounded", "unnamed", "alias_ungrounded", "kind_only")],
                         [2, 1, 1, 0])
        other = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, other, True)
        s = Store(other)
        s.name("Nimbus", ["nb"], "app")
        s.name("box-7", [], "machine")
        s.remember("box-7", "owner", "Dana", since="2026-09-01")
        notes.chat = None
        rst = notes.Migrator(other).session("memory@x", DAY, text, git_mode=True, replay=json.loads(json.dumps(st)))
        keys = notes.SUMMED[2:] + ("unnamed", "kind_only", "alias_ungrounded", "lost")   # not the token counts
        self.assertEqual({k: total(rst, k) for k in keys}, {k: total(st, k) for k in keys})
        self.assertEqual([x.get("ungrounded") for x in rst["subs"]], [x.get("ungrounded") for x in st["subs"]])
        self.assertEqual(tree(other), tree(self.root))
        # without the text the halves apply every logged line (how replays ran until 10-19)
        bare = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, bare, True)
        Store(bare).name("Nimbus", ["nb"], "app")
        notes.Migrator(bare).session("memory@x", DAY, "", git_mode=True, replay=json.loads(json.dumps(st)))
        self.assertIsNotNone(Store(bare).find("Zephyr"))

    def test_a_note_receipt_is_the_words_around_the_value(self):
        line = "- " + "some earlier words " * 20 + "Nimbus now listens on port 9100 " + "and later words " * 20
        self.note("--- memory/a.md\n" + line + "\n", "Nimbus | port | 9100 | -")
        said = Store(self.root).find("Nimbus").attrs["port"].said
        self.assertIn("9100", said[0])
        self.assertLessEqual(len(said[0]), 200)
        self.assertTrue(said[0].startswith("…"))


class RunTest(unittest.TestCase):
    def setUp(self):
        self.real = (notes.chat, notes.OUTAGE_TRIES)
        self.addCleanup(lambda: (setattr(notes, "chat", self.real[0]), setattr(notes, "OUTAGE_TRIES", self.real[1])))

    def test_each_logged_row_gets_its_text_beside_it_and_a_replay_uses_it(self):
        answers = {"memory@a": "= Vega | vg | service\nVega | port | 9100 | -\nVega | host | box-9 | -",
                   "memory@b": "Vega | version | 2.1 | 2026-10-05\nVega | owner | Tamsin | -"}

        def chat(messages, max_tokens=0, **kw):
            name = messages[1]["content"].split(":")[0].removeprefix("Note ")
            if name == "memory@c":
                raise OSError("the model is down")
            return {"content": answers[name]}, {"completion_tokens": 20}
        notes.chat, notes.OUTAGE_TRIES = chat, 1
        texts = {"memory@a": "--- memory/a.md\n- Vega (vg) serves on port 9100\n",
                 "memory@b": "--- memory/b.md\n- Vega 2.1 shipped\n", "memory@c": "--- memory/c.md\n- x\n"}
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as d2:
            out = Path(d)
            jobs = [(DAY, DAY, "memory@a", 0, texts["memory@a"], 1), ("2026-10-05", "2026-10-05", "memory@b", 0,
                                                                         texts["memory@b"], 1),
                    ("2026-10-06", "2026-10-06", "memory@c", 0, texts["memory@c"], 1)]
            said = []
            notes.run(out, jobs, workers=1, say=said.append)
            rows = [json.loads(l) for l in (out / "migrate-log.jsonl").read_text().splitlines()]
            side = [json.loads(l) for l in (out / "migrate-texts.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(side, [{"note": r["note"], "chunk": r["chunk"], "text": texts[r["note"]]} for r in rows])
            self.assertEqual(len((out / "migrate-errors.jsonl").read_text().splitlines()), 1)
            self.assertEqual(sum(r.get("ungrounded", 0) for r in rows), 2)
            self.assertIn("ungrounded 2", said[-1])
            # the replay of the logged run, with the sidecar, builds the same notes
            notes.chat = None
            with contextlib.redirect_stdout(io.StringIO()):
                notes.replay(out / "migrate-log.jsonl", Path(d2) / "r", texts=out / "migrate-texts.jsonl")
            self.assertEqual(tree(Path(d2) / "r"), tree(out))
            self.assertNotIn("host", Store(Path(d2) / "r").find("Vega").attrs)
            # the same texts as a json object
            with contextlib.redirect_stdout(io.StringIO()):
                notes.replay(out / "migrate-log.jsonl", Path(d2) / "j", texts={f"{k}#0": v for k, v in texts.items()})
            self.assertEqual(tree(Path(d2) / "j"), tree(out))


if __name__ == "__main__":
    unittest.main()
