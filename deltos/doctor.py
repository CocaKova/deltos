"""deltos doctor: health numbers and the safe fixes, brain-free.

  deltos doctor            # report: size, biggest profiles, one fact under two labels,
                            # volatile values not re-checked, what --fix would move
  deltos doctor --fix      # move session-log settings, event statuses, and (past 60 entries)
                            # settings untouched for 30 days out of profiles into
                            # <thing>-extra.md: kept and searchable, not crowding the core

A session log written as settings ('Setting 141 assistant turns: 27B run') and an
event written as a status ('bot-dm-brake skipped (not armed)', which replaced
'PAUSED') crowd a profile and mislead. The store refuses new ones
(store.junk_setting / junk_status); --fix handles what is already there.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

from .store import PEOPLE, Past, Store, _key, junk_setting, junk_status, name_problem, numbered_sibling, parse_thing

STALE_DAYS = 30
CHURN = 0.8        # an entry kind "changes often": its label word's entries have this many older values each
CHURN_SUPPORT = 5  # entries a label word needs before its churn counts
LATER = 5          # changes on the same thing since the entry was set


def _num(v: str) -> str:
    m = re.search(r"\d[\d.,]*", v)
    return m.group(0) if m else ""


def things_of(root: Path) -> list:
    """Every profile, with its cold history (an entry's whole trail)."""
    things = []
    for p in sorted((Path(root) / "notes").glob("*.md")):
        if p.name.endswith(("-history.md", "-extra.md")) or p.name.startswith("log-"):
            continue
        h = p.with_name(p.stem + "-history.md")
        t = parse_thing(p.read_text(), h.read_text() if h.exists() else "")
        t.file = p.stem
        things.append(t)
    return things


def report(root: Path, today: str | None = None) -> str:
    today = today or date.today().isoformat()
    things = things_of(root)
    entries = sum(len(t.attrs) for t in things)
    out = [f"deltos {root}: {len(things)} things, {entries} current entries, "
           f"{sum(1 for _ in (Path(root) / 'notes').glob('log-*.md'))} daily logs"]
    out.append("Biggest: " + ", ".join(f"{t.name} {len(t.attrs)}"
                                       for t in sorted(things, key=lambda t: -len(t.attrs))[:8]))
    # The same value under two labels on one thing: a later change updates one
    # and the other stays "current" (e.g. 'Version: 48 entries' + 'patch count: 48').
    dup = []
    for t in things:
        seen: dict[str, str] = {}
        for k, a in t.attrs.items():
            v = _key(a.value)
            if len(v) < 4 or not _num(a.value):
                continue
            if v in seen and seen[v] != k:
                dup.append(f"{t.name}: '{t.attrs[seen[v]].name}' and '{a.name}' both = {a.value}")
            seen.setdefault(v, k)
    out.append(f"Same value under two labels: {len(dup)}" + "".join(f"\n- {d}" for d in dup[:15]))
    stale = []
    for t in things:
        for k, lvl in t.volatile.items():
            a = t.attrs.get(k)
            last = a and (a.confirmed or a.since)
            if lvl == "high" and last and (date.fromisoformat(today) - date.fromisoformat(last)).days > STALE_DAYS:
                stale.append(f"{t.name} {a.name}: {a.value} (last seen {last})")
    out.append(f"Volatile values unconfirmed for {STALE_DAYS}+ days: {len(stale)}"
               + "".join(f"\n- {s}" for s in stale[:15]))
    sus = maybe_outdated(things, today)
    out.append(f"Maybe outdated (set {STALE_DAYS}+ days ago, unconfirmed, a kind that changes here, "
               f"{LATER}+ later changes on the thing): {len(sus)} (deltos doctor --review)"
               + "".join(f"\n- {t.name} {a.name}: {a.value} (since {a.since}; {later} later changes)"
                         for _, t, a, later in sus[:10]))
    bad = [(t.name, a) for t in things for a in t.aliases if name_problem(a)]
    out.append(f"Names that break the name rules (--fix removes them): {len(bad)}"
               + "".join(f"\n- {n}: {a} ({name_problem(a)})" for n, a in bad[:10]))
    # Whole things: the rules above gate new writes and aliases, not what older imports made.
    from .lint import junk, table
    res = table(things)
    c = res["counts"]
    out.append(f"Things with junk signals: {junk(res)} (empty {c['empty']}, kind-only {c['kind-only']}, "
               f"value-kind {c['value-kind']}, name-rule {c['name-rule']}, flag-like {c['flag-like']}, "
               f"plain-word {c['plain-word']}): python3 -m deltos.lint {root}")
    odd = maybe_other_things(things)
    out.append(f"Names that may be another thing (name_thing remove_names to undo): {len(odd)}"
               + "".join(f"\n- {n}: {a} ({why})" for n, a, why in odd[:25]))
    same = maybe_same_people(things)
    out.append(f"Maybe one person in two profiles (merge with name_thing merge=true): {len(same)}"
               + "".join(f"\n- {a} / {b}" for a, b in same[:10]))
    return "\n".join(out)


def maybe_other_things(things) -> list[tuple[str, str, str]]:
    """Names on a profile that name something else (a judgment call: listed,
    never removed here):
    - numbered siblings: 'Node 2' on the profile that has 'Node 1'
    - the VM a service runs in: 'Proxmox VM 7' on Proxmox, 'n8n VM 11' on n8n
    - a slot that held another thing: 'agent-model@qwen-27b' on Brain, while
      'qwen38-27b' has its own profile."""
    own = {_key(t.name): t.name for t in things}
    out = []
    for t in things:
        names = [t.name, *t.aliases]
        for i, a in enumerate(t.aliases):
            sib = numbered_sibling(a, [x for x in names if x != a])
            if sib:
                out.append((t.name, a, f"differs from {sib!r} only by its number"))
            elif re.search(r"(^|\s)VM\s?\d+$", a, re.I) and not re.search(r"\b(vm|virtual machine|machine|server|host)\b", t.kind or "", re.I):
                out.append((t.name, a, f"names a VM; {t.name} is a {t.kind or 'thing'}"))
            else:
                other = own.get(_key(re.sub(r"^[\w-]+@", "", a)))
                if other and _key(other) != _key(t.name):
                    out.append((t.name, a, f"is {other!r}, which has its own profile"))
    return out


def _label_words(label: str) -> list[str]:
    return [w for w in re.findall(r"[a-z]+", label.lower()) if len(w) > 1]


def churn(things) -> dict[str, float]:
    """For each label word, the mean number of older values of the entries that
    carry it: how often this kind of entry changes in THIS memory (version, status,
    model change; package, license, kind don't). Learned, not listed."""
    n: dict[str, int] = {}
    old: dict[str, int] = {}
    for t in things:
        for a in t.attrs.values():
            if a.name.lower().startswith("setting "):
                continue
            k = sum(1 for p in a.trail if not p.wrong)
            for w in set(_label_words(a.name)):
                n[w] = n.get(w, 0) + 1
                old[w] = old.get(w, 0) + k
    return {w: old[w] / c for w, c in n.items() if c >= CHURN_SUPPORT}


def maybe_outdated(things, today: str) -> list[tuple[float, object, object, int]]:
    """Entries memory should not be sure of: set 30+ days ago, never confirmed,
    of a kind that changes often here, on a thing that changed a lot since.
    (score, thing, attr, later changes), most suspect first. On the live store
    (10-09) 30% of old unconfirmed entries were stale; release/version and
    deployment entries were, static ones (package, license, specs) weren't."""
    ch, out = churn(things), []
    for t in things:
        dates = [d for a in t.attrs.values() for d in [a.since] + [p.start for p in a.trail if not p.wrong] if d]
        for k, a in t.attrs.items():
            # Settings are config keys; a label naming one release ("Release v1.0.0")
            # records that event, which stays true.
            if k.startswith("setting") or k == "kind" or re.search(r"\d", k) or a.confirmed or not a.since:
                continue
            try:
                age = (date.fromisoformat(today) - date.fromisoformat(a.since)).days
            except ValueError:
                continue
            c = max([ch.get(w, 0) for w in _label_words(a.name)] or [0])
            later = sum(1 for d in dates if d > a.since)
            if age >= STALE_DAYS and c >= CHURN and later >= LATER:
                out.append((c * later, t, a, later))
    return sorted(out, key=lambda x: -x[0])


def review(things, today: str, pages=None, n: int = 30) -> list[dict]:
    """The review queue: the most suspect entries, each with the newest page line
    that mentions the thing and the entry's label after the entry was set: what a
    person or the agent checks before confirming (remember the same value) or
    replacing it."""
    rows = []
    for score, t, a, later in maybe_outdated(things, today)[:n]:
        ev = ""
        if pages is not None:
            for ref, d, lines in pages.top(f"{t.name} {a.name}", 5):
                if d > a.since and lines:
                    ev = f"{ref} [{d}]: {lines[-1][:200]}"
                    break
        rows.append({"thing": t.name, "attribute": a.name, "value": a.value, "since": a.since,
                     "later_changes": later, "score": round(score, 1), "newer_evidence": ev})
    return rows


def maybe_same_people(things) -> list[tuple[str, str]]:
    """People whose first names are one name and its short form ('Beth' /
    'Bethany Ann Marlowe'): a judgment call, so listed, never merged here."""
    people = [t for t in things if _key(t.kind) in PEOPLE]
    first = {t.name: {_key(n).split()[0] for n in [t.name, *t.aliases] if _key(n) and not name_problem(n)}
             for t in people}
    pairs = []
    for i, a in enumerate(people):
        for b in people[i + 1:]:
            if any(len(x) >= 3 and len(y) >= 3 and (x.startswith(y) or y.startswith(x))
                   for x in first[a.name] for y in first[b.name]):
                pairs.append((a.name, b.name))
    return pairs



ENTRY = re.compile(r"- \*\*(.+?):\*\* (.*)$")
CORE = 60            # entries a profile holds before dormant settings move out
DORMANT_DAYS = 30


def _touched(rest: str) -> str:
    """The last date an entry was set or seen true."""
    return max(re.findall(r"(?:since|confirmed) (\d{4}-\d\d-\d\d)", rest) or [""])
META = re.compile(r"\s*\((?:since|confirmed|source:)[^()]*\)\s*$")


def tidy(root: Path, apply: bool = False) -> dict[str, int]:
    moved: dict[str, int] = {}
    store = Store(root)
    with store._locked():
        for p in sorted((Path(root) / "notes").glob("*.md")):
            if p.name.endswith(("-history.md", "-extra.md")) or p.name.startswith("log-"):
                continue
            text = p.read_text()
            if "\n## Details\n" not in text:
                continue
            lines = text.splitlines(keepends=True)
            entries = [(i, m) for i, ln in enumerate(lines) for m in [ENTRY.match(ln)] if m]
            move = {i for i, m in entries if junk_setting(m.group(1), META.sub("", m.group(2)))}
            # A profile is the thing's core: past CORE entries, the settings
            # nobody has touched for DORMANT_DAYS leave first, oldest first.
            over = len(entries) - len(move) - CORE
            if over > 0:
                cut = (date.fromisoformat(store.today) - timedelta(days=DORMANT_DAYS)).isoformat()
                dormant = sorted((_touched(m.group(2)), i) for i, m in entries
                                 if i not in move and m.group(1).lower().startswith("setting ")
                                 and _touched(m.group(2)) < cut)
                move |= {i for _, i in dormant[:over]}
            keep, out, skip = [], [], False
            for i, ln in enumerate(lines):
                if ENTRY.match(ln):
                    skip = i in move
                elif not ln.startswith("  "):
                    skip = False
                (out if skip else keep).append(ln)
            if not out:
                continue
            moved[p.stem] = sum(1 for ln in out if ln.startswith("- **"))
            if apply:
                store._write(p, "".join(keep))
                store.aside(p.stem, out)
    if apply:
        moved.update(restore_status(root))
        for p in sorted((Path(root) / "notes").glob("*.md")):
            if p.name.endswith(("-history.md", "-extra.md")) or p.name.startswith("log-"):
                continue
            t = parse_thing(p.read_text())
            bad = [a for a in t.aliases if name_problem(a)]
            if bad:   # older imports let them in: versions, apk files, labels
                store.unname(p.stem, bad)
    else:
        for p in sorted((Path(root) / "notes").glob("*.md")):
            if not p.name.endswith(("-history.md", "-extra.md")) and not p.name.startswith("log-"):
                t = store.find(p.stem)
                a = t and t.attrs.get("status")
                if a and junk_status(a.value):
                    moved[p.stem] = moved.get(p.stem, 0) + 1
    return moved


def restore_status(root: Path) -> dict[str, int]:
    """An event written as a status ('bot-dm-brake skipped (not armed)')
    replaced the state ('PAUSED'). Move the events out and make the newest
    real state current again, with its own start date."""
    store, done = Store(root), {}
    with store._locked():
        for p in sorted((Path(root) / "notes").glob("*.md")):
            if p.name.endswith(("-history.md", "-extra.md")) or p.name.startswith("log-"):
                continue
            t = store.find(p.stem)
            a = t and t.attrs.get("status")
            if not a or not junk_status(a.value):
                continue
            events = [Past(a.value, a.since, "")] + [x for x in a.trail if junk_status(x.value) and not x.wrong]
            keep = [x for x in a.trail if not (junk_status(x.value) and not x.wrong)]
            state = next((x for x in keep if not x.wrong), None)
            store.aside(p.stem, [f"- status (event): {e.value} ({e.start}{' to ' + e.end if e.end else ''})" for e in events])
            if state:
                keep.remove(state)
                a.value, a.since, a.trail = state.value, state.start, keep
            else:
                del t.attrs["status"]
            store.save(t)
            done[p.stem] = len(events)
    return done




HOLDS = ("import.hold", "learn.hold")   # a held note or chat run; its first line says why


def doctor(root: Path, fix: bool = False) -> str:
    # The nightly log keeps only the first line: a hold goes there.
    held = [Path(root) / h for h in HOLDS if (Path(root) / h).exists()]
    out = "".join(f"held: {(p.read_text().splitlines() or [''])[0]}\n" for p in held) + report(root)
    moved = tidy(root, apply=fix)
    if moved:
        top = sorted(moved.items(), key=lambda x: -x[1])[:8]
        out += ("\nMoved out of profiles: " if fix else "\n--fix would move: ") + \
            f"{sum(moved.values())} entries on {len(moved)} things (" + ", ".join(f"{k} {v}" for k, v in top) + ")"
    return out
