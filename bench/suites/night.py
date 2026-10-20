"""The night suite: import precision on real nights. The other suites count what memory
answers (recall); none sees the trivia and the wrong lines an import writes. Here a
real night's and a real day's logged import are replayed, with today's importer, onto
the store they met, and every hand-labelled line is graded on whether it landed:

  label  landed           not landed
  K      ok               miss            keep: a line worth holding
  T      conflict         ok              trivia: true, not worth a line
  W      stale-current    ok              wrong or misleading

A line landed when its thing and label resolve to a profile entry whose current value,
or an "also current" item, is the labelled value (lowercased, as bench.importtest._hit
compares). qid = night:<run>:<sha1(line)[:10]>, one per label per run, no context ("c" 0).
Store health (facts, refused, ungrounded, new things, thin new things, lint) is printed,
not gated. The frozen inputs are private (out/gate/night/, git-ignored):

  base/                the store the runs met (git archive of the memory repo)
  runs/<run>.jsonl     each run's logged rows, applied in order
  texts/<run>.json     a run's unit texts by "note#chunk" (grounds the replay); none = applied as logged
  labels.jsonl         {"line": "thing | label | value", "v": "K" | "T" | "W", "src"}

  python3 -m bench.suites.night                      # the suite alone, per-run counts
  python3 -m bench.suites.night --freeze --labels DIR
      DIR: the label sittings (blind-key3/4.json, numbered as lab3/4.py's K and W sets
      list them; labels.py with base/entries.jsonl, its seed-19 sample)
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from collections import Counter
from pathlib import Path

from deltos import items as _items
from deltos.sources import notes
from deltos.store import Store, _attr_key

NAME, SLOW = "night", True
D = Path("out/gate/night")
STATUS = {("K", True): "ok", ("K", False): "miss", ("T", True): "conflict", ("T", False): "ok",
          ("W", True): "stale-current", ("W", False): "ok"}
# What freeze takes: the store both 10-19 runs met, the day run's commit, the night's frozen log.
BASE_REV, DAY_REV, NIGHT = "679a131", "cbb9df8", Path("out/night1019")
BLIND = ("3", "4")   # blind-key<n>.json, labelled in lab<n>.py


def qid(run: str, line: str) -> str:
    return f"night:{run}:{hashlib.sha1(line.encode()).hexdigest()[:10]}"


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def landed(s: Store, line: str) -> bool:
    """Is the labelled value current on its thing's entry (or another current item there)?"""
    thing, label, value = line.split(" | ", 2)
    t = s.find(thing)
    if ";" in label:
        # "Sam | Samuel Reed; sreed | person": names written without the "=" of a
        # registration, labelled for what they say. Landed when each name finds the thing.
        return t is not None and all((f := s.find(n.strip())) is not None and f.name == t.name
                                     for n in label.split(";") if n.strip())
    # the store's own match: "Setting build host", "build host" and "build_host" are one entry
    a = next((v for k, v in t.attrs.items() if _attr_key(k) == _attr_key(label)), None) if t else None
    return a is not None and _norm(value) in {_norm(x.value) for x in [a, *a.also]}


def things(root: Path) -> set[str]:
    return {p.stem for p in (root / "notes").glob("*.md")
            if not p.stem.endswith(("-extra", "-history")) and not p.stem.startswith("log-")}


def replay(base: Path, rows: list[dict], texts: dict, into: Path) -> list[dict]:
    """The rows applied in order onto a copy of base at into, the way the run applied them:
    its owner, its items decisions (no model: an event not decided is "same") and their evidence."""
    shutil.copytree(base, into)
    toml = into / "deltos.toml"
    owner = tomllib.loads(toml.read_text()).get("owner", "") if toml.exists() else ""
    m = notes.Migrator(into, owner=owner, items=_items.Items(_items.load(into / "items.json"), asker=None))
    m._said = notes.load_said(into / "items-said.json")
    sts = []
    with contextlib.redirect_stdout(io.StringIO()):
        for r in rows:
            m.chat = r.get("kind") == "chat"
            sts.append(m.session(r["note"], r["day"], texts.get(f"{r['note']}#{r.get('chunk', 0)}", ""), r["day"],
                                 git_mode=True, replay=r))
    return sts


def health(root: Path, before: set[str], sts: list[dict]) -> dict:
    """Store-health numbers of one replay (printed, not gated)."""
    s, new = Store(root), sorted(things(root) - before)
    thin = 0
    for stem in new:
        t = s.find(s.label(stem).split(" (")[0]) if stem in s.rows else None
        thin += sum(1 for k in (t.attrs if t else {}) if k != "kind") <= 1
    out = {k: sum(x.get(k, 0) for x in sts) for k in ("facts", "refused", "ungrounded")}
    out |= {"new_things": len(new), "thin_new_things": thin}
    try:
        from deltos import lint
        out["lint"] = lint.junk(lint.lint(root))
    except ImportError:
        pass
    return out


def grade(base: Path, rows: list[dict], texts: dict, labels: list[dict]) -> tuple[dict, dict]:
    """{line: landed} for every label after one run's replay, and its store health."""
    tmp = Path(tempfile.mkdtemp(prefix="gate-night-"))
    try:
        sts = replay(base, rows, texts, tmp / "store")
        s = Store(tmp / "store")
        return {x["line"]: landed(s, x["line"]) for x in labels}, health(tmp / "store", things(base), sts)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run() -> dict:
    if not (D / "labels.jsonl").exists() or not (D / "base" / "notes").is_dir():
        print(f"night: skipped (no frozen night in {D}; private, not shipped)")
        return {}
    labels = [json.loads(l) for l in (D / "labels.jsonl").read_text().splitlines() if l.strip()]
    n = Counter(x["v"] for x in labels)
    out = {}
    for p in sorted((D / "runs").glob("*.jsonl")):
        t0 = time.time()
        rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
        tp = D / "texts" / f"{p.stem}.json"
        got, h = grade(D / "base", rows, json.loads(tp.read_text()) if tp.exists() else {}, labels)
        k = Counter(x["v"] for x in labels if got[x["line"]])
        for x in labels:
            out[qid(p.stem, x["line"])] = {"s": STATUS[x["v"], got[x["line"]]], "c": 0}
        print(f"  {p.stem}: landed keep {k['K']}/{n['K']}, trivia {k['T']}/{n['T']}, wrong {k['W']}/{n['W']}"
              f"  | facts {h['facts']}  refused {h['refused']}  ungrounded {h['ungrounded']}  new things "
              f"{h['new_things']} (thin {h['thin_new_things']})" + (f"  lint {h['lint']}" if "lint" in h else "")
              + f"  ({time.time() - t0:.0f} s)", flush=True)
    return out


# -- freeze -------------------------------------------------------------------------

def index_set(lab: Path, c: str) -> set[int]:
    """A labelling script's K or W set: the 1-based numbers its blind key printed."""
    m = re.search(rf"^{c} = \{{([\d,\s]*)\}}", lab.read_text(), re.M)
    return {int(x) for x in m.group(1).split(",") if x.strip()} if m else set()


def assemble(d: Path) -> tuple[list[dict], list[str]]:
    """Every hand label in d, one row per line; a line two sittings labelled differently is
    left out (returned second)."""
    from bench.groundcheck import hand_labels
    rows = []
    for b in BLIND:
        key = json.loads((d / f"blind-key{b}.json").read_text())
        k, w = (index_set(d / f"lab{b}.py", c) for c in "KW")
        rows += [{"line": line, "v": "K" if i in k else "W" if i in w else "T", "src": f"blind{b}:{arm}"}
                 for i, (arm, line) in enumerate(key, 1)]
    sample, lab = hand_labels(d)
    rows += [{"line": f"{e['thing']} | {e['label']} | {e['value']}", "v": v, "src": "seed19"} for e, v in zip(sample, lab)]
    by: dict[str, dict] = {}
    split = []
    for r in rows:
        x = by.setdefault(r["line"], {**r, "src": []})
        x["src"].append(r["src"])
        if x["v"] != r["v"]:
            split.append(r["line"])
    return [dict(x, src="+".join(x["src"])) for line, x in by.items() if line not in split], sorted(set(split))


def freeze(labels: Path, repo: str = "~/deltos") -> None:
    """(Re)take the suite's inputs: the base store, the night's rows and texts, the day's rows
    (the ones DAY_REV added to the memory repo's migrate-log.jsonl), and the labels."""
    from bench.groundcheck import night_base
    git = lambda *a: subprocess.run(["git", "-C", str(Path(repo).expanduser()), *a], capture_output=True, text=True,
                                    check=True).stdout.splitlines()
    shutil.rmtree(D, ignore_errors=True)
    (D / "runs").mkdir(parents=True)
    (D / "texts").mkdir()
    night_base("", repo, BASE_REV, D / "base")
    shutil.copy(NIGHT / "migrate-log.jsonl", D / "runs" / "night1019.jsonl")
    shutil.copy(NIGHT / "texts.json", D / "texts" / "night1019.json")
    old, new = git("show", f"{DAY_REV}^:migrate-log.jsonl"), git("show", f"{DAY_REV}:migrate-log.jsonl")
    if new[:len(old)] != old:
        sys.exit(f"{DAY_REV} rewrote migrate-log.jsonl instead of adding to it")
    (D / "runs" / "day1019.jsonl").write_text("".join(l + "\n" for l in new[len(old):]))
    rows, split = assemble(labels.expanduser())
    (D / "labels.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    n = Counter(r["v"] for r in rows)
    print(f"frozen {D}: base {repo}@{BASE_REV} ({len(things(D / 'base'))} things), night {len((D / 'runs' / 'night1019.jsonl').read_text().splitlines())}"
          f" rows, day {len(new) - len(old)} rows ({DAY_REV}); {len(rows)} labels: keep {n['K']}, trivia {n['T']}, "
          f"wrong {n['W']}; {len(split)} left out (labelled two ways)")
    for line in split:
        print("  two ways:", line[:120])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--freeze", action="store_true")
    ap.add_argument("--labels", default="~/n19", help="with --freeze: the directory of label sittings")
    ap.add_argument("--repo", default="~/deltos", help="with --freeze: the memory repo (read with git, never written)")
    a = ap.parse_args()
    sys.path.insert(0, ".")
    if a.freeze:
        return freeze(Path(a.labels), a.repo)
    t0 = time.time()
    res = run()
    print(f"night: {', '.join(f'{k} {v}' for k, v in sorted(Counter(x['s'] for x in res.values()).items()))}"
          f"  ({time.time() - t0:.0f} s)")


if __name__ == "__main__":
    main()
