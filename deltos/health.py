"""Is a run healthy? Judged against what this store has already accepted.

A run (an import of notes, or chats learned) is staged in a copy, then compared
with the store's own last accepted pieces of the same kind: did more pieces loop,
get cut off, or lose facts than usual, did it write far more lines per piece, and
did it store far fewer facts per piece? No constant here is fit to one model. A
swapped model is judged by whether its run looks like this store's normal runs.

A store with no accepted pieces of a kind yet judges nothing: that first run is
reported and promoted, and becomes the normal the next run is judged against (the
10-08 seed, 540 pieces, 31 cut off: promote). An even prior held a first run once 7
of 7 pieces were cut off, and a hold file stops every later run of its kind until
someone acts: a fresh install would stall on a model that thinks past the cap.

History is kept per kind of unit: a chat is a few short turns, a note piece up to
9,000 chars, and their numbers never share a band.

10-19, the store's own log replayed (bench/healthcheck.py): this holds the 10-17
and 10-19 nightlies (both undone by hand) and the 10-14 catch-up (3 of 4 cut off),
and passes the seven other nightlies and the 10-19 daytime re-run. A refused-share
rule would have held that daytime run too (21% of its lines against 13% in the 60
before it), so refused share is reported, never judged. What a model that breaks
the format does is judged instead by facts stored per piece: 20 pieces the store
refused every line of, or 20 answered in prose, store 0 against a normal 38 (the
log at cbb9df8) and hold; none of the twelve real runs comes near (p 0.075 or more).

Pure functions, stdlib only: deltos/runs.py calls assess() and render().
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import math
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

from .store import _attr_key, parse_thing

WINDOW = 60          # accepted pieces of one kind a run is compared with (10-19: 8 nightlies + 19 seed pieces)
ALPHA = 0.01         # a signal holds only when the store's normal rate makes it this unlikely
MIN_EVENTS = 2       # one loop or one cut-off is never enough on its own
MATERIAL = 1.5       # a volume or yield shift must also be this large (median vs median)
MIN_N_VOLUME = 3     # pieces a run needs before its output volume and yield are tested
MIN_H_VOLUME = 10    # accepted pieces the volume and yield tests need as their normal
EVENTS = ("loop", "cut", "lost")
SAID = ("raw", "repair_raw", "followup_raw", "missed_raw")   # what the model wrote, per call

# More hold rules, registered by name: fn(metrics, hist) -> a reason, or None.
# Empty today; the lint ratchet and the ungrounded share go here once measured.
# Like every signal, they are not consulted while a kind has no history.
HOLD_RULES: dict = {}


@dataclass
class Verdict:
    decision: str                      # "promote" | "hold" | "empty"
    reasons: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def _tree(row: dict):
    """A piece and the halves it was asked again in, recursively."""
    yield row
    for sub in row.get("subs") or []:
        yield from _tree(sub)


def _total(row: dict, key: str) -> int:
    """A count over a piece and its halves. The import adds a half's counts into
    its parent (notes.SUMMED), so the top row already holds those totals; a count
    it doesn't add up is summed over the tree here."""
    from .sources.notes import SUMMED
    if key in SUMMED:
        return row.get(key) or 0
    return sum(x.get(key) or 0 for x in _tree(row))


def kind(row: dict) -> str:
    return row.get("kind", "note")


_kind = kind   # history() takes the kind it wants as `kind`


def signals(row: dict) -> dict:
    """One logged piece, as the numbers a run is judged by. A loop the call
    recovered from (asked again and answered cleanly) is counted apart, not as a loop."""
    nodes = list(_tree(row))
    facts, refused = _total(row, "facts"), _total(row, "refused")
    return {
        "loop": any(x.get("loop") and not x.get("recovered") for x in nodes),
        "cut": bool(row.get("capped")),
        "lost": (row.get("lost") or 0) > 0,
        "recovered": any(x.get("loop") and x.get("recovered") for x in nodes),
        "out_lines": sum(1 for x in nodes for k in SAID for ln in (x.get(k) or "").splitlines() if ln.strip()),
        "facts": facts,
        "refused": refused,
        "ungrounded": _total(row, "ungrounded"),
        "refused_share": refused / max(1, facts + refused),
    }


def history(root: Path, kind: str) -> list[dict]:
    """Signals of the last WINDOW logged pieces of this kind. A row reaches the live
    log only once its run was accepted (a held run stays staged; an undone night is
    taken out of the log), so this is the store's normal."""
    log = Path(root) / "migrate-log.jsonl"
    if not log.exists():
        return []
    out = []
    for ln in reversed(log.read_text(errors="replace").splitlines()):
        if len(out) >= WINDOW:
            break
        try:
            r = json.loads(ln)
        except ValueError:
            continue   # a torn last line
        if isinstance(r, dict) and "error" not in r and _kind(r) == kind:
            out.append(signals(r))
    return out[::-1]


def binom_sf(x: int, n: int, p: float) -> float:
    """P(X >= x) for X ~ Binomial(n, p): the exact sum, in logs so that a run of
    thousands of lines doesn't overflow."""
    if x <= 0:
        return 1.0
    if x > n or p <= 0:
        return 0.0
    if p >= 1:
        return 1.0
    lp, lq, ln_ = math.log(p), math.log1p(-p), math.lgamma(n + 1)
    return min(1.0, sum(math.exp(ln_ - math.lgamma(k + 1) - math.lgamma(n - k + 1) + k * lp + (n - k) * lq)
                        for k in range(x, n + 1)))


def mwu_p(a: list, b: list) -> float:
    """One-sided Mann-Whitney U: the p-value for a's values running higher than b's.
    Normal approximation, with the tie correction and a continuity correction."""
    n1, n2 = len(a), len(b)
    if not n1 or not n2:
        return 1.0
    vals = sorted([(v, 0) for v in a] + [(v, 1) for v in b])
    ra, i = 0.0, 0
    while i < len(vals):
        j = i
        while j + 1 < len(vals) and vals[j + 1][0] == vals[i][0]:
            j += 1
        rank = (i + j) / 2 + 1   # tied values share their mean rank
        ra += rank * sum(1 for k in range(i, j + 1) if vals[k][1] == 0)
        i = j + 1
    big_n = n1 + n2
    ties = sum(c ** 3 - c for c in Counter(v for v, _ in vals).values())
    var = n1 * n2 / 12 * ((big_n + 1) - ties / (big_n * (big_n - 1)))
    if var <= 0:
        return 1.0   # every value tied: no evidence either way
    u = ra - n1 * (n1 + 1) / 2
    z = (u - n1 * n2 / 2 - 0.5) / math.sqrt(var)
    return 0.5 * math.erfc(z / math.sqrt(2))


def _profiles(root: Path) -> dict[str, os.stat_result]:
    """notes/*.md that are things: not a cold history, an -extra page or a daily log."""
    out = {}
    try:
        it = os.scandir(Path(root) / "notes")
    except FileNotFoundError:
        return out
    with it:
        for e in it:
            n = e.name
            if (n.endswith(".md") and not n.endswith(("-history.md", "-extra.md")) and not n.startswith("log-")
                    and e.is_file()):
                out[n[:-3]] = e.stat()
    return out


def _lint_total(root: Path):
    """deltos.lint's total for a store, when that module exists and answers in its
    shape (lint(root) -> {'total': n, ...}); else None and the delta is left out."""
    if importlib.util.find_spec("deltos.lint") is None:
        return None
    try:
        res = importlib.import_module("deltos.lint").lint(Path(root))
        return res["total"] if isinstance(res, dict) and isinstance(res.get("total"), int) else None
    except Exception:   # a report line must never fail the run it reports on
        return None


def store_delta(base: Path, after: Path) -> dict:
    """What a run changed in the store's profiles: new things, new things with at
    most one entry that isn't a Kind (a name with nothing known about it), and
    profiles changed. Size and mtime first, bytes only when they can't tell: on the
    10-19 store (1,909 profiles) 0.03 s against a staged copy (copytree keeps mtimes),
    0.25-0.63 s against a fresh git extraction where every mtime differs."""
    b, a = _profiles(base), _profiles(after)
    new = sorted(set(a) - set(b))
    thin = 0
    for stem in new:
        t = parse_thing((Path(after) / "notes" / f"{stem}.md").read_text(errors="replace"))
        thin += sum(1 for x in t.attrs.values() if _attr_key(x.name) != "kind") <= 1
    changed = 0
    for stem in set(a) & set(b):
        sa, sb = a[stem], b[stem]
        if sa.st_size != sb.st_size:
            changed += 1
        elif sa.st_mtime_ns != sb.st_mtime_ns:
            changed += ((Path(after) / "notes" / f"{stem}.md").read_bytes()
                        != (Path(base) / "notes" / f"{stem}.md").read_bytes())
    out = {"new_things": len(new), "new_thin": thin, "changed_profiles": changed}
    lb, la = _lint_total(base), _lint_total(after)
    if lb is not None and la is not None:
        out["lint_delta"] = la - lb
    return out


def _sig(p: float) -> float:
    return float(f"{p:.3g}")


def assess(rows: list[dict], hist: list[dict], delta: dict | None = None, errors: list | None = None) -> Verdict:
    """promote, hold (with its reasons) or empty, for the pieces a run logged.
    Loops, cut-offs and lost facts are each a count of pieces, held when the count
    is too high for the store's normal rate (exact binomial, ALPHA) and at least
    MIN_EVENTS; output lines per piece hold when clearly higher (Mann-Whitney, ALPHA)
    and materially so (MATERIAL x the normal median), facts stored per piece when
    clearly and materially lower. Everything else is reported. With no history of
    the kind, nothing is judged: the run promotes and becomes the normal."""
    errors = list(errors or []) + [r for r in rows if "error" in r]
    run = [signals(r) for r in rows if "error" not in r]
    hist = [h if "out_lines" in h else signals(h) for h in hist]
    n, big_h = len(run), len(hist)
    m = {"pieces": n, "history": big_h}
    if n == 0:
        m["errors"] = len(errors)
        return Verdict("empty", [], m)
    reasons = []
    words = {"loop": "loops in", "cut": "cut off in", "lost": "facts lost past a cut in"}
    for e in EVENTS:
        x, h = sum(s[e] for s in run), sum(s[e] for s in hist)
        p0 = (h + 0.5) / (big_h + 1)
        p = binom_sf(x, n, p0)
        m[e], m[f"{e}_normal"], m[f"{e}_p"] = x, h, _sig(p)
        if x >= MIN_EVENTS and p < ALPHA:
            reasons.append(f"{words[e]} {x} of {n} pieces (normal {h} of {big_h}, p={p:.1g})")
    out, normal = [s["out_lines"] for s in run], [s["out_lines"] for s in hist]
    got, usual = [s["facts"] for s in run], [s["facts"] for s in hist]
    m["out_lines"], m["facts_per_piece"] = median(out), median(got)
    if hist:
        m["out_lines_normal"], m["facts_per_piece_normal"] = median(normal), median(usual)
    if n >= MIN_N_VOLUME and big_h >= MIN_H_VOLUME:
        p = mwu_p(out, normal)
        m["volume_p"] = _sig(p)
        if p < ALPHA and median(out) / max(1, median(normal)) > MATERIAL:
            reasons.append(f"output {median(out):g} lines per piece vs normal {median(normal):g}")
        # The yield, the volume test turned round. A model whose every line the store
        # refuses, or that answers in prose, neither loops nor runs long: it stores
        # nothing. Promoted, its notes are logged as done and never imported again.
        p = mwu_p(usual, got)
        m["yield_p"] = _sig(p)
        if p < ALPHA and median(got) * MATERIAL < median(usual):
            reasons.append(f"stored {median(got):g} facts per piece vs normal {median(usual):g}")
    facts, refused = sum(s["facts"] for s in run), sum(s["refused"] for s in run)
    m.update(facts=facts, refused=refused, ungrounded=sum(s["ungrounded"] for s in run),
             refused_share=round(refused / max(1, facts + refused), 3),
             recovered_loops=sum(s["recovered"] for s in run), errors=len(errors))
    if hist:
        hf, hr = sum(s["facts"] for s in hist), sum(s["refused"] for s in hist)
        m["refused_share_normal"] = round(hr / max(1, hf + hr), 3)
    m.update(delta or {})
    if not hist:
        # Nothing to judge against: a fresh install's first run, or a store's first
        # run of a new kind. Held, it would block every later run of the kind.
        return Verdict("promote", [], m)
    for fn in HOLD_RULES.values():
        why = fn(m, hist)
        if why:
            reasons.append(why)
    return Verdict("hold" if reasons else "promote", reasons, m)


def fingerprint(parts: dict) -> str:
    """Which model, endpoint, temperature and prompts made a run: recorded with it,
    never judged (a new model is judged by its numbers)."""
    canon = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha1(canon.encode()).hexdigest()[:12]


def _pct(x: float) -> str:
    return f"{x:.0%}"


def render(verdict: Verdict, run_id: str, label: str) -> str:
    """The report a person reads: why, the run's numbers against its normal band, and
    what to do. The first line is the hold file's first line."""
    m = verdict.metrics
    if verdict.decision == "hold":
        lines = [f"held {label} run {run_id}: " + "; ".join(verdict.reasons)]
    elif verdict.decision == "empty":
        errs = f", {m['errors']} error(s)" if m.get("errors") else ""
        return f"empty {label} run {run_id}: nothing logged{errs}\n"
    elif not m.get("history"):
        lines = [f"ok {label} run {run_id}: no accepted history of this kind yet, so nothing is judged"]
    else:
        lines = [f"ok {label} run {run_id}: every signal within the store's normal band"]
    n, big_h = m.get("pieces", 0), m.get("history", 0)
    lines += ["", f"{n} pieces, against the last {big_h} accepted of the same kind"]
    names = {"loop": "loops", "cut": "cut off", "lost": "facts lost"}
    for e in EVENTS:
        if e in m:
            lines.append(f"  {names[e]:<14}{m[e]:>4} of {n:<5} normal {m[f'{e}_normal']} of {big_h}"
                         f"   p={m[f'{e}_p']:.2g}")
    short = "too few pieces" if n < MIN_N_VOLUME else "too little history"
    for key, name, p_key in (("out_lines", "output lines", "volume_p"), ("facts_per_piece", "facts stored", "yield_p")):
        if key in m:
            band = f"normal {m[key + '_normal']:g}" if key + "_normal" in m else "no normal yet"
            tested = f"   p={m[p_key]:.2g}" if p_key in m else f"   (not tested: {short})"
            lines.append(f"  {name:<14}{m[key]:>4g} per piece (median), {band}{tested}")
    if "refused_share" in m:
        band = f"normal {_pct(m['refused_share_normal'])}" if "refused_share_normal" in m else "no normal yet"
        lines.append(f"  {'refused share':<14}{_pct(m['refused_share']):>4} of lines, {band}   (reported, not judged)")
    lines.append(f"facts {m.get('facts', 0)}, refused {m.get('refused', 0)}, ungrounded {m.get('ungrounded', 0)}, "
                 f"recovered loops {m.get('recovered_loops', 0)}, errors {m.get('errors', 0)}")
    if "new_things" in m:
        st = (f"store: new things {m['new_things']} ({m.get('new_thin', 0)} with at most one fact besides a Kind), "
              f"changed profiles {m.get('changed_profiles', 0)}")
        if "lint_delta" in m:
            st += f", lint {m['lint_delta']:+d}"
        lines.append(st)
    if verdict.decision == "hold":
        lines += ["", f"deltos run promote {run_id}   # keep it anyway",
                  f"deltos run discard {run_id}   # drop it; its units are imported again next run"]
    else:
        lines += ["", f"deltos run undo {run_id}   # take it back out"]
    return "\n".join(lines) + "\n"
