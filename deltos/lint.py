"""deltos lint: junk at the level of whole things, one table, brain-free.

The write rules (store.name_problem, value_problem, attribute_problem) only gate
new writes: what older imports made stays. On a live store (10-19) 495 of 1,908
things carried a junk signal while doctor, which checked aliases only, printed
'Names that break the name rules: 0'.

  python3 -m deltos.lint ROOT            # counts and 10 examples per signal
  python3 -m deltos.lint ROOT --fix      # also the proposed cleanup, a dry run: nothing changes
  python3 -m deltos.lint ROOT --apply    # a person's command: perform the proposal

The proposal forgets empty, kind-only, name-rule and flag-like things (Store.forget:
the files go to .trash and `deltos restore <name>` brings one back) and drops Kind
entries that repeat the thing's kind. Value-kind and plain-word things may be real
(a service with one fact, a project named by a plain word): they are listed for a
person to review, never proposed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from .doctor import things_of
from .recall import WORDS
from .store import Store, _key, name_problem

LABEL_USES = 20     # other profiles using a label for an entry before a kind of that name is a value
SMALL = 2           # entries at most for value-kind and plain-word
EXAMPLES = 10
SIGNALS = {
    "empty": "no entries",
    "kind-only": "its only entry is Kind",
    "value-kind": f"its kind is a label {LABEL_USES}+ other profiles use for an entry, and it has at most {SMALL} entries",
    "name-rule": "its name breaks a name rule",
    "flag-like": "named like a command flag",
    "plain-word": f"named by one dictionary word, with at most {SMALL} entries",
    "kind-dup": "a Kind entry that repeats the thing's kind",
}
FORGET = ("empty", "kind-only", "name-rule", "flag-like")   # proposed: the thing is forgotten
REVIEW = ("value-kind", "plain-word")                        # listed for a person, never proposed


def label_use(things) -> Counter:
    """How many profiles use each entry label (one pass): the store's own word for
    what a value is ('setting', 'version', 'host'), learned, not listed."""
    use = Counter()
    for t in things:
        use.update({_key(a.name) for a in t.attrs.values()})
    return use


def signals(t, label_use) -> list[str]:
    keys = [_key(a.name) for a in t.attrs.values()]
    kind, name, out = _key(t.kind), t.name.strip(), []
    if not keys:
        out.append("empty")
    elif keys == ["kind"]:
        out.append("kind-only")
    # A kind that is a value's label here ('bolt port', kind 'setting'): an entry made into a thing.
    if kind and len(keys) <= SMALL and label_use.get(kind, 0) - (kind in keys) >= LABEL_USES:
        out.append("value-kind")
    # A flag is one cause: name_problem refuses it too, but it is counted once, as flag-like.
    if name.startswith("-"):
        out.append("flag-like")
    elif name_problem(name):
        out.append("name-rule")
    if name.isalpha() and name.lower() in WORDS and len(keys) <= SMALL:
        out.append("plain-word")
    if kind and any(k == "kind" and _key(a.value) == kind for k, a in zip(keys, t.attrs.values())):
        out.append("kind-dup")
    return out


def table(things) -> dict:
    use = label_use(things)
    rows = [(t.file, t.name, s) for t in things for s in [signals(t, use)] if s]
    return {"total": len(things), "counts": Counter(x for _, _, s in rows for x in s), "rows": rows}


def lint(root: Path) -> dict:
    return table(things_of(Path(root)))


def junk(res: dict) -> int:
    """Things with a junk signal (a repeated Kind entry alone is not junk)."""
    return sum(1 for _, _, s in res["rows"] if set(s) - {"kind-dup"})


def proposal(res: dict) -> dict:
    """forget: (stem, name, reason); drop: (stem, name), each a Kind entry; review: (stem, name, signals)."""
    out = {"forget": [], "drop": [], "review": []}
    for stem, name, sig in res["rows"]:
        gone = [s for s in sig if s in FORGET]
        if gone:
            out["forget"].append((stem, name, "lint: " + ", ".join(gone)))
            continue
        if "kind-dup" in sig:
            out["drop"].append((stem, name))
        if any(s in REVIEW for s in sig):
            out["review"].append((stem, name, [s for s in sig if s in REVIEW]))
    return out


def _ref(store: Store, stem: str, name: str) -> str:
    """What finds this very note: its name, else its file stem (either can be another thing's name)."""
    for ref in (name, stem):
        t = ref.strip() and store.find(ref)
        if t and t.file == stem:
            return ref
    return ""


def apply(root: Path, plan: dict) -> list[str]:
    """Perform the proposal as a person (no daily budget): one receipt line each."""
    store, out = Store(Path(root)), []
    for stem, name, reason in plan["forget"]:
        ref = _ref(store, stem, name)
        out.append(store.forget(ref, reason=reason) if ref else f"skipped {stem}: nothing finds that note")
    for stem, name in plan["drop"]:
        ref = _ref(store, stem, name)
        out.append(store.forget(ref, "kind", reason="lint: kind-dup") if ref else f"skipped {stem}: nothing finds that note")
    return out


def _entries(t) -> str:
    return f"{len(t.attrs)} entr{'y' if len(t.attrs) == 1 else 'ies'}"


def _detail(t, sig: str, use: Counter) -> str:
    if sig == "name-rule":
        return name_problem(t.name.strip())
    if sig in ("kind-only", "kind-dup"):
        return "Kind: " + next(a.value for a in t.attrs.values() if _key(a.name) == "kind")
    if sig == "value-kind":
        return f"kind {t.kind!r}, a label on {use[_key(t.kind)]} profiles; {_entries(t)}"
    return _entries(t) if sig == "plain-word" else ""


def report(root: Path, fix: bool = False) -> tuple[str, dict]:
    things = things_of(Path(root))
    res, use = table(things), label_use(things)
    by = {t.file: t for t in things}
    out = [f"deltos lint {root}: {res['total']} things, {junk(res)} with a junk signal"]
    for sig, what in SIGNALS.items():
        hits = [(stem, name) for stem, name, s in res["rows"] if sig in s]
        act = ("forget" if sig in FORGET else "for a person to review" if sig in REVIEW else "drop the entry")
        out.append(f"{sig} {len(hits)} ({what}): {act}")
        out += [f"- {name} [{stem}]" + (f": {d}" if (d := _detail(by[stem], sig, use)) else "")
                for stem, name in hits[:EXAMPLES]]
    plan = proposal(res)
    if fix:
        out += ["", f"Proposed cleanup (a dry run, nothing changed; --apply performs it). Forget "
                    f"{len(plan['forget'])} things (each goes to .trash; deltos restore <name> brings it back):"]
        out += [f"- forget {name} [{stem}] ({reason})" for stem, name, reason in plan["forget"]]
        out.append(f"Drop {len(plan['drop'])} Kind entries that repeat the thing's kind (forget of the entry, restorable):")
        out += [f"- drop {name} [{stem}] Kind: {by[stem].kind}" for stem, name in plan["drop"]]
        out.append(f"For a person to review, never proposed: {len(plan['review'])} things")
        out += [f"- {name} [{stem}]: " + "; ".join(f"{s} ({_detail(by[stem], s, use)})" for s in sig)
                for stem, name, sig in plan["review"]]
    return "\n".join(out), plan


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m deltos.lint", description="Thing-level junk in a memory folder.")
    ap.add_argument("root")
    ap.add_argument("--fix", action="store_true", help="list the proposed cleanup (a dry run)")
    ap.add_argument("--apply", action="store_true", help="perform the proposal (a person's command)")
    a = ap.parse_args(argv)
    if not (Path(a.root) / "notes").is_dir():
        print(f"no notes/ in {a.root}")
        return 2
    text, plan = report(Path(a.root), fix=a.fix or a.apply)
    print(text)
    if a.apply:
        print("", *apply(Path(a.root), plan), sep="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
