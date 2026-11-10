#!/usr/bin/env python3
"""Write the pane's test fixtures from the made-up demo store (deltos/demo.py).

Every fixture is what the backend really answers: the demo store is seeded into a
temporary folder, DELTOS_ROOT points at it, and each payload function in
adapters/hermes/deltos/dashboard/plugin_api.py is called the way its route calls it.
Nothing is typed by hand, so the shapes the pane is tested against are the real ones.

    python3 tests/desktop/make_fixtures.py          # from the repository root

Writes tests/desktop/fixtures/{demo,empty,down}.json: a map of request -> answer.
The demo store counts its dates back from the day it is seeded, so a re-run moves
every date; the tests read the dates from the fixtures instead of fixing them.
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
API = REPO / "adapters" / "hermes" / "deltos" / "dashboard" / "plugin_api.py"
OUT = HERE / "fixtures"
SHOWN_ROOT = "~/deltos-demo"          # what the fixtures say instead of the temp folder
DOUBT_DAYS = "21"
AGO = (7, 30, 90, 365)               # the pane's quick steps back in time (STEPS in plugin.js)


def load_api():
    try:
        import fastapi  # noqa: F401
    except ImportError:                # the payload functions need no fastapi; the module does
        import types
        fake = types.ModuleType("fastapi")

        class APIRouter:
            def get(self, _path):
                return lambda fn: fn

        class HTTPException(Exception):
            def __init__(self, status_code=500, detail=""):
                super().__init__(detail)
                self.status_code, self.detail = status_code, detail

        fake.APIRouter, fake.HTTPException = APIRouter, HTTPException
        sys.modules["fastapi"] = fake
    spec = importlib.util.spec_from_file_location("deltos_plugin_api_fixtures", API)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def scrub(data, root: str):
    """The temp folder never reaches a fixture."""
    return json.loads(json.dumps(data).replace(root, SHOWN_ROOT))


def store_routes(api, root: str, with_things: bool) -> dict:
    routes = {"/health": api.health_payload()}
    for days in (7, 30, 90):
        routes[f"/overview?days={days}"] = api.overview_payload(days)
        routes[f"/changes?days={days}"] = api.changes_payload(days)
    routes["/things"] = api.things_payload("")
    routes["/due?n=50"] = api.due_payload(50)
    if with_things:
        today = datetime.date.fromisoformat(routes["/health"]["today"])
        for row in routes["/things"]["things"]:
            slug = row["slug"]
            routes[f"/thing?name={slug}"] = api.thing_payload(slug)
            # The days the pane can jump to: its quick steps, and every day this thing changed
            # (the dots on its history line).
            days = {(today - datetime.timedelta(days=ago)).isoformat() for ago in AGO}
            for item in routes[f"/thing?name={slug}"]["thing"]["items"]:
                days.update(d for d in [item["since"], *(p["start"] for p in item["past"] if not p["wrong"])] if d)
            for day in sorted(days):
                if day < today.isoformat():
                    routes[f"/on?name={slug}&day={day}"] = api.on_payload(slug, day)
        for q in ("orch", "machine", "zzz"):
            routes[f"/things?q={q}"] = api.things_payload(q)
        routes["/thing?name=vps"] = api.thing_payload("vps")              # two things match: not found, near
        routes["/thing?name=nothing-like-this"] = api.thing_payload("nothing-like-this")
    return scrub(routes, root)


def main() -> int:
    sys.path.insert(0, str(REPO))
    from deltos.demo import seed

    api = load_api()
    OUT.mkdir(parents=True, exist_ok=True)
    kept = {k: os.environ.get(k) for k in ("DELTOS_ROOT", "DELTOS_DOUBT_DAYS", "DELTOS_SRC")}
    try:
        with tempfile.TemporaryDirectory(prefix="deltos-fixtures-") as tmp:
            demo, empty = Path(tmp) / "demo", Path(tmp) / "empty"
            empty.mkdir()
            seed(demo)
            os.environ["DELTOS_DOUBT_DAYS"] = DOUBT_DAYS
            os.environ.pop("DELTOS_SRC", None)

            os.environ["DELTOS_ROOT"] = str(demo)
            (OUT / "demo.json").write_text(json.dumps(store_routes(api, str(demo), True), indent=1) + "\n")

            os.environ["DELTOS_ROOT"] = str(empty)
            (OUT / "empty.json").write_text(json.dumps(store_routes(api, str(empty), False), indent=1) + "\n")

            # The library is not where the plugin looks: health says why, every other route is a 500.
            os.environ["DELTOS_SRC"] = "/opt/deltos-not-here"
            (OUT / "down.json").write_text(json.dumps({"/health": api.health_payload()}, indent=1) + "\n")
    finally:
        for k, v in kept.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    for name in ("demo", "empty", "down"):
        print(f"{name}.json: {len(json.loads((OUT / (name + '.json')).read_text()))} requests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
