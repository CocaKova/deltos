"""Synthetic world generator.

Builds a fictional team ("Kestrel Works") whose facts change over nine months,
then writes the memory notes an agent would plausibly have left behind, plus
questions whose answers are computed from the timeline, not from the notes.

Every fact carries two clocks:
  valid_from   - when the value became true in the world
  recorded_at  - when memory found out (valid_from + a lag)
and a volatility tag (static / low / high). Corrections carry a recorded
value that was never true.

The note writer is deliberately messy, the way real agents are: some updates
edit the entity's profile in place, some append a short dated note and leave
the old profile line standing, a few append and mark the old line superseded.
Initial values live in long, keyword-dense profile write-ups; later changes
are often one-liners using an alias. That's the trap.
"""
from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from pathlib import Path

START = date(2026, 1, 5)
NOW = date(2026, 10, 1)
RETIRED = "RETIRED"

# --- the fictional world -------------------------------------------------------
# Names are invented so the answering model can't lean on prior knowledge.

MACHINES = [
    ("Tamsin", ["TS-2", "the big GPU box"]),
    ("Orrel", ["OR-1", "the render node"]),
    ("Quillon", ["QN-4", "the build server"]),
    ("Vesper", ["VS-9", "the backup NAS"]),
    ("Halloran", ["HL-3", "the lab router"]),
    ("Brisk", ["BK-5", "the edge box"]),
    ("Morrow", ["MW-6", "the old workstation"]),
    ("Calder", ["CD-8", "the inference rig"]),
]
SERVICES = [
    ("Lanternd", ["the lantern service", "LTD"]),
    ("Fernqueue", ["the job queue", "FQ"]),
    ("Saltmark", ["the metrics stack", "SMK"]),
    ("Pellucid", ["the docs site", "PLC"]),
    ("Gristle", ["the ingest worker", "GRS"]),
]
PROJECTS = [
    ("Project Ashgrove", ["Ashgrove", "the sensor rewrite"]),
    ("Project Tidewell", ["Tidewell", "the billing port"]),
    ("Project Corvane", ["Corvane", "the mobile client"]),
    ("Project Nimbel", ["Nimbel", "the voice pilot"]),
]
VENDORS = [
    ("parts supplier", ["the parts vendor"]),
    ("courier", ["the shipping vendor"]),
    ("cloud backup provider", ["the offsite backup vendor"]),
]

LOCATIONS = ["Bay 3 rack 12", "the Annex closet", "Dock B shelf 4", "Fenwick repair depot",
             "Bay 1 rack 2", "the north lab bench", "Cold room cage 7", "Harbor St storage unit",
             "Bay 5 rack 9", "the loading mezzanine"]
STATUSES = ["serving traffic", "in repair", "powered off", "on loan to Ridgeway",
            "burn-in testing", "awaiting parts", "idle spare"]
HOSTS = [m[0] for m in MACHINES]
PORTS = ["7710", "7744", "8061", "8093", "9120", "9188", "6402", "6455"]
PEOPLE = ["Ilse Marrow", "Dov Kettering", "Pia Solano", "Arun Belcastro", "Wren Okafor",
          "Teodor Haast", "Mirela Quaye", "Sully Brandt", "Noor Vantongeren", "Cass Imbert"]
STAGES = ["scoping", "prototype", "private beta", "public beta", "frozen", "shipped"]
VENDOR_NAMES = ["Larchmont Supply", "Gilded Freight", "Ostrava Parts", "Brightkeep Backup",
                "Cinderline Logistics", "Pallas Vault", "Hollis & Grey"]
SERIALS = ["SN-48213-K", "SN-90377-Q", "SN-11542-T", "SN-66019-B", "SN-30871-M",
           "SN-72254-R", "SN-55190-D", "SN-24768-X"]
YEARS = ["2019", "2020", "2021", "2022", "2023"]

# attribute -> (pool, volatility, phrasing templates)
ATTRS = {
    "location": (LOCATIONS, "high"),
    "status": (STATUSES, "high"),
    "serial number": (SERIALS, "static"),
    "purchase year": (YEARS, "static"),
    "host": (HOSTS, "low"),
    "port": (PORTS, "low"),
    "owner": (PEOPLE, "low"),
    "stage": (STAGES, "high"),
    "vendor": (VENDOR_NAMES, "low"),
}
ENTITY_ATTRS = {
    "machine": ["location", "status", "serial number", "purchase year"],
    "service": ["host", "port", "owner"],
    "project": ["owner", "stage"],
    "vendor": ["vendor"],
}
# Attributes we never record, used for "should abstain" questions.
UNRECORDED = {
    "machine": ["warranty expiry date", "RAM size"],
    "service": ["uptime SLA", "language it is written in"],
    "project": ["budget", "launch city"],
    "vendor": ["account number"],
}

# ---- scale: procedurally invented entities and values --------------------------
# scale=N keeps the hand-written world and adds (N-1)x more of each kind, with
# value pools grown N-fold. Its own RNG, so scale=1 draws nothing new. Every
# invented string is checked against every other (case-insensitive) so no name
# or value is a substring of another: readers and the scorer match by substring.

_ON = ["b", "br", "d", "dr", "f", "g", "gr", "h", "k", "kr", "l", "m", "n", "p", "pr", "r",
       "s", "st", "t", "tr", "v", "z", "th", "sh", "c", "cl", "w", "j"]
_VO = ["a", "e", "i", "o", "u", "a", "e", "o", "ae", "ia", "ou"]
_CO = ["", "", "", "n", "r", "l", "s", "m", "nd", "rk", "x", "th", "st", "v"]
# Attribute words and past-markers: a name holding one would fool the labeller.
_BANNED = ("location", "status", "serial", "purchase", "year", "host", "port", "owner", "stage",
           "vendor", "retire", "decommission", "never", "supersed", "history", "correction", "the")
_CONS = "BCDFGHJKLMNPRSTVWXZ"
_RARE = "QXZJVK"
_DESC = {
    "machine": (["amber", "quiet", "tall", "narrow", "loud", "blue", "green", "grey", "black", "white",
                 "copper", "iron", "silver", "dusty", "humming", "cold", "warm", "corner", "basement",
                 "rooftop", "spare", "shared", "noisy", "slim", "squat", "twin", "fanless", "headless"],
                ["box", "rig", "node", "server", "NAS", "router", "workstation", "appliance", "tower",
                 "blade", "chassis", "mini PC"]),
    "service": (["lantern", "billing", "audit", "sync", "mail", "search", "alert", "upload", "render",
                 "ledger", "ticket", "badge", "label", "invoice", "camera", "print", "archive", "chat",
                 "login", "payroll", "pager", "thumbnail", "webhook", "feature-flag", "geocode", "token"],
                ["service", "queue", "daemon", "worker", "stack", "site", "API", "gateway", "cache",
                 "scheduler", "indexer", "relay", "proxy", "bridge"]),
    "project": (["sensor", "billing", "mobile", "voice", "kiosk", "payroll", "ledger", "camera", "badge",
                 "warehouse", "tablet", "printer", "loyalty", "signage", "scanner", "intranet", "dock",
                 "telemetry", "onboarding", "search", "checkout", "routing", "survey", "booking"],
                ["rewrite", "migration", "pilot", "overhaul", "client", "refresh", "cleanup", "revamp",
                 "rollout", "integration", "redesign", "trial"]),
    "vendor": (["glass", "cable", "battery", "toner", "filter", "fastener", "label", "solvent", "pallet",
                "uniform", "coolant", "packaging", "lumber", "fuse", "gasket", "hose", "valve", "badge"],
               ["contract", "supply line", "deal", "arrangement", "standing order"]),
}
_VENDOR_ROLES = ["supplier", "provider", "broker", "contractor", "wholesaler", "distributor"]
_COMPANY = ["Supply", "Freight", "Parts", "Logistics", "Vault", "Traders", "Wholesale", "Cargo",
            "Systems", "& Sons", "Depot"]


class _Namer:
    """Invents strings that collide with nothing already registered."""

    def __init__(self, rng: random.Random, taken: list[str]):
        self.r = rng
        self.taken = [t.lower() for t in taken]
        self.roots: list[str] = []

    def free(self, s: str, *, ignore: tuple = ()) -> bool:
        s = s.lower()
        if any(b in s for b in _BANNED if b != "the" or not s.startswith("the ")):
            return False
        return not any(s in t or t in s for t in self.taken if t not in ignore)

    def take(self, *ss: str):
        self.taken += [s.lower() for s in ss]

    def root(self) -> str:
        r = self.r
        while True:
            w = "".join(r.choice(_ON) + r.choice(_VO) for _ in range(r.choice([2, 2, 3]))) + r.choice(_CO)
            if 5 <= len(w) <= 10 and self.free(w) and not any(w in x or x in w for x in self.roots):
                self.roots.append(w)
                return w.capitalize()

    def unique(self, make) -> str:
        while True:
            s = make()
            if self.free(s):
                self.take(s)
                return s

    def desc(self, kind: str) -> str:
        adjs, nouns = _DESC[kind]
        for _ in range(60):
            s = f"the {self.r.choice(adjs)} {self.r.choice(nouns)}"
            if self.free(s):
                self.take(s)
                return s
        s = f"the {self.root().lower()} {self.r.choice(nouns)}"  # combos used up
        self.take(s)
        return s

    def code(self, first: str = "") -> str:
        r = self.r
        if first:  # machine style "TS-2", widened so codes don't nest
            return self.unique(lambda: f"{first}{r.choice(_CONS)}-{r.randint(10, 99)}")
        return self.unique(lambda: "".join(r.sample(_CONS, 2)) + r.choice(_RARE))  # service style "SMK"


def _scaled(seed: int, scale: int):
    """Entity lists and value pools for scale>1."""
    r = random.Random(seed + 4000)
    base = {"machine": MACHINES, "service": SERVICES, "project": PROJECTS, "vendor": VENDORS}
    taken = [s for items in base.values() for n, al in items for s in [n] + al]
    taken += [v for pool, _ in ATTRS.values() for v in pool]
    nm = _Namer(r, taken)
    kinds = {k: list(v) for k, v in base.items()}
    for _ in range(scale - 1):
        for _m in MACHINES:
            name = nm.unique(nm.root)
            kinds["machine"].append((name, [nm.code(name[0]), nm.desc("machine")]))
        for _s in SERVICES:
            name = nm.unique(nm.root)
            kinds["service"].append((name, [nm.desc("service"), nm.code()]))
        for _p in PROJECTS:
            root = nm.unique(nm.root)
            name = f"Project {root}"
            nm.take(name)
            kinds["project"].append((name, [root, nm.desc("project")]))
        for _v in VENDORS:
            name = nm.unique(lambda: f"{nm.root()} {r.choice(_VENDOR_ROLES)}")
            kinds["vendor"].append((name, [nm.desc("vendor"), nm.code()]))
    n = scale
    pools = {
        "location": LOCATIONS + [nm.unique(lambda: r.choice([
            lambda: f"Bay {r.randint(10, 99)} rack {r.randint(10, 99)}",
            lambda: f"the {nm.root()} closet",
            lambda: f"{nm.root()} depot shelf {r.randint(10, 99)}",
            lambda: f"Cold room cage {r.randint(100, 999)}",
            lambda: f"{nm.root()} St storage unit",
        ])()) for _ in range(len(LOCATIONS) * (n - 1))],
        "serial number": SERIALS + [nm.unique(lambda: f"SN-{r.randint(10000, 99999)}-{r.choice(_CONS)}")
                                    for _ in range(len(SERIALS) * n * 5 // 4 - len(SERIALS))],
        "port": PORTS + [nm.unique(lambda: str(r.randint(5000, 9999))) for _ in range(len(PORTS) * (n - 1))],
        "owner": PEOPLE + [nm.unique(lambda: f"{nm.root()} {nm.root()}") for _ in range(len(PEOPLE) * (n - 1))],
        "vendor": VENDOR_NAMES + [nm.unique(lambda: f"{nm.root()} {r.choice(_COMPANY)}")
                                  for _ in range(len(VENDOR_NAMES) * (n - 1))],
        "host": [m[0] for m in kinds["machine"]],
    }
    attrs = {a: (pools.get(a, pool), vol) for a, (pool, vol) in ATTRS.items()}
    return kinds, attrs


@dataclass
class Change:
    value: str
    valid_from: str          # ISO date the value became true ("" = never true)
    recorded_at: str         # ISO date memory learned it
    note: str = ""           # filename the change was written to
    write: str = ""          # profile | edit_in_place | append | append_banner | correction
    never_true: bool = False  # the recorded value was a mistake


@dataclass
class Fact:
    id: str
    entity: str
    kind_of_entity: str
    aliases: list[str]
    attr: str
    volatility: str
    kind: str                 # stable | move | replace | correct | retire
    history: list[Change] = field(default_factory=list)

    def true_history(self) -> list[Change]:
        return [c for c in self.history if not c.never_true]

    def value_on(self, d: date) -> str | None:
        """Ground truth on a world date (valid time)."""
        val = None
        for c in self.true_history():
            if date.fromisoformat(c.valid_from) <= d:
                val = c.value
        return val


def _d(x: date) -> str:
    return x.isoformat()


def _slug(s: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in s).strip("-").replace("--", "-")


STYLES = ("messy", "trail", "append_only")
WARM_TRAIL = 2  # past values kept inline under the current line; older ones go to a cold history note


class World:
    """seed fixes the timeline AND the questions; style only changes how the
    notes get written, so styles are directly comparable."""

    def __init__(self, seed: int = 7, style: str = "messy", scale: int = 1):
        assert style in STYLES, style
        assert scale >= 1, scale
        self.rng = random.Random(seed)            # timeline
        self.wrng = random.Random(seed + 1000)    # note writing
        self.qrng = random.Random(seed + 2000)    # questions
        self.seed = seed
        self.style = style
        self.scale = scale
        if scale == 1:
            self.kinds = {"machine": MACHINES, "service": SERVICES, "project": PROJECTS, "vendor": VENDORS}
            self.attrs = ATTRS
        else:
            self.kinds, self.attrs = _scaled(seed, scale)
        self.trails: dict[str, list[dict]] = {}   # fact id -> past values, newest last (trail style)
        self.facts: list[Fact] = []
        self.entities: dict[str, dict] = {}
        self.notes: dict[str, dict] = {}   # filename -> {title, date, body}

    # ---- timeline ---------------------------------------------------------------
    def build(self):
        r = self.rng
        groups = list(self.kinds.items())
        n = 0
        used_static: dict[str, set] = {}
        for kind, items in groups:
            for name, aliases in items:
                self.entities[name] = {"kind": kind, "aliases": aliases}
                for attr in ENTITY_ATTRS[kind]:
                    pool, vol = self.attrs[attr]
                    fact = Fact(id=f"f{n:03d}", entity=name, kind_of_entity=kind, aliases=aliases,
                                attr=attr, volatility=vol, kind="stable")
                    n += 1
                    pool = [p for p in pool if p != name]  # a service can't be hosted on its own name
                    if attr == "serial number":  # serials are unique per machine
                        pool = [p for p in pool if p not in used_static.setdefault(attr, set())]
                    first = r.choice(pool)
                    if attr == "serial number":
                        used_static[attr].add(first)
                    t0 = START + timedelta(days=r.randint(0, 20))
                    fact.history.append(Change(first, _d(t0), _d(t0), write="profile"))
                    if vol != "static":
                        fact.kind = r.choices(["move", "replace", "correct", "retire", "stable"],
                                              weights=[6, 1, 2, 2, 0] if vol == "high" else [0, 5, 3, 2, 1])[0]
                        if fact.kind == "move" and vol == "low":
                            fact.kind = "replace"
                        if fact.kind == "retire" and attr not in ("status", "host", "vendor", "stage"):
                            fact.kind = "replace"
                        self._evolve(fact, pool, t0)
                    self.facts.append(fact)

    def _evolve(self, fact: Fact, pool: list[str], t0: date):
        r = self.rng
        cur, t = fact.history[0].value, t0
        span = (NOW - t0).days

        def nxt(exclude):
            return r.choice([p for p in pool if p not in exclude])

        if fact.kind == "correct":
            # The profile recorded the wrong value; a later correction gives the
            # value that was true all along.
            wrong = fact.history[0]
            wrong.never_true = True
            right = nxt({wrong.value})
            fix_at = t0 + timedelta(days=r.randint(20, span // 2))
            fact.history.append(Change(right, _d(t0), _d(fix_at), write="correction"))
            cur, t = right, fix_at
            if r.random() < 0.4:  # sometimes it moves later too
                t = t + timedelta(days=r.randint(15, max(16, (NOW - t).days - 10)))
                v = nxt({cur, wrong.value})
                fact.history.append(Change(v, _d(t), _d(t + timedelta(days=r.randint(0, 6)))))
            return
        if fact.kind == "stable":
            return
        steps = {"move": r.randint(2, 4), "replace": r.randint(1, 2), "retire": r.randint(0, 1)}[fact.kind]
        seen = {cur}
        for i in range(steps):
            room = (NOW - t).days - 10
            if room < 12:
                break
            t = t + timedelta(days=r.randint(10, max(11, room // max(1, steps - i))))
            cur = nxt(seen)
            seen.add(cur)
            lag = r.choice([0, 0, 1, 2, 3, 6, 9])
            fact.history.append(Change(cur, _d(t), _d(t + timedelta(days=lag))))
        if fact.kind == "retire":
            room = (NOW - t).days - 5
            t = t + timedelta(days=r.randint(5, max(6, room)))
            fact.history.append(Change(RETIRED, _d(t), _d(t + timedelta(days=r.randint(0, 4)))))

    # ---- notes (what the agent wrote) ---------------------------------------------
    def write_notes(self):
        r = self.wrng
        # One long profile write-up per entity holding every initial value.
        for name, ent in self.entities.items():
            facts = [f for f in self.facts if f.entity == name]
            created = min(date.fromisoformat(f.history[0].recorded_at) for f in facts)
            fn = f"{_slug(name)}.md"
            lines = {f.id: self._profile_line(f, f.history[0]) for f in facts}
            for f in facts:
                f.history[0].note = fn
            self.notes[fn] = {"title": f"{name} profile", "date": _d(created), "updated": _d(created),
                              "kind": "profile", "entity": name, "lines": lines,
                              "prose": self._prose(name, ent)}

        # Apply later changes in recorded order with a messy write policy.
        events = [(c.recorded_at, f, c) for f in self.facts for c in f.history[1:]]
        events.sort(key=lambda e: e[0])
        for rec, f, c in events:
            prof = self.notes[f"{_slug(f.entity)}.md"]
            alias = r.choice([f.entity] + f.aliases)
            if c.write == "correction":
                policy = r.choice(["edit_in_place", "append", "append"])
            else:
                policy = r.choices(["edit_in_place", "append", "append_banner"], weights=[35, 50, 15])[0]
            # The policy draw above always happens so every style sees the same
            # random stream; the style then decides what actually gets written.
            if self.style == "trail":
                policy = "edit_with_trail"
            elif self.style == "append_only":
                policy = "append"
            c.write = policy if c.write != "correction" else f"correction_{policy}"
            if policy == "edit_with_trail":
                prev = self._current(f, c)
                self.trails.setdefault(f.id, []).append(
                    {"value": prev.value, "from": prev.valid_from, "to": c.valid_from, "wrong": prev.never_true})
                prof["lines"][f.id] = self._profile_line(f, c) + self._trail_lines(f)
                prof["updated"] = rec
                c.note = f"{_slug(f.entity)}.md"
                continue
            if policy == "edit_in_place":
                prof["lines"][f.id] = self._profile_line(f, c)
                prof["updated"] = rec
                c.note = f"{_slug(f.entity)}.md"
                continue
            fn = f"{rec}-{_slug(alias)}-{_slug(f.attr)}.md"
            i = 2
            while fn in self.notes:
                fn = f"{rec}-{_slug(alias)}-{_slug(f.attr)}-{i}.md"
                i += 1
            self.notes[fn] = {"title": self._update_title(f, c, alias), "date": rec, "updated": rec,
                              "kind": "update", "entity": f.entity,
                              "body": self._update_body(f, c, alias)}
            c.note = fn
            if policy == "append_banner":
                prof["lines"][f.id] += f" _(superseded {rec}, see {fn[:-3]})_"
                prof["updated"] = rec

        if self.style == "trail":
            self._write_cold_history()

        # Daily log chatter that mentions values as they stood at the time.
        # At scale both grow: scale log notes per log day, longer ones too.
        per_day, per_log = (1, 3) if self.scale == 1 else (self.scale, min(12, 3 + self.scale // 4))
        d = START + timedelta(days=7)
        while d < NOW:
            if r.random() < 0.55:
                for j in range(per_day):
                    picks = r.sample(self.facts, per_log)
                    bits = []
                    for f in picks:
                        known = self._known_on(f, d)
                        if known and known != RETIRED:
                            bits.append(self._chatter(f, known, r.choice([f.entity] + f.aliases)))
                    if bits:
                        fn = f"log-{_d(d)}.md" if j == 0 else f"log-{_d(d)}-{j + 1}.md"
                        self.notes[fn] = {"title": f"Log {_d(d)}" + (f" ({j + 1})" if j else ""), "date": _d(d),
                                          "updated": _d(d), "kind": "log",
                                          "body": "\n".join(f"- {b}" for b in bits)}
            d += timedelta(days=r.randint(2, 6))

    def _current(self, f: Fact, c: Change) -> Change:
        """The change that was current in memory just before c landed."""
        i = f.history.index(c)
        return f.history[i - 1]

    def _trail_lines(self, f: Fact) -> str:
        out = []
        for t in reversed(self.trails[f.id][-WARM_TRAIL:]):
            if t["wrong"]:
                out.append(f"\n  - was recorded as {t['value']} by mistake (corrected; never true)")
            else:
                v = "retired" if t["value"] == RETIRED else t["value"]
                out.append(f"\n  - was: {v} ({t['from']} to {t['to']})")
        older = len(self.trails[f.id]) - WARM_TRAIL
        if older > 0:
            out.append(f"\n  - {older} older value(s) in [[{_slug(f.entity)}-history]]")
        return "".join(out)

    def _write_cold_history(self):
        """Cold tier: values that fell off the warm trail, one note per entity."""
        for name in self.entities:
            rows = []
            for f in self.facts:
                if f.entity != name or len(self.trails.get(f.id, [])) <= WARM_TRAIL:
                    continue
                for t in self.trails[f.id][:-WARM_TRAIL]:
                    tag = " (recorded by mistake, never true)" if t["wrong"] else ""
                    rows.append(f"- {f.attr}: {t['value']} ({t['from']} to {t['to']}){tag}")
            if rows:
                last = max(t["to"] for f in self.facts if f.entity == name for t in self.trails.get(f.id, []))
                self.notes[f"{_slug(name)}-history.md"] = {
                    "title": f"{name} history (archived values)", "date": last, "updated": last,
                    "kind": "archive", "entity": name,
                    "body": f"Past values for {name} that are no longer current. Not current facts.\n\n"
                            + "\n".join(rows)}

    def _known_on(self, f: Fact, d: date) -> str | None:
        """What memory believed on day d (recorded time), mistakes included."""
        val = None
        for c in f.history:
            if date.fromisoformat(c.recorded_at) <= d:
                val = c.value
        return val

    def _profile_line(self, f: Fact, c: Change) -> str:
        if c.value == RETIRED:
            return f"- **{f.attr.capitalize()}:** retired / decommissioned since {c.valid_from}"
        return f"- **{f.attr.capitalize()}:** {c.value} (since {c.valid_from})"

    def _prose(self, name: str, ent: dict) -> str:
        r = self.wrng
        a = ", ".join(ent["aliases"])
        kind = ent["kind"]
        filler = [
            f"{name} (also called {a}) has been part of the Kestrel Works setup since the January rebuild.",
            f"Most of the early notes about {name} came out of the inventory pass, which was thorough: "
            f"every {kind} was photographed, labelled and logged with its full details below.",
            f"When anyone asks about {name}, this is the page to start from. It was written carefully "
            f"and checked twice during the inventory, so the details below were accurate at the time.",
            f"Operational notes: {name} has had the usual small hiccups but nothing structural. "
            f"Keep the {kind} documentation here in one place.",
        ]
        r.shuffle(filler)
        return " ".join(filler)

    def _update_title(self, f: Fact, c: Change, alias: str) -> str:
        if c.value == RETIRED:
            return f"{alias} {f.attr} retired"
        if c.write.startswith("correction"):
            return f"Correction: {alias} {f.attr}"
        return f"{alias} {f.attr} update"

    def _update_body(self, f: Fact, c: Change, alias: str) -> str:
        if c.value == RETIRED:
            return f"{alias}'s {f.attr} is retired / decommissioned as of {c.valid_from}."
        if c.write.startswith("correction"):
            wrong = f.history[0].value
            return (f"Correction: the inventory had {alias}'s {f.attr} wrong. It was never {wrong}; "
                    f"it has been {c.value} all along (since {c.valid_from}).")
        return self.wrng.choice([
            f"{alias} {f.attr} is now {c.value} (since {c.valid_from}).",
            f"Changed {alias}'s {f.attr} to {c.value} on {c.valid_from}.",
            f"Heads up: {f.attr} for {alias} became {c.value} effective {c.valid_from}.",
        ])

    def _chatter(self, f: Fact, val: str, alias: str) -> str:
        return self.wrng.choice([
            f"Checked on {alias} ({f.attr}: {val}), nothing new.",
            f"Spent an hour with {alias}; {f.attr} still {val} at that point.",
            f"Quick note: {alias} — {f.attr} {val}.",
        ])

    # ---- questions ----------------------------------------------------------------
    def questions(self, cap: int = 400) -> list[dict]:
        r = self.qrng
        qs = []

        def ask(f, text, qtype, gold, distractors, **extra):
            qs.append({"id": f"q{len(qs):03d}", "fact": f.id, "type": qtype, "question": text,
                       "gold": gold, "distractors": [d for d in distractors if d not in gold],
                       "volatility": f.volatility, "fact_kind": f.kind, **extra})

        for f in self.facts:
            alias = r.choice([f.entity] + f.aliases)
            vals = [c.value for c in f.history]
            cur = f.value_on(NOW)
            gold = ["retired", "decommissioned"] if cur == RETIRED else [cur]
            older = [v for v in vals if v != cur and v != RETIRED]
            ask(f, f"What is the current {f.attr} of {alias}?", "current", gold, older)

            true = f.true_history()
            if len(true) >= 2 and true[-1].value != RETIRED:
                prev, last = true[-2], true[-1]
                ask(f, f"What was the {f.attr} of {r.choice([f.entity] + f.aliases)} right before it "
                       f"became {last.value}?", "history", [prev.value],
                    [v for v in vals if v != prev.value])
                ask(f, f"On what date did the {f.attr} of {r.choice([f.entity] + f.aliases)} change "
                       f"to {last.value}? Answer YYYY-MM-DD.", "when", [last.valid_from],
                    [last.recorded_at] if last.recorded_at != last.valid_from else [])
            if len(true) >= 2:
                a, b = date.fromisoformat(true[0].valid_from), date.fromisoformat(true[1].valid_from)
                if (b - a).days > 4:
                    mid = a + timedelta(days=r.randint(1, (b - a).days - 1))
                    v = f.value_on(mid)
                    ask(f, f"What was the {f.attr} of {r.choice([f.entity] + f.aliases)} on {_d(mid)}?",
                        "as_of", [v], [x for x in vals if x != v], as_of=_d(mid))

        for name, ent in self.entities.items():
            for attr in UNRECORDED[ent["kind"]]:
                if r.random() < 0.5:
                    alias = r.choice([name] + ent["aliases"])
                    qs.append({"id": f"q{len(qs):03d}", "fact": None, "type": "unknown",
                               "question": f"What is the {attr} of {alias}?", "gold": [], "distractors": [],
                               "volatility": "n/a", "fact_kind": "unrecorded"})
        if self.scale > 1 and len(qs) > cap:
            qs = self._sample(qs, cap)
        return qs

    def _sample(self, qs: list[dict], cap: int) -> list[dict]:
        """A fixed-size subset, uniform within each type, type mix kept (largest remainder)."""
        r = self.qrng
        by = {}
        for i, q in enumerate(qs):
            by.setdefault(q["type"], []).append(i)
        share = {t: cap * len(ix) / len(qs) for t, ix in by.items()}
        quota = {t: int(x) for t, x in share.items()}
        for t in sorted(share, key=lambda t: quota[t] - share[t])[:cap - sum(quota.values())]:
            quota[t] += 1
        keep = sorted(i for t, ix in by.items() for i in r.sample(ix, quota[t]))
        out = [qs[i] for i in keep]
        for n, q in enumerate(out):
            q["id"] = f"q{n:03d}"
        return out

    # ---- output ---------------------------------------------------------------------
    def render_note(self, fn: str) -> str:
        n = self.notes[fn]
        fm = f'---\ntitle: "{n["title"]}"\ndate: "{n["date"]}"\nupdated: "{n["updated"]}"\n---\n\n'
        if n["kind"] == "profile":
            body = f"# {n['title']}\n\n{n['prose']}\n\n## Details\n\n" + "\n".join(n["lines"].values()) + "\n"
        else:
            body = f"# {n['title']}\n\n{n['body']}\n"
        return fm + body

    def save(self, out: Path):
        notes_dir = out / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
        for fn in self.notes:
            (notes_dir / fn).write_text(self.render_note(fn))
        index = ["# MEMORY", "", "Index of memory notes. One line per note.", ""]
        for fn in sorted(self.notes):
            n = self.notes[fn]
            index.append(f"- [{n['title']}](notes/{fn}) — {n['updated']}")
        (out / "MEMORY.md").write_text("\n".join(index) + "\n")
        (out / "world.json").write_text(json.dumps(
            {"seed": self.seed, "now": _d(NOW), "facts": [asdict(f) for f in self.facts]}, indent=1))
        qs = paraphrase(label_questions(out, self.questions()), self.seed)
        with open(out / "questions.jsonl", "w") as fh:
            for q in qs:
                fh.write(json.dumps(q) + "\n")
        return qs


# ---- paraphrases: the same questions without the attribute word ---------------
# "current location of X" names the attribute verbatim, which flatters keyword
# search. These say it the way a person would. Fixed templates, so runs are
# reproducible and the wording never carries the answer.

PARA = {
    "current": {
        "location": ["Where is {x} these days?", "Where's {x} sitting right now?", "Where would I find {x} today?"],
        "status": ["What's going on with {x} at the moment?", "What state is {x} in right now?"],
        "serial number": ["What's the S/N on {x}?", "What ID is stamped on {x}?"],
        "purchase year": ["What year did we buy {x}?", "When was {x} bought?"],
        "host": ["Which machine is {x} running on now?", "What box does {x} live on these days?"],
        "port": ["What does {x} listen on now?", "Which number do I connect to for {x}?"],
        "owner": ["Who's in charge of {x} now?", "Who runs {x} these days?"],
        "stage": ["How far along is {x} right now?", "Where does {x} stand at the moment?"],
        "vendor": ["Which company do we use as our {x} now?", "Who's our {x} these days?"],
    },
    "history": {
        "location": ["Before {x} ended up at {v}, where was it?"],
        "status": ["What was {x} doing just before it went to {v}?"],
        "host": ["Which machine did {x} run on before it moved to {v}?"],
        "port": ["What did {x} listen on before {v}?"],
        "owner": ["Who had {x} before {v} took over?"],
        "stage": ["Before {x} reached {v}, how far along was it?"],
        "vendor": ["Who was our {x} before we switched to {v}?"],
    },
    "as_of": {
        "location": ["Where was {x} on {d}?"],
        "status": ["What was going on with {x} on {d}?"],
        "host": ["Which machine was {x} running on on {d}?"],
        "port": ["What did {x} listen on on {d}?"],
        "owner": ["Who was running {x} on {d}?"],
        "stage": ["How far along was {x} on {d}?"],
        "vendor": ["Who was our {x} on {d}?"],
    },
    "when": {
        "location": ["When did {x} move to {v}? Answer YYYY-MM-DD."],
        "status": ["When did {x} go to {v}? Answer YYYY-MM-DD."],
        "host": ["When did {x} move onto {v}? Answer YYYY-MM-DD."],
        "port": ["When did {x} switch to {v}? Answer YYYY-MM-DD."],
        "owner": ["When did {v} take over {x}? Answer YYYY-MM-DD."],
        "stage": ["When did {x} hit {v}? Answer YYYY-MM-DD."],
        "vendor": ["When did we switch our {x} to {v}? Answer YYYY-MM-DD."],
    },
    "unknown": {
        "warranty expiry date": ["When does the warranty on {x} run out?"],
        "RAM size": ["How much memory does {x} have?"],
        "uptime SLA": ["What uptime do we promise for {x}?"],
        "language it is written in": ["What's {x} coded in?"],
        "budget": ["How much money does {x} have to spend?"],
        "launch city": ["Where is {x} launching?"],
        "account number": ["What's our account ID with the {x}?"],
    },
}
_PARSE = [
    ("current", re.compile(r"^What is the current (?P<a>.+?) of (?P<x>.+)\?$")),
    ("history", re.compile(r"^What was the (?P<a>.+?) of (?P<x>.+?) right before it became (?P<v>.+)\?$")),
    ("when", re.compile(r"^On what date did the (?P<a>.+?) of (?P<x>.+?) change to (?P<v>.+)\? Answer")),
    ("as_of", re.compile(r"^What was the (?P<a>.+?) of (?P<x>.+?) on (?P<d>\d{4}-\d\d-\d\d)\?$")),
    ("unknown", re.compile(r"^What is the (?P<a>.+?) of (?P<x>.+)\?$")),
]


def paraphrase(qs: list[dict], seed: int) -> list[dict]:
    r = random.Random(seed + 3000)
    for q in qs:
        for kind, rx in _PARSE:
            m = rx.match(q["question"])
            if m and kind == q["type"]:
                g = m.groupdict()
                x = g["x"]
                if q["type"] in ("current", "history", "as_of", "when", "unknown") and g["a"] == "vendor":
                    x = x.removeprefix("the ")  # "our the courier" -> "our courier"
                if q["type"] == "unknown" and g["a"] == "account number":
                    x = x.removeprefix("the ")
                tmpl = r.choice(PARA[kind][g["a"]])
                q["paraphrase"] = tmpl.format(x=x, v=g.get("v", ""), d=g.get("d", ""))
                break
        else:
            raise ValueError(f"no paraphrase for {q['question']!r}")
    return qs


# ---- labels, computed from the note files so they work on any world dir ---------

MARKED_PAST = ("was:", "was recorded as", "superseded", "never", "older value")


def _note_kind(fn: str) -> str:
    if fn.startswith("log-"):
        return "log"
    if fn.endswith("-history.md"):
        return "archive"
    if fn[:4].isdigit():
        return "update"
    return "profile"


def label_questions(world_dir: Path, qs: list[dict]) -> list[dict]:
    """Add to each question:
      evidence    kept | faint | erased  (does the gold value survive in memory, and where)
      stale_trap  (current questions) an old value still reads as current somewhere
    """
    w = json.loads((world_dir / "world.json").read_text())
    facts = {f["id"]: f for f in w["facts"]}
    notes = {p.name: p.read_text() for p in (world_dir / "notes").glob("*.md")}
    low = {fn: t.lower() for fn, t in notes.items()}  # lowered once: big worlds have 1000s of notes
    cache: dict[str, list] = {}

    def owned(fn, text, f):
        k = _note_kind(fn)
        names = [f["entity"]] + f["aliases"]
        if k in ("profile", "archive"):
            return fn in (f"{_slug(f['entity'])}.md", f"{_slug(f['entity'])}-history.md")
        return any(n.lower() in low[fn] for n in names)

    def blocks(f):
        if f["id"] not in cache:
            cache[f["id"]] = _attr_blocks(notes, f, owned)
        return cache[f["id"]]

    for q in qs:
        if q["type"] == "unknown" or not q.get("fact"):
            q["evidence"] = "n/a"
            continue
        f = facts[q["fact"]]
        found = set()
        for k, ln in blocks(f):
            if any(g.lower() in ln.lower() for g in q["gold"]):
                found.add(k)
        q["evidence"] = "kept" if found - {"log"} else "faint" if found else "erased"
        if q["type"] == "current":
            trap = False
            for k, ln in blocks(f):
                if k in ("log", "archive") or any(m in ln.lower() for m in MARKED_PAST):
                    continue
                if any(d.lower() in ln.lower() for d in q["distractors"]):
                    trap = True
            q["stale_trap"] = trap
    return qs


def _attr_blocks(notes: dict, f: dict, owned) -> list[tuple[str, str]]:
    """Lines about one fact. In a profile the fact's line plus its indented
    trail lines; in other notes any line naming the attribute."""
    out = []
    names = [f["entity"]] + f["aliases"]
    label = f"**{f['attr'].capitalize()}:**"
    for fn, text in notes.items():
        if not owned(fn, text, f):
            continue
        k = _note_kind(fn)
        lines = text.splitlines()
        for i, ln in enumerate(lines):
            if k == "profile":
                if label in ln:
                    out.append((k, ln))
                    j = i + 1
                    while j < len(lines) and lines[j].startswith("  - "):
                        out.append((k, lines[j]))
                        j += 1
            elif f["attr"] in ln.lower():
                if k == "log" and not any(n.lower() in ln.lower() for n in names):
                    continue
                if k == "archive" and not ln.lower().startswith(f"- {f['attr']}:"):
                    continue
                out.append((k, ln))
    return out


def generate(out: Path, seed: int = 7, style: str = "messy", scale: int = 1) -> list[dict]:
    w = World(seed, style, scale)
    w.build()
    w.write_notes()
    return w.save(out)


if __name__ == "__main__":
    import sys
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "out/world-7")
    qs = generate(out, int(sys.argv[2]) if len(sys.argv) > 2 else 7,
                  sys.argv[3] if len(sys.argv) > 3 else "messy", int(sys.argv[4]) if len(sys.argv) > 4 else 1)
    print(f"{len(qs)} questions -> {out}")
