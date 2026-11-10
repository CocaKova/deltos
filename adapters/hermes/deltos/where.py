"""Where the deltos library and the store are, for both halves of the plugin: the
memory provider (__init__.py) and the pane's routes (dashboard/plugin_api.py). Each
loads this file by path, since neither is imported as a package."""
from __future__ import annotations

import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
SETTINGS = "deltos.json"   # in the Hermes home: what `hermes memory setup` saved


def src() -> str:
    """The folder holding the `deltos` package: $DELTOS_SRC, else the copy shipped
    inside the plugin (lib/, a catalog install), else the checkout this adapter sits in."""
    env = os.environ.get("DELTOS_SRC")
    if env:
        return os.path.expanduser(env)
    if (HERE / "lib" / "deltos").is_dir():
        return str(HERE / "lib")
    return str(HERE.parents[2])


def available() -> str:
    """"" when the library is there, else what is missing, in words for a person."""
    s = src()
    return "" if os.path.isdir(os.path.join(s, "deltos")) else (
        f"the deltos library is not in {s} (reinstall the plugin, or point DELTOS_SRC at a deltos checkout)")


def hermes_home(home: str | os.PathLike | None = None) -> Path:
    if home:
        return Path(home)
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def settings(home: str | os.PathLike | None = None) -> dict:
    try:
        data = json.loads((hermes_home(home) / SETTINGS).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def root(home: str | os.PathLike | None = None) -> str | None:
    """The store folder: $DELTOS_ROOT, else "root" from setup; None leaves the library's
    default (~/deltos)."""
    r = os.environ.get("DELTOS_ROOT") or str(settings(home).get("root") or "")
    return os.path.expanduser(r) if r else None
