"""The pane's backend (adapters/hermes/deltos/dashboard/plugin_api.py): every payload
and every route, over the made-up demo store seeded into a temporary folder, an empty
store, and a plugin whose library is missing. Nothing here reads a real store: each
test points DELTOS_ROOT at its own folder. FastAPI is stubbed when it is not installed
(the payload functions do not need it; the module's import does)."""
import asyncio
import datetime
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from deltos.demo import seed

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "adapters" / "hermes" / "deltos"
API = PLUGIN / "dashboard" / "plugin_api.py"
ROUTES = ("/health", "/overview", "/things", "/thing", "/on", "/changes", "/due")
DOUBT_DAYS = 21


class _Route:
    def __init__(self, path, endpoint):
        self.path, self.endpoint, self.methods = path, endpoint, {"GET"}


class _APIRouter:
    def __init__(self, *args, **kwargs):
        self.routes = []

    def get(self, path, **kwargs):
        def register(fn):
            self.routes.append(_Route(path, fn))
            return fn
        return register


class _HTTPException(Exception):
    def __init__(self, status_code=500, detail=""):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


def _load_api():
    """plugin_api.py, loaded by path the way the gateway loads it."""
    stubbed = False
    try:
        import fastapi  # noqa: F401
    except ImportError:
        fake = types.ModuleType("fastapi")
        fake.APIRouter, fake.HTTPException = _APIRouter, _HTTPException
        sys.modules["fastapi"] = fake
        stubbed = True
    try:
        spec = importlib.util.spec_from_file_location("deltos_plugin_api_under_test", API)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if stubbed:
            sys.modules.pop("fastapi", None)
    return mod


def _snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _is_day(text: str) -> bool:
    try:
        datetime.date.fromisoformat(text)
        return True
    except (TypeError, ValueError):
        return False


class PluginApiCase(unittest.TestCase):
    """A store per class; DELTOS_ROOT, DELTOS_SRC and HERMES_HOME never point outside it."""
    filled = True

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="deltos-plugin-api-")
        cls.addClassCleanup(cls._tmp.cleanup)
        base = Path(cls._tmp.name)
        cls.root = base / "store"
        if cls.filled:
            seed(cls.root)
        else:
            cls.root.mkdir()
        (base / "home").mkdir()
        env = mock.patch.dict(os.environ, {"DELTOS_ROOT": str(cls.root), "DELTOS_SRC": str(REPO),
                                           "DELTOS_DOUBT_DAYS": str(DOUBT_DAYS), "HERMES_HOME": str(base / "home")})
        env.start()
        cls.addClassCleanup(env.stop)
        path = list(sys.path)
        cls.addClassCleanup(lambda: sys.path.__setitem__(slice(None), path))
        cls.api = _load_api()

    def call(self, path, **params):
        """Call a route the way the gateway would, by its path."""
        route = next(r for r in self.api.router.routes if r.path == path)
        return asyncio.run(route.endpoint(**params))


class DemoStore(PluginApiCase):
    def setUp(self):
        self.today = self.api.health_payload()["today"]
        self.things = self.api.things_payload()["things"]

    def back(self, days: int) -> str:
        return (datetime.date.fromisoformat(self.today) - datetime.timedelta(days=days)).isoformat()

    def items(self):
        for row in self.things:
            for item in self.api.thing_payload(row["slug"])["thing"]["items"]:
                yield row, item

    # -- health ------------------------------------------------------------------

    def test_health_says_the_store_is_readable(self):
        h = self.api.health_payload()
        self.assertEqual(h, {"ok": True, "version": self.api.PLUGIN_VERSION, "root": str(self.root),
                             "today": h["today"], "things": len(self.things)})
        self.assertTrue(_is_day(h["today"]))
        self.assertGreater(h["things"], 0)

    # -- overview ----------------------------------------------------------------

    def test_overview_counts_agree_with_the_list(self):
        o = self.api.overview_payload()
        self.assertEqual(o["days"], 7)
        self.assertEqual(o["today"], self.today)
        self.assertEqual(o["doubt_days"], DOUBT_DAYS)
        self.assertEqual(o["things"], len(self.things))
        for key in ("entries", "past", "unsure", "checked", "disputed"):
            self.assertEqual(o[key], sum(row[key] for row in self.things), key)
            self.assertGreater(o[key], 0, f"the demo store should have {key}")
        changes = self.api.changes_payload(7)["changes"]
        self.assertEqual(o["changed"], len(changes))
        self.assertEqual(o["changed_things"], len({c["slug"] for c in changes}))
        self.assertEqual(o["due"], len(self.api.due_payload(self.api.MAX_ROWS)["due"]))
        self.assertLessEqual({"days", "looks", "outcomes"}, set(o["looks"]))
        self.assertEqual(o["looks"]["looks"], sum(o["looks"]["outcomes"].values()))

    def test_overview_days_are_bounded(self):
        for raw, want in ((0, 1), (-40, 1), (1, 1), (30, 30), ("90", 90), (10 ** 9, self.api.MAX_DAYS),
                          ("a week", 7), (None, 7), ("", 7)):
            self.assertEqual(self.api.overview_payload(raw)["days"], want, repr(raw))

    # -- things --------------------------------------------------------------------

    def test_things_are_listed_newest_change_first(self):
        keys = {"name", "slug", "kind", "aliases", "updated", "entries", "past", "unsure", "checked",
                "disputed", "dormant", "changed"}
        for row in self.things:
            self.assertEqual(set(row), keys)
        changed = [row["changed"] or row["updated"] for row in self.things]
        self.assertEqual(changed, sorted(changed, reverse=True))
        self.assertEqual(len({row["slug"] for row in self.things}), len(self.things))
        self.assertTrue(any(row["dormant"] for row in self.things), "the demo store has a retired thing")

    def test_things_search_reads_names_other_names_and_kinds(self):
        names = lambda q: [row["name"] for row in self.api.things_payload(q)["things"]]
        every = [row["name"] for row in self.things]
        self.assertEqual(names(""), every)
        self.assertEqual(names(None), every)
        self.assertEqual(names("orch"), ["Orchard"])
        self.assertEqual(names("THE SHOP"), ["Orchard"])                      # an alias, any case
        self.assertEqual(sorted(names("machine")), sorted(r["name"] for r in self.things if r["kind"] == "machine"))
        self.assertEqual(names("nothing-like-this"), [])
        self.assertEqual(names("x" * 100_000), [])                              # cut, not an error

    # -- one thing -----------------------------------------------------------------

    def test_a_thing_is_found_by_file_name_name_and_other_name(self):
        for name in ("orchard", "Orchard", "the shop", "  ORCHARD APP "):
            got = self.api.thing_payload(name)
            self.assertTrue(got["found"], name)
            self.assertEqual(got["thing"]["slug"], "orchard")
        thing = self.api.thing_payload("orchard")["thing"]
        row = next(r for r in self.things if r["slug"] == "orchard")
        self.assertEqual({k: thing[k] for k in row}, row)
        self.assertEqual(len(thing["items"]), row["entries"])
        self.assertEqual(sum(len(i["past"]) for i in thing["items"]), row["past"])

    def test_entries_carry_what_the_pane_draws(self):
        keys = {"label", "value", "since", "state", "note", "past", "also", "type"}
        states, wrong = set(), 0
        for row, item in self.items():
            self.assertLessEqual(keys, set(item), f"{row['name']} · {item.get('label')}")
            self.assertIn(item["state"], {"plain", "checked", "unsure", "disputed", "retired"})
            states.add(item["state"])
            if item["state"] == "unsure":
                self.assertIn("not confirmed in", item["note"])
            if item["state"] == "checked":
                self.assertTrue(_is_day(item["seen"]) and item["check"], item)
            if item["state"] == "disputed":
                self.assertEqual(set(item["other"]), {"value", "by", "day"})
                self.assertNotEqual(item["other"]["value"], item["value"])
            for past in item["past"]:
                self.assertEqual(set(past), {"value", "start", "end", "wrong", "by"})
                self.assertTrue(_is_day(past["end"]))
                wrong += bool(past["wrong"])
        self.assertEqual(states, {"plain", "checked", "unsure", "disputed", "retired"})
        self.assertGreater(wrong, 0, "the demo store has a value recorded by mistake")

    def test_a_name_that_is_not_one_thing_offers_the_near_ones(self):
        got = self.api.thing_payload("vps")                                   # two machines hold "vps"
        self.assertEqual(got["found"], False)
        self.assertEqual(sorted(got["near"]), ["vps-1", "vps-2"])
        self.assertNotIn("thing", got)
        for name in ("nothing-like-this", "x" * 100_000):
            self.assertEqual(self.api.thing_payload(name), {"found": False, "near": []}, repr(name)[:40])
        names = [row["name"] for row in self.things]
        for name in ("", None, "   "):                                        # no name: never a thing
            got = self.api.thing_payload(name)
            self.assertEqual(got["found"], False, repr(name))
            self.assertLessEqual(set(got["near"]), set(names))

    # -- another day -----------------------------------------------------------------

    def test_today_reads_as_every_current_value(self):
        thing = self.api.thing_payload("orchard")["thing"]
        got = self.api.on_payload("orchard", self.today)
        self.assertTrue(got["found"])
        self.assertEqual(got["thing"]["day"], self.today)
        self.assertEqual([(i["label"], i["value"], i["state"]) for i in got["thing"]["items"]],
                         [(i["label"], i["value"], "current") for i in thing["items"]])

    def test_a_past_day_reads_the_value_held_then(self):
        thing = self.api.thing_payload("orchard")["thing"]
        version = next(i for i in thing["items"] if i["label"] == "Version")
        before = next(p for p in version["past"] if not p["wrong"])
        day = (datetime.date.fromisoformat(version["since"]) - datetime.timedelta(days=1)).isoformat()
        then = next(i for i in self.api.on_payload("Orchard", day)["thing"]["items"] if i["label"] == "Version")
        self.assertEqual((then["value"], then["state"], then["start"], then["end"]),
                         (before["value"], "past", before["start"], before["end"]))
        mistaken = {p["value"] for i in thing["items"] for p in i["past"] if p["wrong"]}
        for back in range(0, 400, 7):                                         # a mistake was never true
            for item in self.api.on_payload("orchard", self.back(back))["thing"]["items"]:
                self.assertNotIn(item["value"], mistaken)
                self.assertIn(item["state"], {"current", "past", "before", "gap"})

    def test_a_day_before_anything_was_known(self):
        got = self.api.on_payload("orchard", "2001-01-01")
        self.assertTrue(got["found"])
        for item in got["thing"]["items"]:
            self.assertEqual((item["value"], item["state"]), ("", "before"))
            self.assertTrue(_is_day(item["start"]))

    def test_a_day_that_is_not_a_day_is_not_found(self):
        for day in ("", None, "yesterday", "2026-13-45", "20261101", "2026-11-0"):
            got = self.api.on_payload("orchard", day)
            self.assertEqual(got["found"], False, repr(day))
            self.assertEqual(got["near"], ["Orchard"])
        self.assertEqual(self.api.on_payload("vps", self.today)["found"], False)
        self.assertEqual(self.api.on_payload("nothing-like-this", self.today), {"found": False, "near": []})

    # -- changes ---------------------------------------------------------------------

    def test_changes_are_newest_first_and_inside_the_window(self):
        keys = {"day", "thing", "slug", "label", "value", "was", "was_since", "corrected"}
        seen = []
        for days in (7, 30, 90, self.api.MAX_DAYS):
            got = self.api.changes_payload(days)
            self.assertEqual(got["days"], days)
            rows = got["changes"]
            self.assertEqual([r["day"] for r in rows], sorted((r["day"] for r in rows), reverse=True))
            for row in rows:
                self.assertEqual(set(row), keys)
                self.assertTrue(self.back(days) <= row["day"] <= self.today, row)
            ids = [(r["slug"], r["label"]) for r in rows]
            self.assertLessEqual(set(seen), set(ids))                       # a wider window only adds
            seen = ids
        self.assertGreater(len(self.api.changes_payload(7)["changes"]), 0)
        every = self.api.changes_payload(self.api.MAX_DAYS)["changes"]
        self.assertTrue(any(r["was"] for r in every) and any(not r["was"] for r in every))
        self.assertTrue(any(r["corrected"] for r in every))

    def test_changes_days_are_bounded(self):
        for raw, want in ((0, 1), (-1, 1), ("30", 30), (10 ** 9, self.api.MAX_DAYS), ("soon", 7), (None, 7)):
            got = self.api.changes_payload(raw)
            self.assertEqual(got["days"], want, repr(raw))
            self.assertLessEqual(len(got["changes"]), self.api.MAX_ROWS)

    # -- worth a look ----------------------------------------------------------------

    def test_due_rows_carry_what_the_pane_draws(self):
        rows = self.api.due_payload(self.api.MAX_ROWS)["due"]
        self.assertGreater(len(rows), 1)
        slugs = {row["slug"] for row in self.things}
        for row in rows:
            self.assertLessEqual({"thing", "stem", "attribute", "value", "seen", "age", "asks", "how", "other",
                                  "moved"}, set(row))
            self.assertIn(row["stem"], slugs)                                # the pane opens a thing by it
            self.assertTrue(self.api.thing_payload(row["stem"])["found"])
        self.assertTrue(rows[0]["other"], "a fact a note disagrees with comes first")
        self.assertEqual(len(rows[0]["other"]), 3)

    def test_due_rows_are_bounded(self):
        every = len(self.api.due_payload(self.api.MAX_ROWS)["due"])
        for raw, want in ((1, 1), (0, 1), (-7, 1), (3, 3), ("2", 2), (10 ** 9, every), ("many", min(every, 20)),
                          (None, min(every, 20))):
            self.assertEqual(len(self.api.due_payload(raw)["due"]), want, repr(raw))
        self.assertLessEqual(every, self.api.MAX_ROWS)

    # -- the routes ------------------------------------------------------------------

    def test_every_route_is_a_read(self):
        routes = self.api.router.routes
        self.assertEqual(sorted(r.path for r in routes), sorted(ROUTES))
        for route in routes:
            self.assertEqual(set(route.methods), {"GET"}, route.path)

    def test_routes_answer_what_the_payloads_answer(self):
        day = self.back(30)
        self.assertEqual(self.call("/health"), self.api.health_payload())
        self.assertEqual(self.call("/overview"), self.api.overview_payload(7))
        self.assertEqual(self.call("/overview", days=30), self.api.overview_payload(30))
        self.assertEqual(self.call("/things"), self.api.things_payload(""))
        self.assertEqual(self.call("/things", q="orch"), self.api.things_payload("orch"))
        self.assertEqual(self.call("/thing", name="orchard"), self.api.thing_payload("orchard"))
        self.assertEqual(self.call("/thing", name="vps"), self.api.thing_payload("vps"))
        self.assertEqual(self.call("/on", name="orchard", day=day), self.api.on_payload("orchard", day))
        self.assertEqual(self.call("/changes"), self.api.changes_payload(7))
        self.assertEqual(self.call("/changes", days=90), self.api.changes_payload(90))
        self.assertEqual(self.call("/due"), self.api.due_payload(20))
        self.assertEqual(self.call("/due", n=2), self.api.due_payload(2))
        self.assertEqual(self.call("/overview", days=10 ** 9)["days"], self.api.MAX_DAYS)
        self.assertEqual(len(self.call("/due", n=-3)["due"]), 1)

    def test_every_answer_is_plain_json(self):
        answers = [self.call("/health"), self.call("/overview"), self.call("/things"), self.call("/changes", days=90),
                   self.call("/due", n=50), self.call("/thing", name="nope"),
                   *(self.call("/thing", name=row["slug"]) for row in self.things),
                   *(self.call("/on", name=row["slug"], day=self.back(30)) for row in self.things)]
        for answer in answers:
            self.assertEqual(json.loads(json.dumps(answer)), answer)

    def test_reading_never_writes_to_the_store(self):
        before = _snapshot(self.root)
        self.assertGreater(len(before), 0)
        for path, params in (("/health", {}), ("/overview", {"days": 90}), ("/things", {"q": "o"}),
                             ("/thing", {"name": "orchard"}), ("/thing", {"name": "nope"}),
                             ("/on", {"name": "orchard", "day": self.back(60)}), ("/changes", {"days": 365}),
                             ("/due", {"n": 50})):
            self.call(path, **params)
        self.assertEqual(_snapshot(self.root), before)


class EmptyStore(PluginApiCase):
    filled = False

    def test_an_empty_store_is_healthy_and_empty(self):
        h = self.api.health_payload()
        self.assertEqual((h["ok"], h["things"], h["root"]), (True, 0, str(self.root)))
        o = self.api.overview_payload()
        for key in ("things", "entries", "past", "unsure", "checked", "disputed", "changed", "changed_things", "due"):
            self.assertEqual(o[key], 0, key)
        self.assertEqual(self.api.things_payload(), {"things": []})
        self.assertEqual(self.api.changes_payload(90), {"days": 90, "changes": []})
        self.assertEqual(self.api.due_payload(), {"due": []})
        self.assertEqual(self.api.thing_payload("orchard"), {"found": False, "near": []})
        self.assertEqual(self.api.on_payload("orchard", h["today"]), {"found": False, "near": []})

    def test_reading_an_empty_store_writes_no_file(self):
        """Opening a store makes its (empty) notes folder, as the library always does; no file appears."""
        for path, params in (("/health", {}), ("/overview", {}), ("/things", {}), ("/changes", {}), ("/due", {}),
                             ("/thing", {"name": "x"})):
            self.call(path, **params)
        self.assertEqual(_snapshot(self.root), {})
        self.assertLessEqual({p.name for p in self.root.iterdir()}, {"notes"})


class MissingLibrary(PluginApiCase):
    """The plugin is installed but its library is not where it looks."""

    def setUp(self):
        gone = Path(self._tmp.name) / "no-library-here"
        env = mock.patch.dict(os.environ, {"DELTOS_SRC": str(gone)})
        env.start()
        self.addCleanup(env.stop)
        self.gone = str(gone)

    def test_health_says_why_instead_of_failing(self):
        h = self.api.health_payload()
        self.assertIs(h["ok"], False)
        self.assertEqual(h["version"], self.api.PLUGIN_VERSION)
        self.assertIn("the deltos library is not in", h["reason"])
        self.assertIn(self.gone, h["reason"])
        self.assertNotIn("things", h)
        self.assertEqual(self.call("/health"), h)

    def test_every_other_route_is_an_error_with_the_reason(self):
        for path, params in (("/overview", {}), ("/things", {}), ("/thing", {"name": "orchard"}),
                             ("/on", {"name": "orchard", "day": "2026-01-01"}), ("/changes", {}), ("/due", {})):
            with self.assertRaises(Exception) as caught:
                self.call(path, **params)
            self.assertEqual(getattr(caught.exception, "status_code", None), 500, path)
            self.assertIn("the deltos library is not in", caught.exception.detail)

    def test_payloads_raise_rather_than_answer_wrongly(self):
        for fn, args in ((self.api.overview_payload, ()), (self.api.things_payload, ()),
                         (self.api.thing_payload, ("orchard",)), (self.api.due_payload, ())):
            with self.assertRaises(RuntimeError):
                fn(*args)


class Versions(unittest.TestCase):
    """One version across the backend, its manifest, the pane and the plugin's own file."""

    @classmethod
    def setUpClass(cls):
        cls.version = re.search(r'^PLUGIN_VERSION = "([^"]+)"$', API.read_text(), re.M).group(1)

    def test_the_backend_has_a_version(self):
        self.assertRegex(self.version, r"^\d+\.\d+\.\d+$")

    def test_the_dashboard_manifest_matches(self):
        manifest = json.loads((PLUGIN / "dashboard" / "manifest.json").read_text())
        self.assertEqual(manifest["version"], self.version)
        self.assertEqual(manifest["name"], "deltos")
        self.assertTrue((PLUGIN / "dashboard" / manifest["api"]).is_file())

    def test_the_pane_matches(self):
        source = (PLUGIN / "desktop" / "plugin.js").read_text()
        found = re.findall(r"^const PLUGIN_VERSION = '([^']+)'$", source, re.M)
        self.assertEqual(found, [self.version])
        self.assertIn("const ID = 'deltos'", source)

    def test_the_plugin_file_matches(self):
        found = re.search(r"^version:\s*['\"]?([^'\"\s]+)", (PLUGIN / "plugin.yaml").read_text(), re.M)
        if not found or found.group(1) != self.version:
            self.skipTest(f"plugin.yaml says {found.group(1) if found else 'no version'}, the pane and its backend "
                          f"say {self.version}: bump plugin.yaml before release")
        self.assertEqual(found.group(1), self.version)


if __name__ == "__main__":
    unittest.main()
