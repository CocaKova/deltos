import ast
import contextlib
import hashlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from deltos import store
from deltos.config import Config
from deltos.doctor import doctor, report
from deltos.lint import lint, main
from deltos.store import Attr, Store, Thing, name_problem, render_thing, slug

DAY = "2026-06-01"


def note(root: Path, name: str, kind: str, entries: dict[str, str], aliases=()):
    """Write a profile by hand, the way an older import left it (the store refuses these names now)."""
    t = Thing(name, list(aliases), kind, DAY, DAY, attrs={k.lower(): Attr(k, v, DAY) for k, v in entries.items()},
              file=slug(name))
    (root / "notes").mkdir(parents=True, exist_ok=True)
    (root / "notes" / f"{t.file}.md").write_text(render_thing(t))


def sums(d: Path) -> dict[str, str]:
    return {str(p.relative_to(d)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.rglob("*")) if p.is_file()}


class LintTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.root = Path(self.d.name)
        note(self.root, "Harbor relay", "service", {"Host": "edge-1", "Port": "9000"})     # clean
        note(self.root, "Quill gateway", "service", {})                                    # empty
        note(self.root, "Copperline", "tool", {"Kind": "tool"})                            # kind-only, kind-dup
        note(self.root, "35.9%", "", {"Status": "measured"})                               # name-rule
        note(self.root, "--no-sandbox", "setting", {"Default": "off"})                     # flag-like
        note(self.root, "Lantern", "app", {"Status": "live"})                              # plain-word
        note(self.root, "Pinecrest API", "service", {"Kind": "service", "Host": "edge-2"})  # kind-dup
        # 'port' is a label on 22 profiles: a thing whose kind is 'port' is a value made into a thing.
        for i in range(21):
            note(self.root, f"Station {chr(65 + i)}{chr(65 + i)}", "machine", {"Port": str(8000 + i)})
        note(self.root, "Bolt listener", "port", {"Owner": "Station AA"})                  # value-kind
        # 'host' is a label on 20 profiles, this one among them: 19 others are not enough.
        for i in range(17):   # and Harbor relay, Pinecrest API
            note(self.root, f"Relay {chr(65 + i)}{chr(65 + i)}", "machine", {"Host": f"edge-{i}"})
        note(self.root, "Uplink pool", "host", {"Host": "edge-9"})

    def tearDown(self):
        self.d.cleanup()

    def table(self) -> dict[str, list[str]]:
        return {name: sig for _, name, sig in lint(self.root)["rows"]}

    def test_each_signal(self):
        self.assertEqual(self.table(), {
            "Quill gateway": ["empty"],
            "Copperline": ["kind-only", "kind-dup"],
            "35.9%": ["name-rule"],
            "--no-sandbox": ["flag-like"],
            "Lantern": ["plain-word"],
            "Pinecrest API": ["kind-dup"],
            "Bolt listener": ["value-kind"],
        })
        res = lint(self.root)
        self.assertEqual(res["total"], 47)
        self.assertEqual(res["counts"]["kind-dup"], 2)
        self.assertIn(("no-sandbox", "--no-sandbox", ["flag-like"]), res["rows"])

    def test_doctor_shows_the_count_and_the_hold(self):
        out = doctor(self.root)
        self.assertTrue(out.startswith("deltos "), out.splitlines()[0])
        self.assertIn("Things with junk signals: 6 (empty 1, kind-only 1, value-kind 1, name-rule 1, flag-like 1, "
                      f"plain-word 1): python3 -m deltos.lint {self.root}", report(self.root))
        (self.root / "import.hold").write_text("note run 20261019-0200 held: loops 10 against 1\nmore numbers\n")
        self.assertEqual(doctor(self.root).splitlines()[0], "held: note run 20261019-0200 held: loops 10 against 1")
        (self.root / "import.hold").unlink()
        (self.root / "learn.hold").write_text("chat run 20261019-0300 held: lost 7\n")
        self.assertEqual(doctor(self.root).splitlines()[0], "held: chat run 20261019-0300 held: lost 7")

    def test_fix_without_apply_changes_nothing(self):
        before = sums(self.root)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main([str(self.root), "--fix"]), 0)
        self.assertEqual(sums(self.root), before)
        self.assertFalse((self.root / store.TRASH).exists())
        text = out.getvalue()
        for line in ("- forget Quill gateway [quill-gateway] (lint: empty)",
                     "- forget Copperline [copperline] (lint: kind-only)",
                     "- forget 35.9% [35-9] (lint: name-rule)",
                     "- forget --no-sandbox [no-sandbox] (lint: flag-like)",
                     "- drop Pinecrest API [pinecrest-api] Kind: service"):
            self.assertIn(line, text.splitlines())
        review = text.split("For a person to review, never proposed: 2 things\n")[1]
        self.assertIn("- Bolt listener [bolt-listener]: value-kind (kind 'port', a label on 22 profiles; 1 entry)", review)
        self.assertIn("- Lantern [lantern]: plain-word (1 entry)", review)

    def test_apply_forgets_the_proposal_and_restore_brings_one_back(self):
        with contextlib.redirect_stdout(io.StringIO()):
            main([str(self.root), "--apply"])
        notes = self.root / "notes"
        for stem in ("quill-gateway", "copperline", "35-9", "no-sandbox"):
            self.assertFalse((notes / f"{stem}.md").exists(), stem)
        for stem in ("bolt-listener", "lantern", "harbor-relay", "pinecrest-api"):   # review and clean: kept
            self.assertTrue((notes / f"{stem}.md").exists(), stem)
        s = Store(self.root)
        self.assertNotIn("kind", s.find("Pinecrest API").attrs)
        self.assertEqual(s.find("Pinecrest API").attrs["host"].value, "edge-2")
        log = [json.loads(x) for x in (self.root / store.FORGET_LOG).read_text().splitlines()]
        self.assertEqual({e["thing"]: e["reason"] for e in log}, {
            "Quill gateway": "lint: empty", "Copperline": "lint: kind-only", "35.9%": "lint: name-rule",
            "--no-sandbox": "lint: flag-like", "Pinecrest API": "lint: kind-dup"})
        self.assertEqual({e["source"] for e in log}, {"person"})
        self.assertEqual(s.restore("--no-sandbox"), "restored --no-sandbox")
        self.assertEqual(s.find("--no-sandbox").attrs["default"].value, "off")
        self.assertEqual(s.restore("Pinecrest API"), "restored Pinecrest API's Kind")
        self.assertEqual(s.find("Pinecrest API").attrs["kind"].value, "service")
        self.assertEqual(self.table()["--no-sandbox"], ["flag-like"])   # back, and still junk

    def test_flag_names_are_refused(self):
        self.assertIn("command flag", name_problem("--no-sandbox"))
        self.assertIn("command flag", name_problem("-v"))
        self.assertEqual(name_problem("-"), "empty")
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d), today=DAY)
            self.assertIn("not created", s.remember("--attempts", "default", "3", create=True, confirm=False))
            self.assertIsNone(s.find("--attempts"))
            self.assertIn("refused as names: --edge", s.name("Edge runner", ["--edge"]))

    def test_refusal_texts_name_no_owner_or_agent(self):
        # Every text the store can answer a writer with (its string constants), against the
        # owner and agent this machine's memory names, when it has one (read only).
        texts = [n.value for n in ast.walk(ast.parse(Path(store.__file__).read_text()))
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        cfg = Config.load()
        for who in {x for x in (cfg.owner, cfg.agent) if x}:
            self.assertEqual([t for t in texts if re.search(rf"\b{re.escape(who)}\b", t)], [])
        # The examples use the made-up names the configuration docs use.
        self.assertIn("('Ava's plan')", name_problem("Sam's old plan for the lab"))


if __name__ == "__main__":
    unittest.main()
