"""How long does the per-turn block take to build: from the raw message, as the
Hermes adapter's on_turn_start did (before), vs from the query Hermes hands
prefetch() with skill scaffolding stripped (after)? Every user message in a
Hermes state.db since a day (read-only) goes through Memory.context on a
scratch copy of the store; pages are read from their configured paths. No model.

  nice -n 19 python3 -m bench.latency            # ~/.hermes/state.db since 2026-10-08
  python3 -m bench.latency --since 2026-10-08 --db ~/.hermes/state.db --live ~/deltos --limit 20

The cleaning is Hermes' own extract_user_instruction_from_skill_message (and its
is_trivial_prompt gate for the carry share), imported from --hermes; without
Hermes the raw message is used and the report says so. Writes out/latency/
(the store copy, report.json, rows.jsonl: ids, sizes and times, no message text).

Pass (pre-registered 10-19): max after <= 4 s (was 31.2 s on a 60,987-char skill
turn), and the messages Hermes leaves unchanged give byte-identical blocks.
Also reported: the share of turns that would carry a previous turn.

10-19, 187 messages since 10-08 (29 skill turns), nice -n 19, 149 MB: before p50
0.28 s, p95 30.2 s, max 33.0 s; after p50 0.28 s, p95 3.65 s, max 3.97 s (the
skill turns' cleaned queries, 7,215 chars); 158 of 158 unchanged messages gave
identical blocks; 30 of 186 prefetched turns carry a previous one (30 of 113 outside cron).
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import resource
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from deltos import Memory

OUT = Path("out/latency")
MAX_AFTER_S = 4.0


def hermes_gates(hermes: Path):
    """Hermes' skill-scaffolding stripper and trivial-prompt gate, or (None, None)."""
    sys.path.append(str(hermes))   # last: Hermes' packages never shadow ours
    try:
        from agent.memory_provider import is_trivial_prompt
        from agent.skill_commands import extract_user_instruction_from_skill_message
        return extract_user_instruction_from_skill_message, is_trivial_prompt
    except Exception as exc:
        print(f"Hermes not importable from {hermes} ({exc}): the raw message is used for 'after'")
        return None, None


def messages(db: Path, since: str) -> list[dict]:
    t0 = datetime.datetime.fromisoformat(since).timestamp()   # local midnight
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute("SELECT m.id, m.session_id, COALESCE(s.source, ''), m.content FROM messages m "
                       "LEFT JOIN sessions s ON s.id = m.session_id WHERE m.role = 'user' AND m.timestamp >= ? "
                       "AND m.content IS NOT NULL AND m.content != '' ORDER BY m.id", (t0,)).fetchall()
    con.close()
    return [{"id": i, "session": s, "source": src, "text": t} for i, s, src, t in rows]


def pct(xs: list[float], p: float) -> float:
    """Nearest-rank percentile."""
    xs = sorted(xs)
    return xs[max(0, math.ceil(p / 100 * len(xs)) - 1)] if xs else 0.0


def timed(m: Memory, q: str) -> tuple[str, float]:
    t = time.perf_counter()
    block = m.context(q)
    return block, time.perf_counter() - t


def stats(xs: list[float]) -> dict:
    return {"n": len(xs), "p50": round(pct(xs, 50), 2), "p95": round(pct(xs, 95), 2), "max": round(max(xs, default=0), 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="~/.hermes/state.db")
    ap.add_argument("--since", default="2026-10-08")
    ap.add_argument("--live", default="~/deltos", help="the store to copy (read only)")
    ap.add_argument("--store", default=str(OUT / "store"), help="the scratch copy (made from --live if missing)")
    ap.add_argument("--hermes", default="~/.hermes/hermes-agent")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    store = Path(a.store)
    if not store.exists():
        shutil.copytree(Path(a.live).expanduser(), store, ignore=shutil.ignore_patterns(".git"))
    clean, trivial = hermes_gates(Path(a.hermes).expanduser())
    msgs = messages(Path(a.db).expanduser(), a.since)[:a.limit or None]
    m = Memory(store)
    m.context("warm up")   # the first call reads the store: no message pays for it
    rows, seen = [], set()
    for x in msgs:
        raw = x["text"]
        c = clean(raw) if clean else raw
        before, tb = timed(m, raw.strip())
        after, ta = timed(m, c.strip()) if c and c.strip() else ("", None)
        # Hermes prefetches when the raw query isn't trivial and the cleaned one isn't empty;
        # the adapter carries a session's previous prefetched query into the next one.
        called = not (trivial and trivial(raw)) and bool(c and c.strip())
        rows.append({"id": x["id"], "source": x["source"], "chars": len(raw), "clean_chars": len(c or ""),
                     "skill": c != raw, "before_s": round(tb, 3), "after_s": None if ta is None else round(ta, 3),
                     "identical": before == after, "called": called, "carried": called and x["session"] in seen})
        if called:
            seen.add(x["session"])
        print(f"{x['id']} {x['source']:<10} {len(raw):>6} -> {len(c or ''):>6} chars  "
              f"before {tb:6.2f}s  after {'-' if ta is None else f'{ta:6.2f}s'}", flush=True)
    plain = [r for r in rows if not r["skill"]]
    calls = [r for r in rows if r["called"]]
    talk = [r for r in calls if r["source"] != "cron"]
    after = [r["after_s"] for r in rows if r["after_s"] is not None]
    rep = {"since": a.since, "messages": len(rows), "hermes_cleaner": bool(clean),
           "skill_turns": len(rows) - len(plain), "no_prefetch": sum(1 for r in rows if r["after_s"] is None),
           "before_s": stats([r["before_s"] for r in rows]), "after_s": stats(after),
           "after_skill_s": stats([r["after_s"] for r in rows if r["skill"] and r["after_s"] is not None]),
           "plain": len(plain), "plain_identical": sum(1 for r in plain if r["identical"]),
           "carried": f"{sum(r['carried'] for r in calls)}/{len(calls)}",
           "carried_not_cron": f"{sum(r['carried'] for r in talk)}/{len(talk)}",
           "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)}
    rep["pass"] = {"max_after": rep["after_s"]["max"] <= MAX_AFTER_S,
                   "plain_identical": rep["plain_identical"] == rep["plain"]}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (OUT / "report.json").write_text(json.dumps(rep, indent=1) + "\n")
    print(json.dumps(rep, indent=1))


if __name__ == "__main__":
    main()
