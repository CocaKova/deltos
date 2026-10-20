"""Block hygiene, brain-free (10-19): an agent's real user messages, each through
Memory.context on a scratch clone of a live memory, at two versions of the read
path, and what each block spends its characters on.

  junk things     blocks with a profile memory knows nothing about (no entry, or only
                  its Kind), one whose name breaks the store's name rules, or one a
                  plain word named only at the start of a sentence ("Run the ..."),
                  and per reason: the read path still indexes the first kind (a
                  deviation from the hygiene spec awaiting the owner's sign-off)
  owner pinned    blocks with the owner's profile ("whose memory this is")
  receipts        "said:" lines under Other notes, cut off from the entry they back
  duplicates      blocks with one Other notes line twice (case and spacing aside)
  chars           median and p90 characters per block

A pre-registered criterion is PASS or NOT MET. Under the table, each NOT MET one
gets its cause, read from the store and the messages: for a --thing, whether any
of its names is a plain word (the sentence-start rule applies to nothing else) and
how the messages that pin it write it; for the owner, which path pins the owner
and how many base pins the "me" rule could have dropped.

No model, no embedder. The messages are read from Hermes' state.db (read-only); the
store is a `git clone` of the memory under out/ (no session digest is rebuilt);
pages are read from the clone's deltos.toml paths, read-only. Each version runs in
its own process under nice, one after the other (~150 MB, ~10 min each on 2,355 notes).

  python3 -m bench.hygienecheck                           # merge-base with main vs this checkout
  python3 -m bench.hygienecheck --base 73406b9 --thing Run
  python3 -m bench.hygienecheck --store out/hygiene/store-old   # another clone, as is
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import re
import resource
import sqlite3
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
OUT = Path("out/hygiene")
PROFILE = re.compile(r"^Profile entries \[([^ \]·]+)")
TAG = re.compile(r"^\[[^\]\n]*\] ")
OWNER_PIN = " · whose memory this is"
MAX_OWNER = 5   # pass: owner pinned on at most this many blocks
PLAIN_BELOW = 3   # a name is a plain word only on a profile with fewer entries (deltos/recall.py _build_index)
WRITTEN = ("mid-sentence", "at a sentence start only", "in another case only", "not at all")
KIND_ONLY, PLAIN = "empty or kind-only", "plain word at a sentence start"
JUNK_REASONS = (KIND_ONLY, "name rules", PLAIN)


def messages(state: Path, since: str) -> list[dict]:
    """Every user message since `since` (YYYY-MM-DD, local time), oldest first."""
    t0 = datetime.datetime.fromisoformat(since).timestamp()
    c = sqlite3.connect(f"file:{state}?mode=ro", uri=True)
    try:
        rows = c.execute("select id, content from messages where role = 'user' and timestamp >= ? "
                         "order by timestamp, id", (t0,)).fetchall()
    finally:
        c.close()
    return [{"id": i, "text": t if isinstance(t, str) else ""} for i, t in rows]


def blocks(src: str, store: str, msgs: str, to: str) -> None:
    """Child process: every message's block with the deltos library in `src`."""
    sys.path.insert(0, src)
    from deltos.memory import Memory
    m = Memory(root=store)
    m.refresh = lambda: None   # read-only: no session digest rebuilt in the clone
    out = []
    for x in json.loads(Path(msgs).read_text()):
        q = x["text"].strip()
        t = time.time()
        b = m.context(q) if q else ""
        out.append({"id": x["id"], "ms": round((time.time() - t) * 1000), "block": b})
    Path(to).write_text(json.dumps(out))


def other_notes(block: str) -> list[str]:
    """The Other notes lines of a block, without their [note · date] tags."""
    if "\nOther notes (" not in "\n" + block:
        return []
    sec = ("\n" + block).split("\nOther notes (", 1)[1].split("\n\n")[0].splitlines()[1:]
    return [TAG.sub("", x) for x in sec]


def mark(ok: bool) -> str:
    return "PASS" if ok else "NOT MET"


def _label(line: str) -> str:
    return line.split(":**")[0].removeprefix("- **").lower()


def junk_of(store: Path, r=None) -> tuple[dict, dict, dict]:
    """profile slug -> why it is junk ("empty or kind-only", "name rules"), slug ->
    its names, and slug -> its names that are plain words (as deltos/recall.py
    _build_index flags them): read from the store, whichever version made the block."""
    from deltos.recall import WORDS, Recall
    from deltos.store import name_problem
    r = r or Recall(store)
    facts: dict[str, int] = {}   # profile -> entries other than its Kind
    for prof, _, ln in r.facts:
        if _label(ln) != "kind":
            facts[prof] = facts.get(prof, 0) + 1
    why, names, plain = {}, {}, {}
    for g in r.names:
        prof = r.profile_of.get(g[0].lower())
        if not prof:
            continue
        slug = prof.removeprefix("notes/").removesuffix(".md")
        names.setdefault(slug, list(g))
        if not facts.get(prof):
            why[slug] = KIND_ONLY
        elif name_problem(g[0]):
            why[slug] = "name rules"
        plain[slug] = [n for n in g if re.fullmatch(r"[A-Z][a-z]+", n) and n.lower() in WORDS
                       and r.size.get(r.profile_of.get(n.lower()), 0) < PLAIN_BELOW]
    return why, names, plain


def plain_initial(q: str, names: list[str], plain: list[str]) -> bool:
    """The message names the thing, and only by a plain word starting a sentence."""
    from deltos.recall import _sentence_start
    ql, any_hit = q.lower(), False
    edge = lambda i: i < 0 or i >= len(ql) or not (ql[i].isascii() and ql[i].isalnum())
    for n in names:
        for m in re.finditer(re.escape(n.lower()), ql):
            i, j = m.start(), m.end()
            if not (edge(i - 1) and edge(j)):
                continue
            if n not in plain:
                return False
            if q[i:j] == n:
                any_hit = True
                if not _sentence_start(q, i):
                    return False
    return any_hit


def written(q: str, names: list[str]) -> str:
    """How a message writes a thing, the best way first (WRITTEN): one of its names
    as the store writes it, not starting a sentence; only at a sentence start; only
    in another case ("run" for Run); or not at all (pinned by another path)."""
    from deltos.recall import _sentence_start
    ql, best = q.lower(), len(WRITTEN) - 1
    edge = lambda i: i < 0 or i >= len(ql) or not (ql[i].isascii() and ql[i].isalnum())
    for n in names:
        for m in re.finditer(re.escape(n.lower()), ql):
            i, j = m.start(), m.end()
            if edge(i - 1) and edge(j):
                best = min(best, 2 if q[i:j] != n else 1 if _sentence_start(q, i) else 0)
    return WRITTEN[best]


def thing_cause(r, thing: str, names: list[str], plain: list[str], qs: list[str]) -> str:
    """Why blocks still pin `thing`: `qs` are the messages whose block pins it."""
    prof = r.profile_of.get(names[0].lower(), "")
    size = r.size.get(prof, 0)
    kinds = sum(1 for p, _, ln in r.facts if p == prof and _label(ln) == "kind")
    if plain:
        why = f"{', '.join(plain)} is a plain word, so the sentence-start rule applies to it"
    elif size >= PLAIN_BELOW:
        why = (f"its profile has {size} entries ({kinds} of them its Kind), and a name is a plain word only "
               f"on a profile with fewer than {PLAIN_BELOW}, so the sentence-start rule never applies to it")
    else:
        why = "none of its names is a capitalized dictionary word, so the sentence-start rule never applies to it"
    how = collections.Counter(written(q, names) for q in qs)
    return (f"{thing}: NOT MET. {why[0].upper() + why[1:]}. Of the {len(qs)} blocks pinning it, the message "
            f"writes it " + ", ".join(f"{k} {how[k]}" for k in WRITTEN)
            + ("" if plain else " (a name that is not a plain word is matched in any case)") + ".")


def owner_how(r, q: str) -> str:
    """Which path pins the owner on a message: "a name" (entities() finds the owner
    by a name or alias, as it would with no owner set), "i/my" (first-person words
    naming an owner entry: rule 5's path) or "another path" (the question's words
    reached the owner's profile)."""
    prof = r.profile_of.get(r.owner.lower()) if r.owner else None
    owner, seen = r.owner, r._seen
    r.owner, r._seen = "", {}   # no owner: entities() adds no first-person pin
    try:
        named = any(r.profile_of.get(g[0].lower()) == prof for g in r.entities(q)) if prof else False
    finally:
        r.owner, r._seen = owner, seen
    return "a name" if named else "i/my" if r._owner_asked(q) else "another path"


def owner_cause(r, here: dict, base: dict) -> str:
    """Why the owner is still pinned: `here` and `base` map message id -> text
    for the blocks pinning the owner at each version."""
    from deltos.recall import FIRST_PERSON, OWNER_SUBJECT
    how = collections.Counter(owner_how(r, q) for q in here.values())
    me = sum(1 for q in base.values() if FIRST_PERSON.search(q) and not OWNER_SUBJECT.search(q))
    return (f"owner pinned: NOT MET ({len(here)} > {MAX_OWNER}). Of the {len(here)} blocks pinning the owner, "
            f"the message names the owner {how['a name']}, has i/my words naming an owner entry {how['i/my']}, "
            f"reaches the owner's profile by another path {how['another path']}. Rule 5 drops only \"me\": "
            f"{me} of the {len(base)} base pins had \"me\" as their only first-person word, and "
            f"{len(set(here) ^ set(base))} blocks changed their owner pin between the versions.")


def pins_owner(block: str) -> bool:
    return any(OWNER_PIN in ln for ln in block.splitlines() if PROFILE.match(ln))


def measure(rows: list[dict], texts: dict, why: dict, names: dict, plain: dict, things: dict) -> dict:
    full = [x for x in rows if x["block"]]
    junk, owner, lines, receipts, dup, with_other, any_dup = 0, 0, 0, 0, 0, 0, 0
    junk_things: dict[str, int] = {}
    junk_by = {r: 0 for r in JUNK_REASONS}   # blocks per reason (a block can count under two)
    named = {t: 0 for t in things}
    for x in full:
        b, q = x["block"], texts[x["id"]].strip()
        pinned = [m.group(1) for ln in b.splitlines() if (m := PROFILE.match(ln))]
        bad = {s for s in pinned if s in why or (plain.get(s) and plain_initial(q, names.get(s, []), plain[s]))}
        junk += bool(bad)
        for r in {why.get(s, PLAIN) for s in bad}:
            junk_by[r] += 1
        for s in bad:
            junk_things[s] = junk_things.get(s, 0) + 1
        for t, slug in things.items():
            named[t] += slug in pinned
        owner += pins_owner(b)
        other = other_notes(b)
        if other:
            with_other += 1
            lines += len(other)
            receipts += sum(1 for t in other if t.lstrip().startswith("- said: "))
            norm = [" ".join(t.lower().split()) for t in other]
            dup += len(set(norm)) < len(norm)
        # any current fact line twice (pinned or loose): the lines the gate grades
        facts = [" ".join(TAG.sub("", ln).lower().split()) for ln in b.split("\nPages (")[0].splitlines()]
        facts = [ln for ln in facts if ln.startswith("- **")]
        any_dup += len(set(facts)) < len(facts)
    chars = sorted(len(x["block"]) for x in full)
    q90 = lambda v: v[min(len(v) - 1, int(len(v) * 0.9))] if v else 0
    return {"messages": len(rows), "blocks": len(full), "junk": junk, "junk_by": junk_by, "junk_things": junk_things,
            "things": named, "owner": owner, "other_lines": lines, "receipts": receipts,
            "with_other": with_other, "dup": dup, "any_dup": any_dup,
            "median_chars": statistics.median(chars) if chars else 0, "p90_chars": q90(chars),
            "median_all": statistics.median(len(x["block"]) for x in rows) if rows else 0,
            "total_chars": sum(chars), "ms_p50": statistics.median([x["ms"] for x in rows]) if rows else 0,
            "ms_p90": q90(sorted(x["ms"] for x in rows))}


def run(src: Path, store: Path, msgs: Path, to: Path) -> int:
    """Blocks for one version, in a child process under nice; the largest RSS
    of any child so far, in MB."""
    subprocess.run(["nice", "-n", "19", sys.executable, "-I", str(Path(__file__).resolve()), "--blocks",
                    "--src", str(src), "--store", str(store), "--msgs", str(msgs), "--to", str(to)], check=True)
    return resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss // 1024


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="", help="git revision to compare against (default: merge-base with main)")
    ap.add_argument("--store", default="", help="a scratch clone of the memory (default: out/hygiene/store)")
    ap.add_argument("--memory", default="~/deltos", help="the memory to clone when --store is missing")
    ap.add_argument("--state", default="~/.hermes/state.db")
    ap.add_argument("--since", default="2026-10-08")
    ap.add_argument("--thing", action="append", default=[], help="also count blocks pinning this thing")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--limit", type=int, default=0, help="only the first N messages (a smoke run)")
    ap.add_argument("--blocks", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--src", help=argparse.SUPPRESS)
    ap.add_argument("--msgs", help=argparse.SUPPRESS)
    ap.add_argument("--to", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.blocks:
        blocks(a.src, a.store, a.msgs, a.to)
        return 0
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    store = Path(a.store or out / "store")
    if not store.exists():
        subprocess.run(["git", "clone", "-q", os.path.expanduser(a.memory), str(store)], check=True)
    git = lambda *x: subprocess.run(["git", "-C", str(HERE), *x], capture_output=True, text=True,
                                    check=True).stdout.strip()
    base = git("rev-parse", "--short", a.base or git("merge-base", "HEAD", "main"))
    here = git("rev-parse", "--short", "HEAD") + ("-dirty" if git("status", "--porcelain", "deltos") else "")
    base_src = out / f"src-{base}"
    if not base_src.exists():
        base_src.mkdir(parents=True)
        subprocess.run(f"git -C {HERE} archive {base} deltos | tar -x -C {base_src}", shell=True, check=True)
    msgs = messages(Path(os.path.expanduser(a.state)), a.since)[:a.limit or None]
    (out / "messages.json").write_text(json.dumps(msgs))
    texts = {x["id"]: x["text"] for x in msgs}
    rss = {}
    for label, src in ((base, base_src), (here, HERE)):   # the children first: one store in memory at a time
        rss[label] = run(src, store, out / "messages.json", out / f"blocks-{label}.json")
    from deltos.memory import Memory
    r = Memory(root=store).recall()   # with the clone's owner, for the owner cause
    why, names, plain = junk_of(store, r)
    things = {}
    for t in a.thing:
        slug = next((s for s, g in names.items() if t.lower() in (n.lower() for n in g)), None)
        if slug:
            things[t] = slug
        else:
            print(f"(--thing {t}: no profile in {store} names it; not counted)")
    res, made = {}, {}
    for label in (base, here):
        made[label] = json.loads((out / f"blocks-{label}.json").read_text())
        res[label] = measure(made[label], texts, why, names, plain, things)
    (out / "report.json").write_text(json.dumps(res, indent=1))
    b, h = res[base], res[here]
    rows = [("messages / non-empty blocks", f"{b['messages']} / {b['blocks']}", f"{h['messages']} / {h['blocks']}", "")]
    for t in things:
        rows.append((f"blocks pinning {t}", b["things"][t], h["things"][t], mark(h["things"][t] == 0)))
    rows += [
        ("blocks with a junk thing", b["junk"], h["junk"], ""),
        *((f"  {r}" + (" (still indexed: needs sign-off)" if r == KIND_ONLY else ""),
           b["junk_by"][r], h["junk_by"][r], "") for r in JUNK_REASONS),
        ("owner pinned", f"{b['owner']}/{b['blocks']}", f"{h['owner']}/{h['blocks']}",
         mark(h["owner"] <= MAX_OWNER)),
        ("detached receipts / Other notes lines", f"{b['receipts']}/{b['other_lines']}",
         f"{h['receipts']}/{h['other_lines']}", mark(h["receipts"] == 0)),
        ("blocks with an Other notes line twice", f"{b['dup']}/{b['with_other']}", f"{h['dup']}/{h['with_other']}",
         mark(h["dup"] == 0)),
        ("blocks with any fact line twice", f"{b['any_dup']}/{b['blocks']}", f"{h['any_dup']}/{h['blocks']}", ""),
        ("median chars per block", b["median_chars"], h["median_chars"],
         mark(h["median_chars"] < b["median_chars"])),
        ("p90 chars per block", b["p90_chars"], h["p90_chars"], ""),
        ("median chars per message (no block = 0)", b["median_all"], h["median_all"], ""),
        ("total chars, all blocks", b["total_chars"], h["total_chars"], ""),
        ("ms per message p50 / p90", f"{b['ms_p50']} / {b['ms_p90']}", f"{h['ms_p50']} / {h['ms_p90']}", ""),
    ]
    w = max(len(r[0]) for r in rows)
    text = [f"block hygiene: {len(msgs)} user messages since {a.since}, store {store}",
            f"{'':{w}}  {'base ' + base:>16}  {'this ' + here:>16}"]
    text += [f"{r[0]:{w}}  {str(r[1]):>16}  {str(r[2]):>16}  {r[3]}" for r in rows]
    for label in (base, here):
        top = sorted(res[label]["junk_things"].items(), key=lambda kv: -kv[1])[:8]
        text.append(f"junk things in blocks at {label}: " + (", ".join(f"{s} {n}" for s, n in top) or "none"))
    causes = [thing_cause(r, t, names[slug], plain.get(slug, []),
                          [texts[x["id"]].strip() for x in made[here] if x["block"] and slug in
                           [m.group(1) for ln in x["block"].splitlines() if (m := PROFILE.match(ln))]])
              for t, slug in things.items() if h["things"][t]]
    if h["owner"] > MAX_OWNER:
        pinned = {lb: {x["id"]: texts[x["id"]].strip() for x in made[lb] if pins_owner(x["block"])} for lb in made}
        causes.append(owner_cause(r, pinned[here], pinned[base]))
    if causes:
        text.append("NOT MET, the causes:")
        text += [f"  {c}" for c in causes]
    text.append(f"max RSS: block processes {max(rss.values())} MB, this process "
                f"{resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024} MB")
    (out / "report.txt").write_text("\n".join(text) + "\n")
    print("\n".join(text))
    return 0 if all(r[3] != mark(False) for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
