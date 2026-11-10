"""A small made-up store to try deltos on: `python3 -m deltos.demo <folder>`.

A two-person studio's memory, written the way an agent writes it over seven months:
values that changed (the old ones kept, with their dates), values someone looked at
(a check), values nobody has confirmed in weeks, one a note disagrees with, one
recorded by mistake. Every date is counted back from today, so the store always reads
as current. Its deltos.toml turns on the "not confirmed" note and checks. Nothing in
it is real.
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

from .store import Store

THINGS = [   # name, other names, kind
    ("Orchard", ["the shop", "orchard app"], "web app"),
    ("vps-2", ["the new server"], "machine"),
    ("vps-1", ["the old server"], "machine"),
    ("Postgres", ["the main database"], "service"),
    ("Local model", ["the local LLM"], "service"),
    ("Mara", [], "person"),
    ("Newsletter", ["the weekly letter"], "project"),
]

# days ago, thing, label, value, how it was seen ("" = told, not seen), recorded by mistake
EVENTS = [
    (210, "Orchard", "status", "live", "", False),
    (210, "Orchard", "host", "vps-1", "", False),
    (210, "Orchard", "version", "3.2.4", "", False),
    (210, "Orchard", "port", "8080", "", False),
    (210, "Orchard", "owner", "Mara", "", False),
    (210, "Orchard", "price", "$9 a month per seat", "", False),
    (210, "vps-1", "status", "running", "", False),
    (210, "vps-1", "ip", "203.0.113.10", "", False),
    (200, "Postgres", "host", "vps-1", "", False),
    (200, "Postgres", "version", "15.6", "", False),
    (200, "Postgres", "port", "5432", "", False),
    (200, "Postgres", "backup schedule", "weekly, Sunday 03:00", "", False),
    (190, "Mara", "role", "designer and co-founder", "", False),
    (190, "Mara", "location", "Porto", "", False),
    (180, "Newsletter", "status", "weekly", "", False),
    (180, "Newsletter", "send day", "Thursday", "", False),
    (150, "Orchard", "version", "3.3.0", "", False),
    (120, "Local model", "model", "Llama 3.3 70B", "", False),
    (120, "Local model", "host", "workstation", "", False),
    (120, "Local model", "url", "http://workstation:8000/v1", "", False),
    (110, "Orchard", "deploy rule", "never deploy on a Friday", "", False),
    (95, "Mara", "location", "Lisbon", "", False),
    (90, "Postgres", "version", "16.4", "", False),
    (90, "Postgres", "backup schedule", "nightly, 02:00", "", False),
    (72, "vps-2", "status", "running", "", False),
    (72, "vps-2", "ip", "203.0.113.20", "", False),
    (72, "vps-2", "size", "4 vCPU, 8 GB", "", False),
    (70, "Orchard", "host", "vps-2", "", False),
    (70, "Orchard", "port", "8443", "", False),
    (70, "Postgres", "host", "vps-2", "", False),
    (65, "vps-1", "status", "retired", "", False),
    (50, "Local model", "model", "Qwen3 Coder 30B", "", False),
    (40, "Orchard", "version", "3.4.0", "", False),
    (38, "Orchard", "version", "3.4.1", "", True),
    (25, "Orchard", "price", "$12 a month per seat", "", False),
    (12, "Newsletter", "status", "paused", "", False),
    (6, "Orchard", "port", "8443", "ss -ltnp on vps-2", False),
    (5, "Orchard", "version", "3.5.0", "curl -s https://orchard.example/version", False),
    (3, "vps-2", "size", "8 vCPU, 16 GB", "", False),
    (2, "Postgres", "port", "5432", "ss -ltnp on vps-2", False),
    (1, "Newsletter", "send day", "Tuesday", "", False),
    (0, "vps-2", "status", "running", "uptime on vps-2", False),
]

# The demo's settings: what it is there to show, both off in a plain store.
SETTINGS = """\
# deltos settings for the demo store. Every key is optional; the full list is in the README.

[recall]
doubt_days = 21   # a host, port, version or status nobody confirmed in 3 weeks reads "not confirmed in N weeks"

[checks]
on = true         # the agent records how it just saw a value, and can ask what is due for a fresh look
"""

# a note that disagrees with a value nobody has looked at since: thing, label, value, days ago
DISPUTES = [("Local model", "url", "http://workstation:8001/v1", 4)]


def seed(root: str | Path, today: str = "") -> list[str]:
    """Write the store into `root` (an empty or new folder); what each write answered."""
    root = Path(root)
    now = datetime.date.fromisoformat(today) if today else datetime.date.today()
    day = lambda ago: (now - datetime.timedelta(days=ago)).isoformat()
    out = []
    store = Store(root, today=day(max(e[0] for e in EVENTS)))
    for name, aliases, kind in THINGS:
        out.append(store.name(name, aliases, kind))
    for ago, thing, label, value, how, mistake in EVENTS:
        store = Store(root, today=day(ago))
        out.append(store.remember(thing, label, value, since=day(ago), source="demo", correction=mistake, check=how))
    for thing, label, value, ago in DISPUTES:
        Store(root, today=day(ago)).dispute(thing, label, value, day(ago), by="a meeting note")
    return out


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(__doc__.strip().splitlines()[0])
        return 2
    root = Path(args[0]).expanduser()
    if (root / "notes").is_dir() and any((root / "notes").iterdir()):
        print(f"{root} already holds a store; give a new folder")
        return 1
    seed(root)
    if not (root / "deltos.toml").exists():
        (root / "deltos.toml").write_text(SETTINGS)
    print(f"A demo store is in {root}. Try:  deltos --root {root} view")
    return 0


if __name__ == "__main__":
    sys.exit(main())
