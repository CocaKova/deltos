"""The grounding gate (deltos/ground.py), measured brain-free on logged model output:
every part replays what a model already said, with grounding off and on. Read-only:
each store is a temp copy; results go to out/groundcheck.json.

  importtest  the logged import-test runs (out/importtest/runs), each piece replayed with
              its text into a copy of the gate's frozen live store, scored on the 71 hand facts
  night       a night's logged import (out/night1019) with its texts, replayed onto the store
              it met (git archive <rev> of the memory repo), and the hand labels still written
  lme         the gate's frozen LongMemEval extraction: lines the chat rule and the note
              rule would drop, and how many of them hit a gold answer
  receipts    the night replay's new receipts: do they hold a word of their value, how long

  python3 -m bench.groundcheck --labels DIR            # DIR: labels.py + base/entries.jsonl
  python3 -m bench.groundcheck --only importtest,lme
"""
from __future__ import annotations

import argparse
import io
import json
import random
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections import Counter
from pathlib import Path

from deltos import ground
from deltos.sources import notes
from deltos.store import Store, _attr_key

RUNS = ("glm-v3-g0", "glm-v3-t06", "glm-v3-t06s1", "glm-v3-t06s2", "glm-v3-gm06", "glm-v3-gm10", "fn-v3", "dsv-v3")
# The 10-19 scratch measure (a filter on the lines each run took, before the gate was code):
# lines off -> on, hand facts found off -> on. Pre-registered: within 2% lines and 1 fact.
SCRATCH = {"glm-v3-g0": (1913, 1390, 60, 60), "glm-v3-t06": (710, 561, 62, 62), "glm-v3-t06s1": (975, 877, 54, 54),
           "glm-v3-t06s2": (1070, 776, 58, 57), "glm-v3-gm06": (1541, 1124, 65, 65),
           "glm-v3-gm10": (1152, 953, 55, 55), "fn-v3": (1010, 608, 49, 48), "dsv-v3": (1845, 1548, 61, 61)}
NIGHT = Path("out/night1019")
NIGHT_PATHS = ("notes", "items.json", "items-said.json", "deltos.toml", "MEMORY.md")
MODES = (("off", None), ("on", "auto"))


def within(on_lines: int, on_found: int, run: str) -> bool:
    """Pre-registered (a), per run: grounding-on lines within 2% of the scratch measure and
    hand facts found within 1 of it."""
    _, sl_on, _, sg_on = SCRATCH[run]
    return abs(on_lines - sl_on) <= 0.02 * sl_on and abs(on_found - sg_on) <= 1


def verdict(what: str, ok: bool) -> str:
    return f"{what}: {'PASS' if ok else 'FAIL'}"


def things(root: Path) -> set[str]:
    return {p.stem for p in (root / "notes").glob("*.md")
            if not p.stem.endswith(("-extra", "-history")) and not p.stem.startswith("log-")}


def total(st: dict, k: str) -> int:
    """A count over a session and its halves (a SUMMED count is already the session's total)."""
    return st.get(k, 0) if k in notes.SUMMED else st.get(k, 0) + sum(total(x, k) for x in st.get("subs") or [])


def profiles(root: Path) -> dict:
    """Every thing's entries: {thing: {label: Attr}}, read through the store."""
    s, out = Store(root), {}
    for sl in sorted(set(s.by_name.values())):
        if sl not in s.rows:
            continue
        t = s.find(s.label(sl).split(" (")[0])
        if t is not None:
            out[t.name] = t.attrs
    return out


# -- importtest ---------------------------------------------------------------------

def part_importtest() -> dict:
    import bench.importtest as it
    g = json.loads(it.GOLD.read_text())
    chunks = {f"{c['note']}#{c['chunk']}": c for c in map(json.loads, it.CHUNKS.read_text().splitlines())}
    base = things(it.STORE)
    res = {}
    print(f"{'run':14} {'lines off→on':>14} {'drop':>6} {'found off→on':>13} {'ungr':>5} {'unnamed':>7} {'kind':>4} "
          f"{'new off→on':>11}  scratch (lines, found)")
    for run in RUNS:
        p = it.RUNS / f"{run}.jsonl"
        if not p.exists():
            print(f"{run:14} (no log)")
            continue
        rows = [json.loads(l) for l in p.read_text().splitlines()]
        r = {}
        for mode, kind in MODES:
            tot, missed = Counter(), {}
            for row in rows:
                if "st" not in row or "error" in row or row["key"] not in g["chunks"]:
                    continue
                c = chunks[row["key"]]
                tmp = Path(tempfile.mkdtemp(prefix="groundcheck-"))
                try:
                    shutil.copytree(it.STORE, tmp / "store")
                    st = notes.Migrator(tmp / "store", lean=True, ground=kind).session(
                        c["note"], c["day"], c["text"], c["day"], git_mode=True, replay=row["st"])
                    sc = it.score(st, g["chunks"][row["key"]], g["forbid_all"])
                    tot["new"] += len(things(tmp / "store") - base)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
                tot["lines"] += sc["lines"]
                tot["found"] += sc["found"]
                tot["gold"] += sc["gold"]
                for k in ("ungrounded", "unnamed", "kind_only", "alias_ungrounded"):
                    tot[k] += total(st, k)
                missed[row["key"]] = sc["missed"]
            r[mode] = dict(tot, missed=missed)
        off, on = r["off"], r["on"]
        lost = [f"{k}: {v}" for k in on["missed"] for v in on["missed"][k] if v not in off["missed"].get(k, [])]
        drop = 1 - on["lines"] / max(1, off["lines"])
        sl_off, sl_on, sg_off, sg_on = SCRATCH[run]
        r.update(lost=lost, drop=round(drop, 4),
                 vs_scratch={"lines_on": f"{on['lines']} vs {sl_on} ({(on['lines'] - sl_on) / sl_on:+.1%})",
                             "found_on": f"{on['found']} vs {sg_on} ({on['found'] - sg_on:+d})",
                             "within": within(on["lines"], on["found"], run)})
        for m in ("off", "on"):
            r[m].pop("missed")
        res[run] = r
        print(f"{run:14} {off['lines']:>6} → {on['lines']:<5} {drop:>6.1%} {off['found']:>5} → {on['found']:<2}/{on['gold']}"
              f" {on['ungrounded']:>5} {on['unnamed']:>7} {on['kind_only']:>4} {off['new']:>5} → {on['new']:<4}"
              f"  ({sl_off}→{sl_on}, {sg_off}→{sg_on}){'' if r['vs_scratch']['within'] else '  OUTSIDE ±2%/±1'}")
        for x in lost:
            print(f"{'':14} lost: {x}")
    if res:
        mean = sum(r["drop"] for r in res.values()) / len(res)
        worst = max(r["off"]["found"] - r["on"]["found"] for r in res.values())
        runs = [r for k, r in res.items() if not k.startswith("_")]
        inside = sum(1 for r in runs if r["vs_scratch"]["within"])
        res["_pass"] = {"mean_line_drop": round(mean, 4), "max_gold_lost": worst,
                        "pass": worst <= 1 and mean >= 0.15,
                        "within_scratch": f"{inside}/{len(runs)}", "scratch_pass": inside == len(runs)}
        print("(a) " + verdict(f"mean line drop {mean:.1%} (≥ 15%), most hand facts lost in a run {worst} (≤ 1)",
                               res["_pass"]["pass"]))
        print("(a) " + verdict(f"runs within ±2% lines and ±1 found of the scratch measure {inside}/{len(runs)} "
                               "(all)", res["_pass"]["scratch_pass"]))
    return res


# -- night --------------------------------------------------------------------------

def night_base(src: str, repo: str, rev: str, into: Path) -> Path:
    """The store the night met: a directory, a tar, or `git archive <rev>` of the memory repo."""
    if src and Path(src).expanduser().is_dir():
        shutil.copytree(Path(src).expanduser(), into, ignore=shutil.ignore_patterns(".git"))
        return into
    if src:
        data = Path(src).expanduser().read_bytes()
    else:
        tracked = subprocess.run(["git", "-C", str(Path(repo).expanduser()), "ls-tree", "--name-only", rev],
                                 capture_output=True, text=True, check=True).stdout.split()
        data = subprocess.run(["git", "-C", str(Path(repo).expanduser()), "archive", rev,
                               *[x for x in NIGHT_PATHS if x in tracked]], capture_output=True, check=True).stdout
    into.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        tf.extractall(into, filter="data")
    return into


def entries(after: dict, before: dict) -> list[tuple[str, str, str]]:
    """The entries a replay wrote: new, or with another value than the store had."""
    out = []
    for name, attrs in after.items():
        b = before.get(name) or {}
        for k, a in attrs.items():
            if k not in b or b[k].value != a.value:
                out.append((name, a.name, a.value))
    return out


def hand_labels(d: Path) -> tuple[list, str]:
    """The 80 hand-labelled entries (seed 19) and their labels, the way labels.py picks them."""
    lab = re.search(r'^L = "([KTW]+)"', (d / "labels.py").read_text(), re.M).group(1)
    base = [json.loads(l) for l in (d / "base" / "entries.jsonl").read_text().splitlines()]
    return random.Random(19).sample(base, len(lab)), lab


def part_night(base_src: str, repo: str, rev: str, labels: str) -> dict:
    texts = json.loads((NIGHT / "texts.json").read_text())
    rows = [json.loads(l) for l in (NIGHT / "migrate-log.jsonl").read_text().splitlines()]
    work = Path(tempfile.mkdtemp(prefix="groundcheck-night-"))
    res = {}
    try:
        base = night_base(base_src, repo, rev, work / "base")
        before, before_things = profiles(base), things(base)
        labelled = hand_labels(Path(labels).expanduser()) if labels else None
        print(f"night: {len(rows)} logged sessions onto {base_src or f'{repo}@{rev}'} "
              f"({len(before_things)} things)")
        for mode, kind in MODES:
            root = work / mode
            shutil.copytree(base, root)
            m = notes.Migrator(root, ground=kind)
            sts = [m.session(r["note"], r["day"], texts.get(f"{r['note']}#{r.get('chunk', 0)}", ""), r["day"],
                             git_mode=True, replay=r) for r in rows]
            after = profiles(root)
            new = sorted(things(root) - before_things)
            s = Store(root)
            nonkind = {}
            for stem in new:
                t = s.find(s.label(stem).split(" (")[0]) if stem in s.rows else None
                nonkind[stem] = sum(1 for k in (t.attrs if t else {}) if _attr_key(k) != "kind")
            written = entries(after, before)
            r = {"facts": sum(x.get("facts", 0) for x in sts), "restated": sum(x.get("restated", 0) for x in sts),
                 "refused": sum(x.get("refused", 0) for x in sts),
                 "ungrounded": sum(x.get("ungrounded", 0) for x in sts),
                 "unnamed": sum(total(x, "unnamed") for x in sts), "kind_only_refused": sum(total(x, "kind_only") for x in sts),
                 "alias_ungrounded": sum(total(x, "alias_ungrounded") for x in sts),
                 "new_things": len(new), "new_le1": sum(1 for x in new if nonkind[x] <= 1),
                 "new_kind_only": sum(1 for x in new if nonkind[x] == 0), "entries_written": len(written),
                 "summary": notes.summary(sts)}
            if labelled:
                have = {tuple(x.lower() for x in e) for e in written}
                sample, lab = labelled
                key = lambda e: (e["thing"].lower(), e["label"].lower(), e["value"].lower())
                r["labels"] = {c: f"{sum(1 for e, l in zip(sample, lab) if l == c and key(e) in have)}/{lab.count(c)}"
                               for c in "KTW"}
            r["receipts"] = receipts(after, before)
            r["ungrounded_restates"] = restates(sts, Store(base))
            res[mode] = r
            print(f"  {mode:3} facts {r['facts']}  refused {r['refused']}  ungrounded {r['ungrounded']}  "
                  f"unnamed {r['unnamed']}  kind-only refused {r['kind_only_refused']}  aliases dropped "
                  f"{r['alias_ungrounded']}  new things {r['new_things']} (≤1 non-kind entry {r['new_le1']}, "
                  f"kind-only {r['new_kind_only']})  entries written {r['entries_written']}"
                  + (f"  labels still written: keep {r['labels']['K']}, trivia {r['labels']['T']}, "
                     f"wrong {r['labels']['W']}" if labelled else ""))
            print(f"      receipts: {r['receipts']}")
        on, off = res["on"], res["off"]
        res["ungrounded_share"] = round(on["ungrounded"] / max(1, on["ungrounded"] + on["facts"]), 4)
        rs = on["ungrounded_restates"]
        other = on["ungrounded"] - rs
        res["ungrounded_other_share"] = round(other / max(1, other + on["facts"] - on["restated"]), 4)
        print(f"  ungrounded {on['ungrounded']} of {on['ungrounded'] + on['facts']} values the store would take "
              f"({res['ungrounded_share']:.1%}); {rs} only repeat memory's current value, {other} are other values: "
              f"{res['ungrounded_other_share']:.1%} of the values that are not restates (10-19 expectation ~13%)")
        if labelled:
            k, w = (int(on["labels"][c].split("/")[0]) for c in "KW")
            res["_pass"] = {"kind_only_new": on["new_kind_only"] == 0, "wrong_le_10": w <= 10, "keep_ge_21": k >= 21}
        else:
            res["_pass"] = {"kind_only_new": on["new_kind_only"] == 0}
        rc = on["receipts"]
        res["_pass"]["receipts"] = rc["written"] > 0 and rc["with_value_token"] == rc["written"] and rc["max_len"] <= 200
        print("  (b) " + verdict(f"kind-only new things {on['new_kind_only']} (0)", res["_pass"]["kind_only_new"]))
        if labelled:
            print("  (b) " + verdict(f"hand labels W still written {on['labels']['W']} (≤ 10)", res["_pass"]["wrong_le_10"]))
            print("  (b) " + verdict(f"hand labels K still written {on['labels']['K']} (≥ 21)", res["_pass"]["keep_ge_21"]))
        print("  (d) " + verdict(f"receipts holding a word of their value {rc['with_value_token']}/{rc['written']} "
                                 f"(all), longest {rc['max_len']} (≤ 200)", res["_pass"]["receipts"]))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return res


def restates(sts: list[dict], base: Store) -> int:
    """Ungrounded lines whose value is the one memory already held: the reference list copied back."""
    n = 0
    for st in sts:
        for r in [x for node in walk(st) for x in node.get("refusals", []) if x.endswith("-> not recorded: not in the text")]:
            p = r.rsplit(" -> ", 1)[0].split("|")
            t = base.find(p[0]) if len(p) >= 3 else None
            a = next((v for k, v in t.attrs.items() if _attr_key(k) == _attr_key(p[1])), None) if t else None
            n += bool(a and a.value.strip().lower() == p[2].strip().lower())
    return n


def walk(st: dict):
    yield st
    for x in st.get("subs") or []:
        yield from walk(x)


def receipts(after: dict, before: dict) -> dict:
    """The receipts a replay wrote (said lines new or changed): how many hold a word of their value."""
    n = hit = longest = 0
    for name, attrs in after.items():
        b = before.get(name) or {}
        for k, a in attrs.items():
            if a.said and (k not in b or b[k].said != a.said):
                n += 1
                hit += bool(ground.tokens(a.said[0]) & ground.tokens(a.value))
                longest = max(longest, len(a.said[0]))
    return {"written": n, "with_value_token": hit, "max_len": longest}


# -- lme ----------------------------------------------------------------------------

def gold_hit(p: list[str], gold: list[str]) -> bool:
    """Does an extraction line hit a gold answer? Read with its label, the way the gate reads
    an entry: a key like '(painting|project).*\\b5\\b' needs the label, the value is only '5'."""
    return any(re.search(x, f"{p[1]}: {p[2]}", re.I) for x in gold)


def part_lme() -> dict:
    import bench.lme as L
    from bench.gate import LME_EXTRACT, LME_KEYS
    L.READ = "user"   # what the frozen extraction read
    keys = json.loads(LME_KEYS.read_text())
    cache = {(r["qid"], r["sid"]): r for r in map(json.loads, LME_EXTRACT.read_text().splitlines())}
    n = gold = 0
    drop = {"chat": [0, 0, set()], "note": [0, 0, set()]}
    examples = []
    for q in L.questions(["knowledge-update"], 0):
        k = keys.get(q["question_id"])
        if not k:
            continue
        for sid, d, turns in L.sessions(q):
            r = cache.get((L.mid(q), sid))
            if not r:
                continue
            text = L.for_import(turns)
            for raw in (r.get("raw", ""), r.get("repair_raw", "")):
                for p in notes._plan(raw, False)[0]:
                    if p[0].startswith(("=", "+")) or len(p) < 4:
                        continue
                    n += 1
                    hit = gold_hit(p, k.get("gold", []))
                    gold += hit
                    for rule in drop:
                        if not ground.grounded(p[2], text, rule, d):
                            drop[rule][0] += 1
                            if hit:
                                drop[rule][1] += 1
                                drop[rule][2].add(q["question_id"])
                                if rule == "chat":
                                    examples.append(f"{q['question_id']}: {' | '.join(p[:3])}"[:140])
    res = {"lines": n, "gold_hitting": gold}
    print(f"lme: {n} extraction lines, {gold} hit a gold answer")
    for rule, (dl, dg, qs) in drop.items():
        res[rule] = {"dropped": dl, "share": round(dl / max(1, n), 4), "gold_dropped": dg, "questions": sorted(qs)}
        print(f"  {rule} rule: drops {dl} ({dl / max(1, n):.1%}), gold-hitting {dg} in {len(qs)} question(s)")
    for e in examples:
        print(f"    chat drop: {e}")
    res["_pass"] = {"pass": res["chat"]["share"] <= 0.02 and res["chat"]["gold_dropped"] <= 1}
    print("  (c) " + verdict(f"chat rule drops {res['chat']['share']:.1%} of lines (≤ 2%) and "
                             f"{res['chat']['gold_dropped']} gold-hitting (≤ 1)", res["_pass"]["pass"]))
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="comma list of parts: importtest, night, lme")
    ap.add_argument("--night-base", default="", help="the store the night met: a directory or a tar")
    ap.add_argument("--night-repo", default="~/deltos", help="else: the memory repo to git archive")
    ap.add_argument("--night-rev", default="679a131", help="the commit the night started from")
    ap.add_argument("--labels", default="", help="a directory with labels.py and base/entries.jsonl")
    a = ap.parse_args()
    sys.path.insert(0, ".")
    parts = a.only.split(",") if a.only else ["importtest", "night", "lme"]
    out = {}
    if "importtest" in parts:
        out["importtest"] = part_importtest()
    if "night" in parts:
        out["night"] = part_night(a.night_base, a.night_repo, a.night_rev, a.labels)
    if "lme" in parts:
        out["lme"] = part_lme()
    Path("out").mkdir(exist_ok=True)
    Path("out/groundcheck.json").write_text(json.dumps(out, indent=1, default=sorted) + "\n")


if __name__ == "__main__":
    main()
