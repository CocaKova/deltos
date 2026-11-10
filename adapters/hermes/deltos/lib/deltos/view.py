"""A store as data, read-only: what a pane, a slash command or a script shows a person.

Recall builds the block an agent reads. This is the same store for a person to look
at: every thing, each entry's current value with its dates, the values it had before,
which entries were seen in the world (a check), which have gone unconfirmed, what
changed lately, and what was true on a given day.

Nothing here writes and nothing here is shown to a model, so it adds no reading rule
of its own: "not confirmed" is recall's rule (Recall.unconfirmed: a label that drifts
whose newest date is doubt_days old), the due list is checks.due, a day's values are
the store's as-of reading (store._as_of_line).
"""
from __future__ import annotations

import datetime
from pathlib import Path

from . import checks
from .doctor import things_of
from .entrytypes import drifts, entry_type
from .store import RETIRED, Attr, Thing, _key, date_problem, dormant

STATES = ("disputed", "unsure", "checked", "plain", "retired")


def _days(a: str, b: str) -> int | None:
    try:
        return (datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days
    except ValueError:
        return None


def ago(n: int) -> str:
    """An age as recall says it: days, then weeks, then months."""
    return f"{n} days" if n < 14 else f"{n // 7} weeks" if n < 60 else f"{n // 30} months"


def entry(a: Attr, kind: str, today: str, doubt_days: int = 0) -> dict:
    """One entry: its current value, how sure memory is of it, and what it was before.

    state: "disputed" a note said otherwise since someone looked; "unsure" a label that
    drifts, not confirmed in doubt_days; "checked" someone saw it and said how; "retired";
    else "plain"."""
    retired = a.value.lower() == RETIRED
    seen = max(filter(None, [a.since, a.confirmed]), default="")
    age = _days(seen, today) if seen else None
    unsure = bool(doubt_days > 0 and not retired and a.since and drifts(a.name)
                  and age is not None and age >= doubt_days)
    state = ("retired" if retired else "disputed" if a.other else "unsure" if unsure
             else "checked" if a.check else "plain")
    return {
        "label": a.name, "value": a.value, "since": a.since, "confirmed": a.confirmed, "seen": seen,
        "age": age, "source": a.source, "type": entry_type(a.name, a.value, kind), "state": state,
        "note": f"not confirmed in {ago(age)}" if unsure else "",
        "check": a.check,
        "said": {"words": a.said[0], "day": a.said[1]} if a.said else None,
        "other": {"value": a.other[0], "by": a.other[1], "day": a.other[2]} if a.other else None,
        "also": [{"value": x.value, "since": x.since} for x in a.also],
        "past": [{"value": p.value, "start": p.start, "end": p.end, "wrong": p.wrong, "by": p.by}
                 for p in a.trail],
    }


def on(a: Attr, day: str) -> dict:
    """One entry on `day`, as the store's as-of reading has it: the value whose dates
    cover the day ("current" or "past"), or none ("before" its first value, or a "gap")."""
    past = [p for p in a.trail if not p.wrong]
    start = a.since or (max(p.end for p in past) if past else "")
    also = [{"value": x.value, "since": x.since} for x in a.also if not x.since or x.since <= day]
    out = {"label": a.name, "value": "", "start": "", "end": "", "state": "", "also": also}
    if not start or start <= day:
        return {**out, "value": a.value, "start": start, "state": "current"}
    for p in past:
        if day < p.end and (not p.start or p.start <= day):
            return {**out, "value": p.value, "start": p.start, "end": p.end, "state": "past"}
    first = min([p.start for p in past if p.start] + [start] + [x.since for x in a.also if x.since])
    return {**out, "start": first, "state": "before" if day < first else "gap"}


class View:
    def __init__(self, root: str | Path, today: str = "", doubt_days: int = 0):
        self.root = Path(root)
        self.today = today or datetime.date.today().isoformat()
        self.doubt_days = doubt_days
        self._things: list[Thing] | None = None   # one read of the notes per view

    @classmethod
    def of(cls, memory) -> "View":
        """The view of a Memory's store, with its settings (today, [recall] doubt_days)."""
        return cls(memory.root, memory.today, memory.doubt_days)

    def _load(self) -> list[Thing]:
        if self._things is None:
            self._things = things_of(self.root) if (self.root / "notes").is_dir() else []
        return self._things

    def _entries(self, t: Thing) -> list[dict]:
        return [entry(a, t.kind, self.today, self.doubt_days) for a in t.attrs.values()]

    def _summary(self, t: Thing) -> dict:
        es = self._entries(t)
        status = t.attrs.get("status")
        n = lambda state: sum(e["state"] == state for e in es)
        return {"name": t.name, "slug": t.slug, "kind": t.kind, "aliases": t.aliases, "updated": t.updated,
                "entries": len(es), "past": sum(len(e["past"]) for e in es),
                "unsure": n("unsure"), "checked": n("checked"), "disputed": n("disputed"),
                "dormant": bool(status and (status.value.lower() == RETIRED or dormant(status.value))),
                "changed": max((e["since"] for e in es if e["since"]), default="")}

    def _find(self, name: str) -> Thing | None:
        """By file name, then by a name or alias, then by the one thing whose name holds the words."""
        things, key = self._load(), _key(name)
        if not key:
            return None
        for t in things:
            if t.slug == name.strip():
                return t
        for t in things:
            if key in (_key(n) for n in [t.name, *t.aliases]):
                return t
        near = [t for t in things if any(key in _key(n) for n in [t.name, *t.aliases])]
        return near[0] if len(near) == 1 else None

    # -- what a caller asks for -------------------------------------------------

    def things(self, q: str = "") -> list[dict]:
        """Every thing (or those whose name, alias or kind holds `q`), newest change first."""
        key = _key(q)
        rows = [self._summary(t) for t in self._load()
                if not key or any(key in _key(n) for n in [t.name, *t.aliases, t.kind])]
        return sorted(rows, key=lambda r: (r["changed"] or r["updated"], r["name"].lower()), reverse=True)

    def near(self, name: str, n: int = 5) -> list[str]:
        """Names to offer when `name` is not one thing."""
        return [r["name"] for r in self.things(name)[:n]]

    def thing(self, name: str) -> dict | None:
        t = self._find(name)
        return None if t is None else {**self._summary(t), "about": t.about, "items": self._entries(t)}

    def on(self, name: str, day: str) -> dict | None:
        """What memory holds as true of a thing on `day` (YYYY-MM-DD), entry by entry."""
        t = self._find(name)
        if t is None or date_problem(day, "9999-12-31"):
            return None
        return {"name": t.name, "slug": t.slug, "kind": t.kind, "day": day,
                "items": [on(a, day) for a in t.attrs.values()]}

    def changes(self, days: int = 7) -> list[dict]:
        """Entries whose current value dates from the last `days` days, newest first,
        each with the value it replaced ("was") when there was one."""
        since = (datetime.date.fromisoformat(self.today) - datetime.timedelta(days=days)).isoformat()
        rows = []
        for t in self._load():
            for a in t.attrs.values():
                if not since <= a.since <= self.today:
                    continue
                was = next((p for p in a.trail if not p.wrong), None)
                rows.append({"day": a.since, "thing": t.name, "slug": t.slug, "label": a.name, "value": a.value,
                             "was": was.value if was else "", "was_since": was.start if was else "",
                             "corrected": any(p.wrong and p.end >= since for p in a.trail)})
        return sorted(rows, key=lambda r: (r["day"], r["thing"].lower(), r["label"].lower()), reverse=True)

    def due(self, n: int = 20) -> list[dict]:
        """checks.due: the entries most worth a look now."""
        return checks.due(self.root, self.today, n) if (self.root / "notes").is_dir() else []

    def looks(self, days: int = 90) -> dict:
        """checks.stats: how often a look found memory right, wrong or new."""
        return checks.stats(self.root, self.today, days)

    def overview(self, days: int = 7) -> dict:
        rows = self.things()
        total = lambda k: sum(r[k] for r in rows)
        changed = self.changes(days)
        return {"root": str(self.root), "today": self.today, "doubt_days": self.doubt_days,
                "things": len(rows), "entries": total("entries"), "past": total("past"),
                "unsure": total("unsure"), "checked": total("checked"), "disputed": total("disputed"),
                "days": days, "changed": len(changed), "changed_things": len({r["slug"] for r in changed}),
                "due": len(self.due(0)), "looks": self.looks()}


# ---- as text, for a slash command or a terminal ------------------------------------

def _plural(n: int, one: str, many: str = "") -> str:
    return f"{n} {one if n == 1 else many or one + 's'}"


def text_overview(o: dict) -> str:
    if not o["things"]:
        return (f"Memory is empty ({o['root']}). It fills as the agent calls `remember`, "
                "or from your notes with `deltos import`.")
    out = [f"**Memory** · {_plural(o['things'], 'thing')}, {_plural(o['entries'], 'entry', 'entries')}, "
           f"{_plural(o['past'], 'past value')} kept"]
    sure = []
    if o["doubt_days"]:
        sure.append(f"{o['unsure']} not confirmed in {ago(o['doubt_days'])} or more")
    sure.append(f"{o['checked']} seen with a check")
    if o["disputed"]:
        sure.append(f"{o['disputed']} a note disagrees with")
    out.append(" · ".join(sure))
    out.append(f"Last {o['days']} days: {_plural(o['changed'], 'entry', 'entries')} changed on "
               f"{_plural(o['changed_things'], 'thing')}; {o['due']} due for a look")
    looks = o["looks"]
    seen = looks["outcomes"].get("same", 0) + looks["outcomes"].get("changed", 0)
    if seen:
        out.append(f"Looks in the last {looks['days']} days: {looks['looks']}; memory was right "
                   f"{looks['outcomes'].get('same', 0)} of {seen} times it already had a value")
    return "\n".join(out)


def text_thing(t: dict) -> str:
    head = f"**{t['name']}**" + (f" ({t['kind']})" if t["kind"] else "")
    out = [head + (f" · also called {', '.join(t['aliases'])}" if t["aliases"] else "")]
    for e in t["items"]:
        when = [f"since {e['since']}"] if e["since"] else []
        if e["confirmed"] and e["confirmed"] != e["since"]:
            when.append(f"confirmed {e['confirmed']}")
        if e["note"]:
            when.append(e["note"])
        out.append(f"- **{e['label']}:** {e['value']}" + (f" ({'; '.join(when)})" if when else ""))
        if e["other"]:
            out.append(f"  - {e['other']['by']} said {e['other']['value']} on {e['other']['day']}; not seen yet")
        for x in e["also"]:
            out.append(f"  - also: {x['value']}" + (f" (from {x['since']})" if x["since"] else ""))
        if e["check"]:
            out.append(f"  - check: {e['check']}")
        for p in e["past"]:
            span = f"{p['start']} to {p['end']}" if p["start"] else f"until {p['end']}"
            out.append(f"  - recorded as {p['value']} by mistake (corrected {p['end']})" if p["wrong"]
                       else f"  - was {p['value']} ({span})")
    return "\n".join(out)


def text_on(t: dict) -> str:
    out = [f"**{t['name']}** on {t['day']}"]
    for e in t["items"]:
        if e["state"] == "current":
            v = e["value"] + (f" (since {e['start']}, still current)" if e["start"] else " (still current)")
        elif e["state"] == "past":
            v = e["value"] + (f" ({e['start']} to {e['end']})" if e["start"] else f" (until {e['end']})")
        else:
            v = (f"nothing known yet (first value from {e['start']})" if e["state"] == "before"
                 else "nothing known (a gap between known values)")
        also = "".join(f"; also {x['value']}" for x in e["also"])
        out.append(f"- **{e['label']}:** {v}{also}")
    return "\n".join(out)


def text_changes(rows: list[dict], days: int) -> str:
    if not rows:
        return f"Nothing changed in the last {days} days."
    out, day = [f"Changed in the last {days} days:"], ""
    for r in rows:
        if r["day"] != day:
            day = r["day"]
            out.append(f"**{day}**")
        out.append(f"- {r['thing']} · {r['label']}: {r['value']}"
                   + (f" (was {r['was']})" if r["was"] else " (new)")
                   + (" (an earlier value was recorded by mistake)" if r["corrected"] else ""))
    return "\n".join(out)


def text_due(rows: list[dict]) -> str:
    if not rows:
        return "Nothing is due for a look."
    out = ["Worth a look (a note disagrees first, then oldest and most asked):"]
    for r in rows:
        when = f"last seen {r['seen']}, {ago(r['age'])} ago" if r["seen"] else "never dated"
        if r.get("other"):
            when += f"; {r['other'][1]} said {r['other'][0]} on {r['other'][2]}"
        if r.get("moved"):
            when += f"; {r['moved'][0]} changed {r['moved'][1].lower()} on {r['moved'][2]}"
        out.append(f"- {r['thing']} · {r['attribute']}: {r['value']} ({when})"
                   + (f"\n  - check: {r['how']}" if r["how"] else ""))
    return "\n".join(out)


HELP = """Look at memory (read-only):
  (nothing)             how much it holds, what it has not confirmed, what changed lately
  <thing>               one thing: each value, since when, and what it was before
  on <YYYY-MM-DD> <thing>   what was true of it on that day
  changes [days]        what changed lately (7 days)
  due [n]               what is worth a fresh look, most pressing first"""


def command(view: View, raw: str = "") -> str:
    """The answer to `/deltos <raw>` in a chat and `deltos view <raw>` in a terminal.
    A word is a command only in its own shape (`changes`, `changes 30`, `on <date> ...`),
    so a thing called "Changes log" or "On call" is still found by name."""
    words = (raw or "").split()
    head, rest = (words[0].lower(), words[1:]) if words else ("", [])
    count = int(rest[0]) if len(rest) == 1 and rest[0].isdigit() and int(rest[0]) > 0 else 0
    if not words:
        return text_overview(view.overview())
    if head in ("help", "-h", "--help") and not rest:
        return HELP
    if head == "changes" and (not rest or count):
        return text_changes(view.changes(count or 7), count or 7)
    if head in ("due", "unsure") and (not rest or count):
        return text_due(view.due(count or 20))
    if head == "on" and len(rest) >= 2 and not date_problem(rest[0], "9999-12-31"):
        name, t = " ".join(rest[1:]), view.on(" ".join(rest[1:]), rest[0])
        return text_on(t) if t else _unknown(view, name)
    name = " ".join(words)
    t = view.thing(name)
    return text_thing(t) if t else _unknown(view, name)


def _unknown(view: View, name: str) -> str:
    near = view.near(name)
    return f"Memory has no thing called {name!r}." + (f" Closest: {', '.join(near)}." if near else "")
