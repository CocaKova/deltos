"""deltos: the pane's backend.

Read-only routes over the store's view (deltos/view.py): the things memory knows,
one thing's entries with every value they had, what changed lately, what is due for
a look, and what was true on a day. Mounted by the gateway at ``/api/plugins/deltos/``
and reached from the desktop pane with ``ctx.rest``. Nothing here writes to the store
and nothing here calls a model or the network.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter()

# Kept in step with plugin.yaml and desktop/plugin.js (a test checks all three):
# the pane compares it with its own to spot a backend still running old code.
PLUGIN_VERSION = "0.2.0"

PLUGIN = Path(__file__).resolve().parent.parent
MAX_DAYS = 3650
MAX_ROWS = 500


def _where():
    spec = importlib.util.spec_from_file_location("_deltos_plugin_where", PLUGIN / "where.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def view():
    """The store's view, read fresh: the same library and the same store folder the
    memory provider uses (where.py)."""
    where = _where()
    missing = where.available()
    if missing:
        raise RuntimeError(missing)
    if where.src() not in sys.path:
        sys.path.insert(0, where.src())
    from deltos import Memory
    from deltos.view import View

    return View.of(Memory(where.root()))


def _int(raw, default: int, top: int) -> int:
    try:
        return max(1, min(int(raw), top))
    except (TypeError, ValueError):
        return default


def health_payload() -> dict:
    try:
        v = view()
        return {"ok": True, "version": PLUGIN_VERSION, "root": str(v.root), "today": v.today,
                "things": len(v.things())}
    except Exception as exc:   # the pane shows the reason instead of an empty list
        return {"ok": False, "version": PLUGIN_VERSION, "reason": str(exc)}


def overview_payload(days=7) -> dict:
    return view().overview(_int(days, 7, MAX_DAYS))


def things_payload(q: str = "") -> dict:
    return {"things": view().things(str(q or "")[:200])}


def thing_payload(name: str) -> dict:
    v = view()
    t = v.thing(str(name or "")[:200])
    return {"found": True, "thing": t} if t else {"found": False, "near": v.near(str(name or "")[:200])}


def on_payload(name: str, day: str) -> dict:
    v = view()
    t = v.on(str(name or "")[:200], str(day or "")[:10])
    return {"found": True, "thing": t} if t else {"found": False, "near": v.near(str(name or "")[:200])}


def changes_payload(days=7) -> dict:
    days = _int(days, 7, MAX_DAYS)
    return {"days": days, "changes": view().changes(days)[:MAX_ROWS]}


def due_payload(n=20) -> dict:
    return {"due": view().due(_int(n, 20, MAX_ROWS))}


def _http(fn, *args):
    try:
        return fn(*args)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/health")
async def health():
    return health_payload()


@router.get("/overview")
async def overview(days: int = 7):
    return _http(overview_payload, days)


@router.get("/things")
async def things(q: str = ""):
    return _http(things_payload, q)


@router.get("/thing")
async def thing(name: str):
    return _http(thing_payload, name)


@router.get("/on")
async def on(name: str, day: str):
    return _http(on_payload, name, day)


@router.get("/changes")
async def changes(days: int = 7):
    return _http(changes_payload, days)


@router.get("/due")
async def due(n: int = 20):
    return _http(due_payload, n)
