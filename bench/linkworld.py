"""A small made-up world whose answers sit in links between things.

Every fact is written the way an agent writes it (Store.remember / Store.name, one
call at a time, dated), so the store looks like one an agent left behind. The
questions need what one profile can't give alone: what points at a thing (a host's
services, an owner's machines), a role word ("my grandma"), a dependency two links
away, a linked thing that has since been retired, a page reached through the person
it is about. Names are invented, so a reader can't answer from what it already knows.

    python3 -m bench.linkworld OUT      # build the world in OUT and print the questions

Each question: {"id", "question", "gold": [...], "stale": [...], "all": bool, "where": "current" | "anywhere"}.
gold "all": every gold value must be in the block (a list question), else any one.
where "anywhere": the answer may sit in a sub-line or a page (bench.gate status past=True).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

OWNER = "Ivo"
AGENT = "Wren"
TODAY = "2026-09-30"

# (day, thing, attribute, value) in the order an agent would have written them.
# A name call is ("name", thing, [other names], kind).
SCRIPT: list[tuple] = [
    ("name", OWNER, ["Ivo Brandt"], "person"),
    ("2026-03-01", OWNER, "location", "Lisbon"),
    ("2026-03-01", OWNER, "employer", "Fennick Labs"),
    # machines
    ("name", "Halden", ["HD-1", "the rack server"], "machine"),
    ("name", "Tolva", ["TV-2"], "machine"),
    ("name", "Mirefield", ["MF-3"], "machine"),
    ("name", "Brakken", ["BK-4", "the old NAS"], "machine"),
    ("2026-03-02", "Halden", "owner", OWNER),
    ("2026-03-02", "Tolva", "owner", OWNER),
    ("2026-03-02", "Halden", "location", "the hall closet"),
    ("2026-03-02", "Tolva", "location", "the office desk"),
    ("2026-03-02", "Brakken", "location", "the garage shelf"),
    # services and where they run
    ("name", "Quarry", ["the job queue"], "service"),
    ("name", "Lumen", ["the dashboard"], "service"),
    ("name", "Saffi", ["the backup job"], "service"),
    ("name", "Ploverd", ["the metrics collector"], "service"),
    ("name", "Wrenline", ["the chat bridge"], "service"),
    ("name", "Tessel", ["the search index"], "service"),
    ("2026-03-03", "Quarry", "host", "Halden"),
    ("2026-03-03", "Quarry", "port", "7310"),
    ("2026-03-03", "Lumen", "host", "Halden"),
    ("2026-03-03", "Lumen", "port", "8640"),
    ("2026-03-04", "Saffi", "host", "Brakken"),
    ("2026-03-04", "Saffi", "schedule", "nightly at 02:00"),
    ("2026-03-04", "Ploverd", "host", "Halden"),
    ("2026-03-05", "Wrenline", "runs on", "Quarry"),
    ("2026-03-05", "Tessel", "host", "Mirefield"),
    ("2026-06-10", "Lumen", "host", "Tolva"),            # moved: Halden no longer runs it
    ("2026-08-20", "Brakken", "status", "retired"),      # Saffi's host is gone
    # an agent and the models it uses
    ("name", "Moth", ["the house agent"], "agent"),
    ("name", "Corvid-7", [], "model"),
    ("name", "Corvid-9", [], "model"),
    ("2026-04-01", "Moth", "model", "Corvid-7"),
    ("2026-04-01", "Moth", "host", "Tolva"),
    ("2026-04-02", "Wrenline", "model", "Corvid-9"),
    ("2026-09-01", "Corvid-7", "status", "retired"),
    # people
    ("name", "Mira", [], "person"),
    ("name", "Teo", [], "person"),
    ("name", "Nana Ilse", [], "person"),
    ("name", "Dasha", [], "person"),
    ("name", "Pell", [], "person"),
    ("2026-03-10", "Mira", "relationship", "Ivo's sister"),
    ("2026-03-10", "Teo", "relationship", "Mira's partner"),
    ("2026-03-12", "Nana Ilse", "relationship", "Ivo's grandmother"),
    ("2026-05-01", "Nana Ilse", "location", "Harbor View care home"),
    ("2026-03-15", "Dasha", "relationship", "Ivo's coworker"),
    ("2026-03-15", "Dasha", "employer", "Fennick Labs"),
    ("2026-03-16", "Mirefield", "owner", "Dasha"),
    ("2026-03-20", "Pell", "relationship", "Dasha's brother"),
    # known by relation first, named later (a placeholder where the store allows one)
    ("name", "Ivo's dad", [], "person"),
    ("2026-04-05", "Ivo's dad", "relationship", "Ivo's dad"),
    ("name", "Rowan", ["Ivo's dad"], "person"),
    ("2026-07-01", "Rowan", "relationship", "Ivo's dad"),
    ("2026-07-01", "Rowan", "location", "Porto"),
    # a client
    ("name", "Bramble & Co", ["Bramble"], "company"),
    ("2026-05-20", "Bramble & Co", "client of", OWNER),
    ("2026-05-20", "Bramble & Co", "contract", "site rebuild, $4,000"),
]

# Things with nothing to do with the questions: a store is never only the answer.
# Ten more services on Halden make it a hub whose list can't fit in three loose lines.
FILLER_HOSTED = ["Arbit", "Cindle", "Dorrow", "Eskel", "Fobbin", "Gantry", "Hollis", "Imber", "Jessop", "Kettle"]
FILLER: list[tuple] = []
for i, n in enumerate(FILLER_HOSTED):
    FILLER += [("name", n, [], "service"), (f"2026-0{3 + i % 6}-1{i % 9}", n, "host", "Halden"),
               (f"2026-0{3 + i % 6}-1{i % 9}", n, "port", str(9100 + i))]
FILLER += [("name", "Orla", [], "person"), ("2026-04-11", "Orla", "employer", "Fennick Labs"),
           ("name", "Fennick Labs", ["Fennick"], "company"), ("2026-03-01", "Fennick Labs", "location", "Porto"),
           ("name", "Sable", [], "app"), ("2026-04-20", "Sable", "runs on", "Lumen"),
           ("2026-04-20", "Sable", "version", "2.4.1")]

PAGES = {
    "family.md": """---
title: Family notes
updated: 2026-09-20
---

# Family notes

- Nana Ilse moved into the Harbor View care home in May. Her 90th birthday party is on 11-02.
- Mira and Teo are hosting this year's dinner.
- Rowan (dad) is back in Porto after the summer.
""",
    "ops/brakken-decommission.md": """---
title: Brakken decommission
updated: 2026-08-20
---

# Brakken decommission

The old NAS was powered off for good on 08-20. Anything still pointed at it needs a new home.
""",
    "ops/dashboards.md": """---
title: Dashboards
updated: 2026-06-10
---

# Dashboards

The dashboard moved to the office desk box in June so it stays up when the closet is rewired.
""",
}

QUESTIONS = [
    # what points at a thing
    {"id": "hub-halden", "question": "What runs on Halden?", "gold": ["Quarry", "Ploverd"], "stale": ["Lumen"],
     "all": True},
    {"id": "hub-tolva", "question": "What is hosted on Tolva?", "gold": ["Lumen", "Moth"], "stale": [], "all": True},
    {"id": "owned-dasha", "question": "What does Dasha own?", "gold": ["Mirefield"], "stale": []},
    {"id": "owned-mine", "question": "Which machines do I own?", "gold": ["Halden", "Tolva"], "stale": ["Mirefield"],
     "all": True},
    {"id": "clients-mine", "question": "Who are my clients?", "gold": ["Bramble"], "stale": []},
    # a link that moved
    {"id": "moved-lumen", "question": "Where does Lumen run now?", "gold": ["Tolva"], "stale": ["Halden"]},
    {"id": "moved-lumen-past", "question": "Where did Lumen run before?", "gold": ["Halden"], "stale": [],
     "where": "anywhere"},
    # roles: a word for what something is to someone
    {"id": "role-sister", "question": "Who is my sister?", "gold": ["Mira"], "stale": []},
    {"id": "role-grandma", "question": "How is my grandma doing?", "gold": ["Harbor View"], "stale": []},
    {"id": "role-dad", "question": "Where does my dad live?", "gold": ["Porto"], "stale": []},
    {"id": "role-partner-of", "question": "Who is Mira's partner?", "gold": ["Teo"], "stale": []},
    {"id": "role-family", "question": "Who is in my family?", "gold": ["Mira", "Nana Ilse", "Rowan"], "stale": [],
     "all": True},
    # two links away
    {"id": "path-teo", "question": "Who is Teo to me?", "gold": ["Mira's partner", "Ivo's sister"], "stale": [],
     "all": True},
    {"id": "path-pell", "question": "How do I know Pell?", "gold": ["Dasha's brother", "Ivo's coworker"], "stale": [],
     "all": True},
    {"id": "deps-halden", "question": "If Halden goes down, what stops working?",
     "gold": ["Quarry", "Ploverd", "Wrenline"], "stale": [], "all": True},
    {"id": "deps-lumen", "question": "Which apps depend on the dashboard?", "gold": ["Sable"], "stale": []},
    # a linked thing that is gone
    {"id": "gone-saffi", "question": "Is the backup job's host still up?", "gold": ["retired"], "stale": [],
     "where": "anywhere"},
    {"id": "gone-moth", "question": "Can Moth still use its model?", "gold": ["retired"], "stale": [],
     "where": "anywhere"},
    # a page reached through the thing it is about
    {"id": "page-grandma", "question": "Anything coming up for my grandmother?", "gold": ["birthday"], "stale": [],
     "where": "anywhere"},
    {"id": "page-nas", "question": "What happened to the old NAS?", "gold": ["powered off"], "stale": [],
     "where": "anywhere"},
    # plain questions that links must not crowd out
    {"id": "plain-port", "question": "What port does Quarry listen on?", "gold": ["7310"], "stale": []},
    {"id": "plain-model", "question": "Which model does Wrenline use?", "gold": ["Corvid-9"], "stale": ["Corvid-7"]},
    {"id": "plain-schedule", "question": "When does Saffi run?", "gold": ["02:00"], "stale": []},
    {"id": "plain-employer", "question": "Where does Dasha work?", "gold": ["Fennick"], "stale": []},
]


def build(root: Path) -> list[str]:
    """Write the world into root (notes through the store, pages as files). Returns
    the store's receipts for calls it refused (what this deltos can't record)."""
    from deltos.store import Store
    root = Path(root)
    refused = []
    by_day: dict[str, Store] = {}
    for step in SCRIPT + FILLER:
        if step[0] == "name":
            _, thing, names, kind = step
            s = by_day.setdefault(TODAY, Store(root, today=TODAY))
            out = s.name(thing, names, kind)
        else:
            day, thing, attr, value = step
            s = Store(root, today=day)
            out = s.remember(thing, attr, value, since=day, source=AGENT)
        if out.startswith(("not ", "nothing")):
            refused.append(f"{step}: {out}")
    for rel, text in PAGES.items():
        p = root / "pages" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return refused


def memory(root: Path):
    """The per-turn reader an agent gets for this world (owner Ivo, its pages)."""
    from deltos import Memory
    from deltos.config import Config
    return Memory(config=Config(root=Path(root), owner=OWNER, pages_write=Path(root) / "pages"), today=TODAY)


if __name__ == "__main__":
    out = Path(sys.argv[1])
    for r in build(out):
        print("refused:", r)
    for q in QUESTIONS:
        print(json.dumps(q))
