"""`hermes deltos ...`: the deltos command on this Hermes's store, e.g.
`hermes deltos view`, `hermes deltos show <thing> --history`, `hermes deltos checks`.
Hermes adds it while deltos is the active memory provider."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _where():
    spec = importlib.util.spec_from_file_location("deltos_plugin_where", Path(__file__).resolve().parent / "where.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(args) -> int:
    where = _where()
    why = where.available()
    if why:
        print(f"deltos: {why}", file=sys.stderr)
        return 1
    if where.src() not in sys.path:
        sys.path.insert(0, where.src())
    from deltos.cli import main
    rest = list(getattr(args, "deltos_args", None) or []) or ["view"]
    root = getattr(args, "root", None) or where.root()   # a folder named here, else the one setup was given
    agent = getattr(args, "agent", "") or ""
    return main((["--root", root] if root else []) + (["--agent", agent] if agent else []) + rest)


def register_cli(subparser) -> None:
    import argparse
    subparser.description = "The deltos command on this Hermes's memory (try: hermes deltos view)."
    subparser.add_argument("--root", help="another memory folder than this Hermes's")
    subparser.add_argument("--agent", default="", help="who writes (default: you, by hand)")
    subparser.add_argument("deltos_args", nargs=argparse.REMAINDER, help="what to pass to deltos (default: view)")
    subparser.set_defaults(func=run)
