"""Would deltos.health have held each import a store took? Replayed from the
store's own git history, no brain.

  git clone ~/deltos /tmp/store-copy      # a clone: never the live store
  python3 -m bench.healthcheck /tmp/store-copy --extra out/night1019/migrate-log.jsonl@679a131 [--delta]

Every commit that added rows to migrate-log.jsonl is a run: the rows it added are
assessed against the last WINDOW rows of the same kind in the log at its parent
(what the run would have been judged against). An undo commit takes rows out of the
log, so the run after it is judged without the undone night. --extra LOG@REV
assesses a log that never landed against the store's log at REV.

Each run also shows a counterfactual: a refused-share rule (refused lines as
binomial events against the history's refused share, ALPHA, MIN_EVENTS) and what
it would have said. --delta adds the store change of each commit (its notes/ against
its parent's, which includes the agent and hand writes committed with it) and how
long health.store_delta took on it.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from pathlib import Path

from deltos import health


def _git(clone: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(clone), *args], capture_output=True, text=True)


def log_at(clone: Path, rev: str) -> list[str]:
    """migrate-log.jsonl at a commit, as lines ([] where it didn't exist yet)."""
    r = _git(clone, "show", f"{rev}:migrate-log.jsonl")
    return [ln for ln in r.stdout.splitlines() if ln.strip()] if r.returncode == 0 else []


def added(before: list[str], after: list[str]) -> list[str]:
    """The lines after holds that before didn't, in after's order (as a multiset)."""
    left = Counter(before)
    out = []
    for ln in after:
        if left[ln]:
            left[ln] -= 1
        else:
            out.append(ln)
    return out


def history_from(lines: list[str], kind: str) -> list[dict]:
    """health.history() over a log given as lines, through a scratch root."""
    with tempfile.TemporaryDirectory() as d:
        Path(d, "migrate-log.jsonl").write_text("\n".join(lines) + "\n" if lines else "")
        return health.history(Path(d), kind)


def refused_rule(rows: list[dict], hist: list[dict]) -> str:
    """The counterfactual: refused lines held against the history's refused share."""
    run = [health.signals(r) for r in rows if "error" not in r]
    x, n = sum(s["refused"] for s in run), sum(s["facts"] + s["refused"] for s in run)
    hx, hn = sum(s["refused"] for s in hist), sum(s["facts"] + s["refused"] for s in hist)
    p0 = (hx + 0.5) / (hn + 1)
    p = health.binom_sf(x, n, p0)
    would = "HOLD" if x >= health.MIN_EVENTS and p < health.ALPHA else "pass"
    return f"refused {x}/{n} ({x / max(1, n):.0%}) vs normal {hx / max(1, hn):.0%}, p={p:.1g}: {would}"


def _notes_at(clone: Path, rev: str, into: Path) -> Path:
    """notes/ of a commit, extracted to a scratch root (fresh mtimes: the slow case)."""
    (into / "notes").mkdir(parents=True, exist_ok=True)
    arch = subprocess.run(["git", "-C", str(clone), "archive", rev, "notes"], capture_output=True) if rev else None
    if arch is not None and arch.returncode == 0:
        subprocess.run(["tar", "-x", "-C", str(into)], input=arch.stdout, check=True)
    return into


def assess_run(name: str, rows: list[dict], before: list[str], delta: dict | None = None) -> list[str]:
    out = []
    for kind in sorted({health.kind(r) for r in rows}):
        rs = [r for r in rows if health.kind(r) == kind]
        hist = history_from(before, kind)
        v = health.assess(rs, hist, delta)
        m = v.metrics
        bands = " ".join(f"{e} {m.get(e, 0)}/{m['pieces']} p={m[f'{e}_p']:.2g}" for e in health.EVENTS)
        vol = ", ".join(f"{word} {m[key]:g} vs " + (f"{m[key + '_normal']:g}" if key + "_normal" in m else "-")
                        + (f" p={m[p_key]:.2g}" if p_key in m else "")
                        for word, key, p_key in (("lines", "out_lines", "volume_p"),
                                                 ("facts", "facts_per_piece", "yield_p")))
        out.append(f"{name:<14} {kind:<5} {m['pieces']:>4} {m['history']:>4}  {v.decision.upper():<8} "
                   f"{bands}, {vol}  | {refused_rule(rs, hist)}")
        for why in v.reasons:
            out.append(f"{'':<16}hold: {why}")
        if delta:
            out.append(f"{'':<16}store: " + ", ".join(f"{k} {val}" for k, val in delta.items()))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clone", type=Path, help="a git clone of a deltos store")
    ap.add_argument("--extra", action="append", default=[], metavar="LOG@REV",
                    help="assess this migrate-log against the clone's log at REV")
    ap.add_argument("--delta", action="store_true", help="also the store delta per commit, timed")
    a = ap.parse_args(argv)
    clone = a.clone.resolve()
    revs = _git(clone, "log", "--reverse", "--format=%h", "--", "migrate-log.jsonl").stdout.split()
    print(f"health: WINDOW {health.WINDOW}, ALPHA {health.ALPHA}, MIN_EVENTS {health.MIN_EVENTS}, "
          f"MATERIAL {health.MATERIAL}")
    print(f"{'run':<14} {'kind':<5} {'rows':>4} {'hist':>4}  {'verdict':<8} bands  | counterfactual refused-share rule")
    timings = []
    for rev in revs:
        r = _git(clone, "rev-parse", "--short", "--verify", "-q", f"{rev}^")
        parent = r.stdout.strip() if r.returncode == 0 else ""   # "" = the first commit
        before = log_at(clone, parent) if parent else []
        new = added(before, log_at(clone, rev))
        if not new:
            continue   # an undo, or a commit that only rewrote rows
        delta = None
        if a.delta:
            with tempfile.TemporaryDirectory() as d:
                b, af = _notes_at(clone, parent, Path(d, "b")), _notes_at(clone, rev, Path(d, "a"))
                t = time.perf_counter()
                delta = health.store_delta(b, af)
                timings.append((rev, len(health._profiles(af)), time.perf_counter() - t))
        print("\n".join(assess_run(rev, [json.loads(ln) for ln in new], before, delta)))
    for spec in a.extra:
        path, _, rev = spec.rpartition("@")
        rows = [json.loads(ln) for ln in Path(path).read_text().splitlines() if ln.strip()]
        print("\n".join(assess_run(f"{Path(path).parent.name}@{rev}", rows, log_at(clone, rev))))
    if a.delta:
        for rev, n, s in timings:
            print(f"store_delta {rev}: {n} profiles, {s:.2f} s (every mtime differs)")
        with tempfile.TemporaryDirectory() as d:   # a staged copy keeps mtimes (copytree = copy2)
            b, af = Path(d, "b"), Path(d, "a")
            shutil.copytree(clone / "notes", b / "notes")
            shutil.copytree(b / "notes", af / "notes")
            t = time.perf_counter()
            health.store_delta(b, af)
            print(f"store_delta staged copy: {len(health._profiles(af))} profiles, "
                  f"{time.perf_counter() - t:.2f} s (mtimes kept)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
