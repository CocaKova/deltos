"""Gate suites that live in their own module: every module in this package that
defines NAME, SLOW and run() is a suite of bench.gate. run() -> {qid: {"s": status,
"c": context chars}}, the statuses bench.gate ranks; {} when its frozen inputs are
missing. SLOW suites are skipped by --quick, as the import replay is."""
from __future__ import annotations

import importlib
import pkgutil
import sys


def load() -> dict:
    """{NAME: run} for every suite module here."""
    out = {}
    for m in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        mod = importlib.import_module(f"{__name__}.{m.name}")
        if all(hasattr(mod, k) for k in ("NAME", "SLOW", "run")):
            out[mod.NAME] = mod.run
    return out


def slow(run) -> bool:
    """Is this suite's run() a SLOW one?"""
    return bool(getattr(sys.modules.get(getattr(run, "__module__", "")), "SLOW", False))
