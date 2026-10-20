"""The gate: every benchmark deltos has, brain-free, per question, against a
saved baseline. A change that fixes two questions and breaks two shows up as
two regressions, not as "no change". No model calls; inputs are frozen.

Suites (each question gets a status: ok > conflict > buried > miss > stale-current):
  lme        LongMemEval knowledge-update (every question with a key): the frozen
             user-turn extraction replayed through today's importer,
             Memory.context(question); keys out/gate/lme-keys.json; with
             out/gate/lme-slots.json (frozen same-fact decisions) the stores
             fold what it says is one fact under two labels; with out/gate/lme-items.json
             (frozen items-check decisions, deltos/items.py; 10-14 Phase 2) the import
             keeps "another one" beside the current value ("also current:" lines count)
  live       an agent's real questions (out/real/questions-night10.jsonl) on a frozen snapshot
             of a live memory (store + its pages + read-only pages; private, not shipped), the full per-turn block; when the
             facts don't answer, the pages part is graded too (clean > mixed > none > stale)
  import     the real git-history import replayed through today's importer (+ told
             facts), recall block on questions-v2 (skipped by --quick)
  synth      the 4 synthetic tool-writer stores, paraphrased questions
  ...        every suite module in bench/suites (bench.suites.load()); night: a real night's
             and day's logged import replayed through today's importer, graded on hand
             keep / trivia / wrong labels (import precision; skipped by --quick)

  A "current line" is a fact line (`- **Label:** value`, also under Other notes),
  not a `was:` history line or a page: pages hold the old value almost always.
  ok             the answer is on a current line, the stale value isn't
  conflict       both are on current lines (two "current" values; the reader guesses)
  buried         the answer is only in history or a page, the stale value isn't current
  miss           the answer isn't there
  stale-current  the stale value is current and the answer isn't (confident wrong answer)

  python3 -m bench.gate            # all suites vs out/gate/baseline.json, exit 1 on regression
  python3 -m bench.gate --quick    # skip the import replay and the SLOW suites (~45 s)
  python3 -m bench.gate --accept   # save this run as the baseline; refused on regressions
                                   # unless --force "why" (logged in out/gate/accept-log.jsonl)
  python3 -m bench.gate --freeze   # (re)take the live snapshot + LME extraction copy
  python3 -m bench.gate -v         # list every question that isn't ok
  python3 -m bench.gate --quick --items out/phase2/items.json
                                   # the lme suite with other items decisions (never --accept);
                                   # --items off = without any
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import bench.suites

GATE = Path("out/gate")
LIVE = GATE / "live"
LME_EXTRACT = GATE / "lme-extract.jsonl"
LME_KEYS = GATE / "lme-keys.json"
LME_ITEMS = GATE / "lme-items.json"   # frozen items decisions; --items overrides, "off" = none
BASELINE = GATE / "baseline.json"
RANK = {"ok": 4, "conflict": 3, "buried": 2, "miss": 1, "stale-current": 0}
CHARS_SLACK = 1.05            # a suite's context may grow 5% before the gate fails it
# "  - also current: X (another one, ...)": another current item under the entry above it
CURRENT = re.compile(r"^(\[[^\]\n]*\] )?(- \*\*[^*\n]+:\*\* |  - also current: )", re.M)
ITEMS: Path | None = LME_ITEMS if LME_ITEMS.exists() else None   # the lme suite keeps "another one" by these
PROFILE = re.compile(r"^Profile entries \[([^ \]·]+)")


def current_lines(ctx: str, owners: bool = False) -> str:
    """The current fact lines. owners: each led by the thing whose profile it is
    on ("ethereal dreams: - **Location:** above the bed"): a line on a thing's own
    profile is about that thing, though its text doesn't name it. For keys that
    name the thing (LME); word keys would hit other things' names ("matrix")."""
    facts, thing, out = ctx.split("\nPages (")[0], "", []
    for ln in facts.splitlines():
        h = PROFILE.match(ln)
        if h:
            thing = h.group(1).replace("-", " ")
        elif ln and not ln.startswith(("  ", "- ", "[")):
            thing = ""   # another section (Other notes, names): no owner
        if CURRENT.match(ln):
            out.append(f"{thing}: {ln}" if owners and thing and not ln.startswith("[") else ln)
    return "\n".join(out)


def status(ctx: str, has, past: bool = False, owners: bool = False) -> str:
    """has(text, which) -> bool, which = "gold" | "stale". past: the question asks
    about an earlier value, which belongs in history: anywhere in the context counts."""
    if past:
        return "ok" if has(ctx, "gold") else "miss"
    cur = current_lines(ctx, owners)
    g, s = has(cur, "gold"), has(cur, "stale")
    if g:
        return "conflict" if s else "ok"
    if s:
        return "stale-current"
    return "buried" if has(ctx, "gold") else "miss"


def rx(k: dict):
    """LongMemEval keys: regexes; "all": every gold regex must match (two-part answers)."""
    def has(text: str, which: str) -> bool:
        hit = [bool(re.search(p, text, re.I | re.S)) for p in k.get(which, [])]
        return bool(hit) and (all(hit) if which == "gold" and k.get("all") else any(hit))
    return has


def words(q: dict):
    from bench.real import _has
    return lambda text, which: bool(q.get(which)) and _has(text, q[which])


# -- suites -------------------------------------------------------------------------

def suite_lme() -> dict:
    import bench.lme as L
    from deltos import Memory
    from deltos.config import Config
    if not LME_KEYS.exists() or not LME_EXTRACT.exists():
        print(f"lme: skipped ({LME_KEYS} or {LME_EXTRACT} missing; run --freeze / write keys)")
        return {}
    keys = json.loads(LME_KEYS.read_text())
    L.READ = "user"   # what the frozen extraction read (the replay sees the same text)
    L.SLOTS, L._slots_cache = (GATE / "lme-slots.json", None) if (GATE / "lme-slots.json").exists() else (None, None)
    L.ITEMS, L._items_cache = ITEMS, None
    cache = {(r["qid"], r["sid"]): r for r in map(json.loads, open(LME_EXTRACT))}
    out, tmp = {}, Path(tempfile.mkdtemp(prefix="gate-lme-"))
    try:
        for q in L.questions(["knowledge-update"], 0):
            k = keys.get(q["question_id"])
            if not k:
                continue
            root = L.deltos_store(q, cache, rebuild=True, root=tmp / q["question_id"])
            m = Memory(config=Config(root=root, pages_write=root / "pages", owner=L.OWNER), today=L.day(q["question_date"]))
            ctx = m.context(q["question"])
            out[q["question_id"]] = {"s": status(ctx, rx(k), k.get("past", False), owners=True), "c": len(ctx)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def suite_live() -> dict:
    from deltos import Memory
    from deltos.config import Config
    if not (LIVE / "deltos" / "notes").is_dir():
        print(f"live: skipped (no snapshot in {LIVE}; run --freeze)")
        return {}
    day = (LIVE / "SNAPSHOT").read_text().split()[0]
    m = Memory(config=Config(root=LIVE / "deltos", owner=Config.load(LIVE / "deltos").owner, pages_write=LIVE / "brain",
                             pages_read={"memory": LIVE / "memory"}), today=day)
    out = {}
    for q in map(json.loads, open("out/real/questions-night10.jsonl")):
        if q["gold"][0] == "unknown":
            continue
        ctx = m.context(q["question"])
        out[q["id"]] = {"s": status(ctx, words(q)), "c": len(ctx), "p": pages_status(ctx, words(q))}
    return out


def pages_status(ctx: str, has) -> str:
    """The pages part of a block (the agent reads it too; the fact grade can't see it):
    clean = the answer without the stale value, mixed = both, none, stale = only stale."""
    pg = ctx.split("\nPages (", 1)[1] if "\nPages (" in ctx else ""
    g, s = has(pg, "gold"), has(pg, "stale")
    return "mixed" if g and s else "clean" if g else "stale" if s else "none"


PAGE_RANK = {"clean": 3, "mixed": 2, "none": 1, "stale": 0}


def suite_import() -> dict:
    if not Path("out/real/migrated-g9/migrate-log.jsonl").exists():
        print("import: skipped (its frozen import log is private, not shipped)")
        return {}
    from bench.told import apply
    from deltos.recall import Recall
    from deltos.sources.notes import replay
    tmp = Path(tempfile.mkdtemp(prefix="gate-import-"))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            replay(Path("out/real/migrated-g9/migrate-log.jsonl"), tmp)
            apply(Path("out/real/told.txt"), tmp)
        r, out = Recall(tmp), {}
        for q in map(json.loads, open("out/real/questions-v2.jsonl")):
            if q["gold"][0] == "unknown":
                continue
            ctx = r.context(q["question"])[0]
            out[q["id"]] = {"s": status(ctx, words(q)), "c": len(ctx)}
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def suite_synth() -> dict:
    from bench.score import has as score_has
    from deltos.recall import Recall
    out = {}
    for w in ("stream-tool3", "stream-tool4", "stream-tool5", "stream-tool6"):
        if not Path(f"out/{w}/questions.jsonl").exists():
            print(f"synth: skipped {w} (generated stores aren't shipped)")
            continue
        r = Recall(f"out/{w}")
        for q in map(json.loads, open(f"out/{w}/questions.jsonl")):
            if not q.get("gold") or q["gold"] == ["unknown"]:
                continue
            ctx = r.context(q.get("paraphrase") or q["question"])[0]
            vals = {"gold": q["gold"], "stale": q.get("distractors", []) if q.get("type") == "current" else []}
            has = lambda text, which, v=vals: any(score_has(text, x) for x in v[which])
            out[f"{w}:{q['id']}"] = {"s": status(ctx, has, q.get("type") != "current"), "c": len(ctx)}
    return out


SUITES = {"lme": suite_lme, "live": suite_live, "import": suite_import, "synth": suite_synth}
SUITES.update(bench.suites.load())


# -- compare ------------------------------------------------------------------------

def summary(res: dict) -> dict:
    return {k: sum(1 for v in res.values() if v["s"] == k) for k in RANK} | \
           {"n": len(res), "chars": round(sum(v["c"] for v in res.values()) / max(1, len(res))),
            "pages_clean": sum(1 for v in res.values() if v.get("p") == "clean")}


def compare(run: dict, base: dict) -> tuple[list[str], list[str], list[str]]:
    """(regressions, fixes, size warnings) between two runs' suites."""
    reg, fix, size = [], [], []
    for suite, res in run.items():
        old = base.get(suite)
        if not old or not res:
            continue
        for q, v in res.items():
            if q in old and RANK[v["s"]] != RANK[old[q]["s"]]:
                (reg if RANK[v["s"]] < RANK[old[q]["s"]] else fix).append(f"{suite}:{q} {old[q]['s']}→{v['s']}")
            elif q in old and v["s"] != "ok" and "p" in v and "p" in old[q] and v["p"] != old[q]["p"]:
                # the facts don't answer it: then the pages are what the reader has
                (reg if PAGE_RANK[v["p"]] < PAGE_RANK[old[q]["p"]] else fix).append(
                    f"{suite}:{q} pages {old[q]['p']}→{v['p']}")
        a, b = summary(res)["chars"], summary(old)["chars"]
        if b and a > b * CHARS_SLACK:
            size.append(f"{suite}: context {b}→{a} chars/q (+{100 * (a - b) / b:.0f}%)")
    return reg, fix, size


def quick_skips(name: str) -> bool:
    """--quick leaves out the import replay and every SLOW suite module."""
    return name == "import" or bench.suites.slow(SUITES[name])


def sha() -> str:
    s = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "deltos", "bench"], capture_output=True, text=True).stdout.strip()
    return s + ("-dirty" if dirty else "")


def freeze() -> None:
    """Take the live snapshot (store, its pages, read-only pages) and copy the LME extraction."""
    from deltos.config import Config
    home = Path.home()
    live = Config.load(home / "deltos")
    shutil.rmtree(LIVE, ignore_errors=True)
    LIVE.mkdir(parents=True)
    for src, dst in ((live.root, "deltos"), (live.pages_write, "brain"), (live.pages_read.get("memory"), "memory")):
        subprocess.run(["rsync", "-a", "--exclude", ".git", "--exclude", ".trash", "--exclude", ".deltos.lock",
                        f"{src}/", str(LIVE / dst)], check=True)
    rev = lambda p: subprocess.run(["git", "-C", str(p), "rev-parse", "--short", "HEAD"],
                                   capture_output=True, text=True).stdout.strip()
    (LIVE / "SNAPSHOT").write_text(f"{datetime.date.today()} deltos@{rev(live.root)} brain@{rev(live.pages_write)}\n")
    shutil.copy("out/lme/extract3-user.jsonl", LME_EXTRACT)
    print("frozen:", (LIVE / "SNAPSHOT").read_text().strip(), f"+ {LME_EXTRACT} ({sum(1 for _ in open(LME_EXTRACT))} sessions)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true", help="skip the import replay and the SLOW suites")
    ap.add_argument("--only", default="", help="comma list of suites")
    ap.add_argument("--accept", action="store_true")
    ap.add_argument("--force", default="", help="with --accept: accept regressions, and why")
    ap.add_argument("--freeze", action="store_true")
    ap.add_argument("-v", action="store_true")
    ap.add_argument("--items", default="", help="lme suite with these items decisions (json) or 'off'; never --accept")
    a = ap.parse_args()
    global ITEMS
    if a.items:
        if a.accept:
            sys.exit("--items runs are not a baseline (the baseline's decisions are out/gate/lme-items.json)")
        ITEMS = None if a.items == "off" else Path(a.items)
    sys.path.insert(0, ".")
    GATE.mkdir(parents=True, exist_ok=True)
    if a.freeze:
        freeze()
        return
    names = a.only.split(",") if a.only else [s for s in SUITES if not (a.quick and quick_skips(s))]
    run, t0 = {}, time.time()
    for n in names:
        t = time.time()
        run[n] = SUITES[n]()
        if run[n]:
            s = summary(run[n])
            print(f"{n:7} ok {s['ok']}/{s['n']}  conflict {s['conflict']}  buried {s['buried']}  miss {s['miss']}  "
                  f"stale-current {s['stale-current']}  {s['chars']} chars/q"
                  + (f"  pages-clean {s['pages_clean']}" if any("p" in v for v in run[n].values()) else "")
                  + f"  ({time.time() - t:.0f} s)", flush=True)
            if a.v:
                for q, v in run[n].items():
                    if v["s"] != "ok":
                        print(f"    {v['s']:11} {q}")
    base = json.loads(BASELINE.read_text()) if BASELINE.exists() else {"suites": {}}
    reg, fix, size = compare(run, base["suites"])
    rec = {"sha": sha(), "at": datetime.datetime.now().isoformat(timespec="seconds"), "suites": run}
    (GATE / "runs").mkdir(exist_ok=True)
    (GATE / "runs" / f"{rec['at'].replace(':', '')}-{rec['sha']}.json").write_text(json.dumps(rec) + "\n")
    print(f"vs baseline {base.get('sha', '(none)')}: {len(fix)} fixed, {len(reg)} regressed  ({time.time() - t0:.0f} s)")
    for x in fix:
        print("  FIXED    ", x)
    for x in reg:
        print("  REGRESSED", x)
    for x in size:
        print("  BIGGER   ", x)
    bad = bool(reg or size)
    if a.accept:
        if bad and not a.force:
            sys.exit("not accepted: regressions (fix them, or --accept --force \"why\")")
        merged = dict(base["suites"]) | {k: v for k, v in run.items() if v}
        BASELINE.write_text(json.dumps({"sha": rec["sha"], "at": rec["at"], "suites": merged}) + "\n")
        with open(GATE / "accept-log.jsonl", "a") as fh:
            fh.write(json.dumps({"sha": rec["sha"], "at": rec["at"], "fixed": fix, "regressed": reg,
                                 "bigger": size, "why": a.force}) + "\n")
        print("baseline ->", rec["sha"])
        return
    print("GATE:", "FAIL" if bad else "PASS")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
