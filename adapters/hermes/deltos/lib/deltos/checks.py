"""Checks: memory knows how each fact can be seen in the world, what is due for a
look, and how often a look found it wrong.

Why (10-26): of the 24 live questions the reader missed on 10-25 (links off), none
was the reader skipping a clear current line. 19 were memory itself: a value never
written (9), an old value still current (5), two current values (2), the answer
only in history or a page (3). Notes say what was true when they were written;
nothing in memory ever looks again. Many of those facts can be seen: a version,
a port, a config key, what is running.

One mechanism, every stage:
- write: `remember(..., check=how)` = the writer saw the value just now, this way
  (a command, a file and key, a page). Same value: confirmed today. Another: the
  new value, from today. Either way what is current after the look is confirmed
  today, even with an earlier `since`. The entry keeps how ("  - check: ..."), across value
  changes; `<root>/checks.jsonl` keeps one row per look and what it found.
- import: never writes a check. A note can't look; only a person or an agent can.
- recall: a `check:` line shows under a current line marked "not confirmed in N
  weeks" ([recall] doubt_days), so a reader with tools can look before trusting it.
- due: `due()` (`deltos checks`, the memory_checks tool): the current entries most
  worth a look, ranked by how long since they were last seen and how often the
  agent's turns touched their thing. An agent with tools works through it at a
  quiet time; deltos runs nothing itself, so it works in any harness.
- measured: `stats()` = per label, how many looks found the value had changed.
  That rate is memory's own measured error on live state.
- forget / merge / fold: the check line is part of its entry (store.py).

Off by default ([checks] on = true, or DELTOS_CHECKS=1): with it off, agents get
no check parameter and no memory_checks tool, and blocks show no check lines.
The store keeps check lines either way, and the CLI works either way.
"""
from __future__ import annotations

import datetime
import json
import re
from collections import Counter
from pathlib import Path

from .entrytypes import drifts
from .store import RETIRED, Store, dormant, parse_thing

MIN_DAYS = 21          # seen this recently: not due
ASK_DAYS = 30          # how far back the reads log counts asks



def _days(a: str, b: str) -> int:
    try:
        return (datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days
    except ValueError:
        return 0


def asks(root: Path, today: str, days: int = ASK_DAYS) -> Counter:
    """How many memory blocks named each profile stem in the last `days` days
    (<root>/reads/<YYYY-MM>.jsonl, written by the Hermes adapter; none = no asks)."""
    out, start = Counter(), (datetime.date.fromisoformat(today) - datetime.timedelta(days=days)).isoformat()
    for f in sorted((root / "reads").glob("*.jsonl")) if (root / "reads").is_dir() else []:
        for ln in f.read_text().splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if start <= str(r.get("at", ""))[:10] <= today:
                out.update(set(r.get("things") or []))
    return out


# Where a thing is: when one of these changes, what runs on it or is part of it may have
# moved too (11-03: an agent moved to a VM; the Host entries of what runs beside it kept the old box).
WHERE = {"host", "location", "runs on", "ip", "address", "machine", "server"}
ON = WHERE | {"part of"}   # a link that says one thing is where another is, or inside it
MOVE_DAYS = 30


def moves(root: Path, today: str) -> dict[str, tuple[str, str, str]]:
    """Things whose where-entry changed in the last MOVE_DAYS, and what depends on them:
    {dependent profile: (moved thing, its label, the day)}; the nearest move wins."""
    from .recall import Recall
    out: dict[str, tuple[str, str, str]] = {}
    r = None
    for p in sorted((root / "notes").glob("*.md")):
        if p.name.startswith("log-") or p.name.endswith(("-history.md", "-extra.md")):
            continue
        t = parse_thing(p.read_text())
        for k, a in t.attrs.items():
            if k in WHERE and a.trail and a.since and 0 <= _days(a.since, today) <= MOVE_DAYS:
                r = r or Recall(root)
                for lk in r.graph.inc.get(f"notes/{p.name}", []):
                    dep = lk.src
                    if lk.exact and lk.key in ON and (dep not in out or out[dep][2] < a.since):
                        out[dep] = (t.name, a.name, a.since)
    return out


def due(root: Path, today: str = "", n: int = 20, min_days: int = MIN_DAYS) -> list[dict]:
    """The current entries most worth a look now: labels that drift (or any entry with
    a known check), not seen in `min_days`, oldest-and-most-asked first. Retired
    things and values are left alone. Before them: entries a note said otherwise
    about, then the drifting entries of things that depend on a thing that moved
    (moves), not seen since the move."""
    root = Path(root)
    today = today or datetime.date.today().isoformat()
    asked = asks(root, today)
    moved = moves(root, today)
    rows = []
    for p in sorted((root / "notes").glob("*.md")):
        if p.name.startswith("log-") or p.name.endswith(("-history.md", "-extra.md")):
            continue
        t = parse_thing(p.read_text())
        status = next((a.value for k, a in t.attrs.items() if k == "status"), "")
        if status.lower() == RETIRED or dormant(status):
            continue   # no longer in use (stopped, parked, deleted): nothing to look at
        for k, a in t.attrs.items():
            seen = max(filter(None, [a.since, a.confirmed]), default="")
            mv = moved.get(f"notes/{p.name}")
            mv = mv if mv and (drifts(a.name) or k in ON) and seen < mv[2] else None
            if a.value.lower() == RETIRED or k == "kind" or not (a.check or a.other or mv or drifts(a.name)
                                                                  or t.volatile.get(k) == "high"):
                continue
            age = _days(seen, today) if seen else 0
            # A note said otherwise since the last look: due now, first (memory knows it may be wrong).
            if not a.other and not mv and (not seen or age < min_days):
                continue
            q = asked.get(p.stem, 0)
            rows.append({"thing": t.name, "stem": p.stem, "attribute": a.name, "value": a.value, "seen": seen,
                         "age": age, "asks": q, "how": a.check, "score": age * (1 + q),
                         "other": list(a.other) if a.other else None, "moved": list(mv) if mv else None})
    rows.sort(key=lambda r: (not r["other"], not r["moved"], -r["score"], r["stem"], r["attribute"]))
    return rows[:n] if n else rows


def render_due(rows: list[dict]) -> str:
    if not rows:
        return "Nothing is due for a check."
    out = ["Due for a check (oldest and most asked first). Look with your own tools, read-only; then `remember` "
           "what you saw with check = how you looked (the same value confirms it, another replaces it):"]
    for r in rows:
        when = f"last seen {r['seen']} ({r['age']} days)" + (f", asked {r['asks']}x in 30 days" if r["asks"] else "")
        if r.get("other"):
            when += f", but {r['other'][1]} said {r['other'][0]} on {r['other'][2]}"
        if r.get("moved"):
            when += f", and {r['moved'][0]} it depends on changed {r['moved'][1].lower()} on {r['moved'][2]}"
        out.append(f"- {r['thing']} · {r['attribute']}: {r['value']} · {when} · "
                   + (f"check: {r['how']}" if r["how"] else "no known way to check yet"))
    return "\n".join(out)


def looks(root: Path) -> list[dict]:
    f = Path(root) / "checks.jsonl"
    out = []
    for ln in f.read_text().splitlines() if f.exists() else []:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def stats(root: Path, today: str = "", days: int = 90) -> dict:
    """Looks in the last `days` days: totals and per label word ("version", "host"),
    with how many found the value had changed (memory was wrong) or was new."""
    today = today or datetime.date.today().isoformat()
    start = (datetime.date.fromisoformat(today) - datetime.timedelta(days=days)).isoformat()
    rows = [r for r in looks(root) if start <= r.get("day", "") <= today]
    by: dict[str, Counter] = {}
    for r in rows:
        word = re.sub(r"^setting ", "", r.get("attribute", "").lower()).rsplit(" ", 1)[-1] or "?"
        by.setdefault(word, Counter())[r.get("outcome", "?")] += 1
    tot = Counter(r.get("outcome", "?") for r in rows)
    return {"days": days, "looks": len(rows), "outcomes": dict(tot), "by_label": {k: dict(v) for k, v in by.items()}}


def render_stats(s: dict) -> str:
    o = s["outcomes"]
    if not s["looks"]:
        return f"No checks in the last {s['days']} days."
    judged = o.get("same", 0) + o.get("changed", 0)
    rate = f"{100 * o.get('changed', 0) / judged:.0f}%" if judged else "-"
    out = [f"Checks in the last {s['days']} days: {s['looks']} looks; memory was right {o.get('same', 0)}, "
           f"wrong {o.get('changed', 0)} ({rate} of known values had changed), new {o.get('new', 0)}, "
           f"held {o.get('held', 0)}"]
    for word, c in sorted(s["by_label"].items(), key=lambda kv: -sum(kv[1].values())):
        out.append(f"  {word:16} {sum(c.values()):4} looks  changed {c.get('changed', 0)}  same {c.get('same', 0)}"
                   f"  new {c.get('new', 0)}")
    return "\n".join(out)


def store_due(store: Store, n: int = 20) -> str:
    """The list as an agent's tool returns it."""
    return render_due(due(store.root, store.today, n))
