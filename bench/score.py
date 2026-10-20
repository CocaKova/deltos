"""Deterministic scoring. Values in the world are distinctive strings, so we
match on normalized substrings instead of using an LLM judge.

Outcomes per question:
  correct      gold value present, no other historical value present
  stale        an older/never-true value of the same fact, and not gold (the worst sin)
  hedged       gold and a distractor both present
  abstained    said it didn't know (correct for "unknown" questions, and the
               honest move when the answer was erased from memory)
  wrong        anything else (includes hallucinated answers to "unknown")
"""
from __future__ import annotations

import re
from collections import defaultdict


def norm(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    return re.sub(r"\b(the|a|an) ", "", s)  # "the Annex closet" == "Annex closet"


def has(ans: str, val: str) -> bool:
    return f" {norm(val)} " in f" {norm(ans)} "


def grade(q: dict, a: dict) -> str:
    ans = a.get("answer", "")
    abstain = a.get("abstain") or not norm(ans)
    if q["type"] == "unknown":
        return "abstained" if abstain else "wrong"
    if abstain:
        return "abstained"
    g = any(has(ans, v) for v in q["gold"])
    d = any(has(ans, v) for v in q["distractors"])
    if g and d:
        return "hedged"
    if g:
        return "correct"
    if d:
        return "stale"
    return "wrong"


def summarize(rows: list[dict]) -> dict:
    """rows: [{q, a, outcome}] -> metrics overall and by question type."""
    def block(rs):
        n = len(rs)
        c = defaultdict(int)
        for r in rs:
            c[r["outcome"]] += 1
        answerable = [r for r in rs if r["q"]["type"] != "unknown"]
        conf_wrong = [r for r in answerable if r["outcome"] in ("stale", "wrong")
                      and r["a"].get("confidence", 0) >= 0.8]
        return {
            "n": n,
            "accuracy": round((c["correct"] + (c["abstained"] if rs and rs[0]["q"]["type"] == "unknown" else 0)) / n, 3) if n else None,
            "stale_rate": round(c["stale"] / len(answerable), 3) if answerable else None,
            "confident_wrong": len(conf_wrong),
            **{k: c[k] for k in ("correct", "stale", "hedged", "abstained", "wrong")},
        }

    by_type = defaultdict(list)
    for r in rows:
        by_type[r["q"]["type"]].append(r)
    unknown = by_type.get("unknown", [])
    answerable = [r for r in rows if r["q"]["type"] != "unknown"]
    traps = [r for r in rows if r["q"].get("stale_trap")]
    vol = defaultdict(list)
    for r in answerable:
        vol[r["q"]["volatility"]].append(r)
    toks = [r["a"].get("usage", {}) for r in rows]
    kept = [r for r in answerable if r["q"].get("evidence", "kept") != "erased"]
    erased = [r for r in answerable if r["q"].get("evidence") == "erased"]
    ev = defaultdict(list)
    for r in answerable:
        ev[r["q"].get("evidence", "kept")].append(r)
    return {
        "overall": {
            # The reader's skill: questions whose answer still exists somewhere in memory.
            "truth_accuracy": round(sum(r["outcome"] == "correct" for r in kept) / len(kept), 3) if kept else None,
            # The writer's damage: questions whose answer memory destroyed.
            "erased": len(erased),
            "honest_on_erased": round(sum(r["outcome"] == "abstained" for r in erased) / len(erased), 3) if erased else None,
            "truth_accuracy_all": round(sum(r["outcome"] == "correct" for r in answerable) / len(answerable), 3),
            "stale_rate": round(sum(r["outcome"] == "stale" for r in answerable) / len(answerable), 3),
            "abstain_when_unknown": round(sum(r["outcome"] == "abstained" for r in unknown) / len(unknown), 3) if unknown else None,
            "stale_trap_accuracy": round(sum(r["outcome"] == "correct" for r in traps) / len(traps), 3) if traps else None,
            "confident_wrong": sum(1 for r in answerable if r["outcome"] in ("stale", "wrong")
                                   and r["a"].get("confidence", 0) >= 0.8),
            "avg_prompt_tokens": round(sum(t.get("prompt_tokens", 0) for t in toks) / len(rows)),
            "avg_seconds": round(sum(r.get("seconds", 0) for r in rows) / len(rows), 1),
        },
        "by_type": {k: block(v) for k, v in sorted(by_type.items())},
        "by_evidence": {k: block(v) for k, v in sorted(ev.items())},
        "by_volatility": {k: block(v) for k, v in sorted(vol.items())},
    }
