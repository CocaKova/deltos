"""Page recency and follow-up carry (10-19), brain-free: the pages part of the
per-turn block at this code against another checkout (main), on a private
read-only snapshot of a live memory and its chats. No model calls; the
snapshot and the probe questions stay outside the repo (they describe someone's
real setup).

  python3 -m bench.pagecheck --snap DIR --take                 # copy the store + its pages, read-only
  python3 -m bench.pagecheck --snap DIR --before ../main-checkout --probes probes.json

probes.json (private): {"family": [question, paraphrase, ...],
                        "target": regex a line of the right page matches,
                        "followups": [[first message, follow-up], ...]}

  a) family     rank of the first page with a `target` line, in the ranking the
                block's pages come from (#1 = the block's first page)
  b) opened     chats since --since where the agent's first memory read of the turn
                was page_read: ok when that page is in the block's 3
  c) stale      the gate's live questions on the snapshot: blocks led by a page over
                30 days old; and pages that describe a superseded version (a version
                a thing's profile lists as an old value) against the newest later page
                whose title names the same thing and a version: ok when the newer ranks above
  d) follow-up  two-turn pairs from the chats where the second has <= 6 words or names
                nothing: ok when what the agent opened in turn 2 (page_read / memory_show),
                else the thing turn 1 names, is in the block; with and without carry.
                Counter-metric, over every such pair (a known answer or not): turns
                whose block gains, by carry, a profile the agent read in neither turn
                (a wrong subject, or at best one it didn't need)
  e) latency    Memory.context p50/p95 over the gate's live questions (frozen snapshot)
  plus the gate's live suite graded at both (a preview, not the gate)

Each side runs in its own process with its own checkout first on sys.path.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

DEEP = 20          # ranking depth for the family probes
PAIR_DEEP = 60     # ranking depth for the superseded pairs
STALE_DAYS = 30
MAX_CHARS = 4000   # longer messages (pasted logs, skill scaffolding) are left out
ROUNDS = 3         # latency: the fastest of 3 runs per question
LABEL = re.compile(r"^\[(.+?) · (\d{4}-\d\d-\d\d)[^\]\n]*\]$", re.M)
PROFILE = re.compile(r"^Profile entries \[(.+?) · ", re.M)
VER = re.compile(r"(?<![\w.])v?(\d+\.\d+(?:\.\d+)?(?:-[a-z0-9.]+)?)(?![\w.])", re.I)


# -- the snapshot ----------------------------------------------------------------

def take(snap: Path, root: str | None) -> None:
    """Copy the store, its writable pages and its read-only pages into snap, read-only."""
    from deltos.config import Config
    c = Config.load(root)
    snap.mkdir(parents=True, exist_ok=True)
    dirs = [(c.root, "deltos")] + ([(c.pages_write, "pages")] if c.pages_write else []) + \
        [(p, f"read-{k}") for k, p in c.pages_read.items()]
    for src, dst in dirs:
        if (snap / dst).exists():
            subprocess.run(["chmod", "-R", "u+w", str(snap / dst)], check=True)
        subprocess.run(["rsync", "-a", "--delete", "--exclude", ".git", "--exclude", ".trash", "--exclude",
                        ".deltos.lock", f"{src}/", str(snap / dst)], check=True)
        subprocess.run(["chmod", "-R", "a-w", str(snap / dst)], check=True)
    (snap / "SNAPSHOT.json").write_text(json.dumps({
        "day": datetime.date.today().isoformat(), "owner": c.owner, "read": sorted(c.pages_read),
        "pages": bool(c.pages_write), "chats": str(c.sessions.get("hermes", ""))}) + "\n")
    print("snapshot:", snap, (snap / "SNAPSHOT.json").read_text().strip())


def memory(snap: Path):
    from deltos import Memory
    from deltos.config import Config
    s = json.loads((snap / "SNAPSHOT.json").read_text())
    c = Config(root=snap / "deltos", owner=s["owner"], pages_write=snap / "pages" if s["pages"] else None,
               pages_read={k: snap / f"read-{k}" for k in s["read"]})
    return Memory(config=c, today=s["day"]), s


def gate_memory(live: Path):
    from deltos import Memory
    from deltos.config import Config
    day = (live / "SNAPSHOT").read_text().split()[0]
    return Memory(config=Config(root=live / "deltos", owner=Config.load(live / "deltos").owner,
                                pages_write=live / "brain", pages_read={"memory": live / "memory"}), today=day)


# -- what the chats say (parent side, read-only) ------------------------------------

def _cleaner():
    """Hermes' own skill-scaffolding cleaner (what its prefetch is given), when
    importable; else the message as it is."""
    sys.dont_write_bytecode = True   # never leave caches in the agent's install
    try:
        from agent.skill_commands import extract_user_instruction_from_skill_message as clean
        return clean, True
    except Exception:
        return (lambda c: c), False


def _calls(raw: str | None) -> list[tuple[str, dict]]:
    """An assistant message's tool calls as (name, args), batched calls unwrapped."""
    out = []
    try:
        items = json.loads(raw or "[]")
    except ValueError:
        return out
    for x in items if isinstance(items, list) else []:
        f = x.get("function", {}) if isinstance(x, dict) else {}
        try:
            args = json.loads(f.get("arguments") or "{}")
        except (TypeError, ValueError):
            args = {}
        args = args if isinstance(args, dict) else {}
        if f.get("name") == "tool_call":
            out += [(y.get("name"), y.get("arguments") if isinstance(y.get("arguments"), dict) else {})
                    for y in args.get("calls") or [] if isinstance(y, dict)]
        else:
            out.append((f.get("name"), args))
    return out


def turns(db: Path, since: str) -> list[dict]:
    """User turns since `since` (YYYY-MM-DD), in order per session: the cleaned
    message and the tool calls the agent made before the next user message."""
    hermes = db.parent
    if hermes.is_dir() and str(hermes) not in sys.path:
        sys.path.append(str(hermes / "hermes-agent"))
    clean, cleaned = _cleaner()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute("select m.id, m.session_id, m.role, m.content, m.tool_calls from messages m "
                       "where m.timestamp >= strftime('%s', ?) order by m.session_id, m.id", (since,)).fetchall()
    out = []
    for mid, sid, role, content, calls in rows:
        if role == "user":
            q = clean(content or "") or ""
            out.append({"id": mid, "sid": sid, "q": q.strip(), "calls": []})
        elif role == "assistant" and out and out[-1]["sid"] == sid:
            out[-1]["calls"] += _calls(calls)
    print(f"chats: {len(out)} user turns since {since}" + ("" if cleaned else " (no Hermes cleaner: raw messages)"))
    return out


def opened(t: dict, reads: set[str]) -> tuple[str, str] | None:
    """The first memory read of a turn, as ("page", ref) or ("thing", name)."""
    for name, args in t["calls"]:
        if name in reads:
            if name == "page_read" and args.get("ref"):
                return "page", str(args["ref"])
            if name == "memory_show" and args.get("thing"):
                return "thing", str(args["thing"])
            return None   # a search first: what it opened came from the search
    return None


def superseded(m) -> list[dict]:
    """Pages that describe a version a thing's profile lists as an old value
    (their title/description names the thing and the old version): [{page,
    thing, label, old, now}]. Only the read-only folders, the agent's own notes
    are rewritten in place."""
    from deltos.recall import _value
    r, notes = m.recall(), m.root / "notes"

    def values(prof: str):
        cur, old, lab = {}, {}, None
        p = notes / prof.removeprefix("notes/")
        for ln in p.read_text().splitlines():
            if ln.startswith("- **") and ":**" in ln:
                lab = ln.split(":**")[0][4:].lower()
                cur[lab], old[lab] = _value(ln), old.get(lab, set())
            elif ln.startswith("  - was: ") and lab:
                old[lab].add(re.sub(r" \(\d{4}-.*$", "", ln[9:]).strip())
        h = notes / p.name.replace(".md", "-history.md")
        if h.is_file():
            for ln in h.read_text().splitlines():
                mm = re.match(r"- ([^:]+): (.*?) \(\d{4}-\d\d-\d\d to ", ln)
                if mm:
                    old.setdefault(mm.group(1).lower(), set()).add(mm.group(2).strip())
        return cur, old

    out = []
    for f in m.pages().folders[1:]:
        if not f.searchable or f.prefix in ("sessions/",):
            continue
        for page in sorted(f.path.glob("*.md")):
            if page.stem.split("-")[0].isupper():
                continue   # an index (MEMORY.md, ARCHIVE-index-...)
            head = _head(page, first=True)
            said = {v.lower().lstrip("v") for v in VER.findall(head)}
            hits = []
            for g in r.entities(head)[:3]:
                prof = r.profile_of.get(g[0].lower())
                if not prof:
                    continue
                cur, old = values(prof)
                for lab in cur:
                    if "version" not in lab and "release" not in lab:
                        continue
                    now = cur[lab].lower().lstrip("v").split()[0] if cur[lab] else ""
                    hit = sorted(s for s in said if s in {o.lower().lstrip("v") for o in old.get(lab, ())} and s != now)
                    if hit and now:
                        hits.append({"page": f.prefix + page.name, "thing": g[0], "label": lab, "old": hit[0],
                                     "now": now, "head": head})
            out += hits[:1]   # one per page: the first thing its title names
    return out


def _head(page: Path, first: bool = False) -> str:
    """A page's title / description / name lines (else its first line, with first=True)."""
    lines = page.read_text(errors="replace").splitlines()
    head = "\n".join(x for x in lines[:12] if re.match(r"\s*(title|description|name):", x))
    return head or (lines[0] if first and lines else "")


def pairs(m, flagged: list[dict]) -> list[dict]:
    """Each superseded page with the newest later page in its folder whose title
    or description names the same thing first, and a version (a later state of the
    same thing); the question asks for the entry the old page is behind on."""
    from deltos.pages import _date
    r, pg = m.recall(), m.pages()
    seen: dict[Path, list] = {}   # folder -> [(date, ref, things its head names)] of pages whose head names a version
    out = []
    for x in flagged:
        old_path, f = pg.resolve(x["page"])
        if f.path not in seen:
            seen[f.path] = [(_date(p), f.prefix + p.name, r.entities(h)) for p in sorted(f.path.glob("*.md"))
                            if not p.stem.split("-")[0].isupper() and VER.search(h := _head(p))]
        group = next(g for g in r.entities(x["head"]) if g[0] == x["thing"])
        new = [(d, ref) for d, ref, groups in seen[f.path] if group in groups[:1] and d > _date(old_path)]
        if new:
            out.append({"old": x["page"], "new": max(new)[1], "q": f"What is the {x['label']} of {x['thing']}?"})
    return out


# -- one side (worker process) -------------------------------------------------------

def work(code: Path, task: dict) -> dict:
    sys.path.insert(0, str(code))
    import inspect

    import deltos.pages as P
    from bench.gate import pages_status, status, words
    from deltos import Memory
    carries = "previous" in inspect.signature(Memory.context).parameters
    seen = []
    top = P.Pages.top

    def spy(self, question, n=3, *a, **kw):
        seen.append((self, question, a, kw))
        return top(self, question, n, *a, **kw)
    P.Pages.top = spy

    def block(m, q, prev=""):
        seen.clear()
        return m.context(q, previous=prev) if prev and carries else m.context(q)

    def ranking(m, q, n, prev=""):
        b = block(m, q, prev)
        if not seen:
            return b, []
        pg, question, a, kw = seen[-1]
        return b, [ref for ref, _, _ in top(pg, question, n, *a, **kw)]

    refs = lambda b: [x.group(1) for x in LABEL.finditer(b.split("\nPages (", 1)[1])] if "\nPages (" in b else []
    m, snap = memory(Path(task["snap"]))
    res = {"carries": carries}
    res["family"] = {q: ranking(m, q, DEEP)[1] for q in task["family"]}
    res["followup_probe"] = [{"no": refs(block(m, b)), "carry": refs(block(m, b, a)) if carries else None}
                             for a, b in task["probe_pairs"]]
    res["opened"] = [refs(block(m, q)) for q in task["opened"]]
    res["followups"] = [{"no": block(m, b), "carry": block(m, b, a) if carries else None} for a, b in task["followups"]]
    profiles = lambda b: sorted(set(PROFILE.findall(b)))
    res["carried"] = [{"no": profiles(block(m, b)), "carry": profiles(block(m, b, a))}
                      for a, b in task["carried"]] if carries else []
    res["pairs"] = {q: ranking(m, q, PAIR_DEEP)[1] for q in task["pair_questions"]}
    live = [json.loads(x) for x in open(task["questions"])]
    res["stale"] = []
    for q in live:
        b = block(m, q["question"])
        first = LABEL.search(b.split("\nPages (", 1)[1]) if "\nPages (" in b else None
        res["stale"].append(first.group(2) if first else None)
    res["day"] = snap["day"]
    g = gate_memory(Path(task["gate_live"]))
    g.context("warm up")
    ms = {q["id"]: [] for q in live}
    res["live"] = {}
    for k in range(ROUNDS):
        for q in live:
            t = time.perf_counter()
            b = g.context(q["question"])
            ms[q["id"]].append(1000 * (time.perf_counter() - t))
            if k == 0 and q["gold"][0] != "unknown":
                res["live"][q["id"]] = {"s": status(b, words(q)), "p": pages_status(b, words(q)), "c": len(b)}
    res["ms"] = {k: min(v) for k, v in ms.items()}
    return res


def side(code: Path, task: dict) -> dict:
    """One checkout's results, from its own process (its deltos first on the path)."""
    tf = Path(task["out"]) / f"task-{os.getpid()}.json"
    tf.write_text(json.dumps(task))
    out = subprocess.run([sys.executable, __file__, "--worker", str(code.resolve()), "--task", str(tf)],
                         capture_output=True, text=True, cwd=code)
    if out.returncode:
        sys.exit(f"{code}: {out.stderr[-3000:]}")
    return json.loads(out.stdout.strip().splitlines()[-1])


# -- the report --------------------------------------------------------------------

def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else 0.0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snap", default="", help="the private snapshot dir (required)")
    ap.add_argument("--take", action="store_true", help="(re)take the snapshot from the configured store")
    ap.add_argument("--root", default=None, help="the store to snapshot (default $DELTOS_ROOT or ~/deltos)")
    ap.add_argument("--before", default="", help="the checkout to compare with (main)")
    ap.add_argument("--probes", default="", help="private probe questions (json)")
    ap.add_argument("--data", default=".", help="the checkout whose out/ holds the frozen gate inputs")
    ap.add_argument("--since", default="2026-10-01")
    ap.add_argument("--out", default="out/pagecheck")
    ap.add_argument("--worker", default="")
    ap.add_argument("--task", default="")
    a = ap.parse_args()
    if a.worker:   # bench/ itself off the path: only the checkout's packages
        sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != Path(__file__).resolve().parent]
        print(json.dumps(work(Path(a.worker), json.loads(Path(a.task).read_text()))))
        return
    if not a.snap:
        ap.error("--snap is required")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    snap = Path(a.snap).expanduser().resolve()
    if a.take:
        take(snap, a.root)
        return
    from deltos.memory import TOOLS
    from deltos.pages import PageError
    m, s = memory(snap)
    r, pg = m.recall(), m.pages()
    probes = json.loads(Path(a.probes).read_text()) if a.probes else {"family": [], "target": "", "followups": []}
    target = re.compile(probes["target"]) if probes.get("target") else None

    def canon(ref: str) -> str | None:
        try:
            return pg.ref(pg.resolve(ref)[0])
        except PageError:
            return None
    right = lambda ref: bool(target) and any(target.search(x) for x in pg.read(ref, 10 ** 7).splitlines())
    mine = r.profile_of.get(s["owner"].lower()) if s["owner"] else None
    things = lambda q: [g for g in r.entities(q) + r.described(q) if not mine or r.profile_of.get(g[0].lower()) != mine]
    stem = lambda name: (r.profile_of.get(name.lower()) or "").removeprefix("notes/").removesuffix(".md")

    # the chats
    db = Path(s["chats"]) / "state.db" if s["chats"] else None
    chats = turns(db, a.since) if db and db.is_file() else []
    reads = {t["name"] for t in TOOLS if t["name"] in ("memory_recall", "memory_show", "page_search", "page_read")}
    ok_len = lambda q: 0 < len(q) <= MAX_CHARS
    opened_q, opened_ref = [], []
    for t in chats:
        o = opened(t, reads)
        if o and o[0] == "page" and ok_len(t["q"]) and canon(o[1]):
            opened_q.append(t["q"])
            opened_ref.append(canon(o[1]))
    fups, carried = [], []
    # what the agent read of a thing in a turn: memory_show, or page_read of its note
    read_of = lambda t: {stem(x.get("thing", "")) if name == "memory_show" else
                         str(x.get("ref", "")).removeprefix("facts/").removesuffix(".md")
                         for name, x in t["calls"] if name in ("memory_show", "page_read")} - {""}
    for t1, t2 in zip(chats, chats[1:]):
        if t1["sid"] != t2["sid"] or not ok_len(t1["q"]) or not ok_len(t2["q"]):
            continue
        if len(t2["q"].split()) > 6 and things(t2["q"]):
            continue
        carried.append({"a": t1["q"], "b": t2["q"], "read": sorted(read_of(t1) | read_of(t2)),
                        "names": bool(things(t2["q"]))})
        o = opened(t2, reads)
        want, why = None, "opened"
        if o and o[0] == "page" and canon(o[1]):
            want = ("page", canon(o[1]))
        elif o and o[0] == "thing" and stem(o[1]):
            want = ("thing", stem(o[1]))
        elif things(t1["q"]) and stem(things(t1["q"])[0][0]):
            want, why = ("thing", stem(things(t1["q"])[0][0])), "turn 1"
        if want:
            fups.append({"a": t1["q"], "b": t2["q"], "want": want, "from": why})
    flagged = superseded(m)
    prs = pairs(m, flagged)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    task = {"snap": str(snap), "family": probes.get("family", []), "probe_pairs": probes.get("followups", []),
            "opened": opened_q, "followups": [(f["a"], f["b"]) for f in fups],
            "carried": [(c["a"], c["b"]) for c in carried],
            "pair_questions": sorted({p["q"] for p in prs}),
            "questions": str(Path(a.data) / "out/real/questions-night10.jsonl"),
            "gate_live": str(Path(a.data) / "out/gate/live"), "out": str(out.resolve())}
    sides = {"after": Path(".")} | ({"before": Path(a.before).expanduser()} if a.before else {})
    res = {k: side(v, task) for k, v in sides.items()}
    order = [k for k in ("before", "after") if k in res]
    rep = {"snapshot_day": s["day"]}
    say = []

    # a) the family
    for k in order:
        ranks = [next((i + 1 for i, ref in enumerate(res[k]["family"][q]) if right(ref)), None) for q in task["family"]]
        rep[f"family_{k}"] = ranks
        say.append(f"a) family {k:6}: first right page at {['#%d' % x if x else '-' for x in ranks]}  "
                   f"#1 on {sum(1 for x in ranks if x == 1)}/{len(ranks)}")
    # b) the page the agent opened
    for k in order:
        ok = sum(1 for ref, got in zip(opened_ref, res[k]["opened"]) if ref in got)
        rep[f"opened_{k}"] = [ok, len(opened_ref)]
        say.append(f"b) opened {k:6}: {ok}/{len(opened_ref)} turns whose first memory read was page_read had "
                   "that page in the block")
    # c) stale first, superseded pairs
    for k in order:
        dates = [d for d in res[k]["stale"] if d]
        old = sum(1 for d in dates if (datetime.date.fromisoformat(res[k]["day"]) - datetime.date.fromisoformat(d)).days > STALE_DAYS)
        rep[f"stale_{k}"] = [old, len(dates)]
        ok = both = 0
        for p in prs:
            rk = res[k]["pairs"][p["q"]]
            new, old_ = (rk.index(p["new"]) if p["new"] in rk else None), (rk.index(p["old"]) if p["old"] in rk else None)
            if new is None and old_ is None:
                continue
            both += 1
            ok += old_ is None or (new is not None and new < old_)
        rep[f"pairs_{k}"] = [ok, both, len(prs)]
        say.append(f"c) stale  {k:6}: {old}/{len(dates)} blocks led by a page over {STALE_DAYS} days old; newer page above "
                   f"the superseded one in {ok}/{both} pairs ranked ({len(prs)} pairs from {len(flagged)} flagged pages)")
    # d) follow-ups
    def has(b: str, want) -> bool:
        if want[0] == "page":
            return any(x.group(1) == want[1] for x in LABEL.finditer(b.split("\nPages (", 1)[1])) if "\nPages (" in b else False
        return re.search(rf"^Profile entries \[{re.escape(want[1])}[ \]]", b, re.M) is not None
    no = [has(x["no"], f["want"]) for x, f in zip(res["after"]["followups"], fups)]
    lines = [f"d) follow-up: {len(fups)} pairs (turn 2 <= 6 words or naming nothing)"]
    for k in order:
        n = sum(has(x["no"], f["want"]) for x, f in zip(res[k]["followups"], fups))
        lines.append(f"   {k:6} no carry: {n}/{len(fups)}")
    if res["after"]["carries"]:
        yes = [has(x["carry"], f["want"]) for x, f in zip(res["after"]["followups"], fups)]
        lost = [f for f, y, n in zip(fups, yes, no) if n and not y]
        lines.append(f"   after  carry:    {sum(yes)}/{len(fups)}  (gained {sum(1 for y, n in zip(yes, no) if y and not n)}, "
                     f"lost {len(lost)})")
        # what the agent opened in turn 2 is the stronger test: turn 1's things come
        # with carry by construction
        own = [i for i, f in enumerate(fups) if f["from"] == "opened"]
        lines.append(f"   of which the agent's own read in turn 2: {len(own)} pairs, no carry "
                     f"{sum(no[i] for i in own)}, carry {sum(yes[i] for i in own)}")
        rep["followup"] = {"pairs": len(fups), "no_carry": sum(no), "carry": sum(yes), "lost": len(lost),
                           "opened_pairs": len(own), "opened_no_carry": sum(no[i] for i in own),
                           "opened_carry": sum(yes[i] for i in own)}
        for k in order:
            if not res[k]["carries"]:
                continue
            gain = [(c, sorted(set(x["carry"]) - set(x["no"]))) for c, x in zip(carried, res[k]["carried"])]
            gain = [(c, g) for c, g in gain if g]
            wrong = [(c, g) for c, g in gain if set(g) - set(c["read"])]
            lines.append(f"   {k:6} counter: of {len(carried)} carried turns, {len(gain)} gained a profile, "
                         f"{len(wrong)} one the agent read in neither turn ({sum(1 for c, _ in wrong if c['names'])} "
                         "of them name their own thing)")
            rep[f"carried_{k}"] = {"turns": len(carried), "gained": len(gain), "unread": len(wrong),
                                   "unread_naming": sum(1 for c, _ in wrong if c["names"])}
        for p, x in zip(task["probe_pairs"], res["after"]["followup_probe"]):
            first = [right(ref) for ref in x["carry"][:1]]
            lines.append(f"   probe pair: right page first with carry: {bool(first and first[0])}; "
                         f"no carry: {bool(x['no'] and right(x['no'][0]))}")
            rep.setdefault("probe_pairs", []).append({"carry_first": bool(first and first[0])})
    say += lines
    # e) latency, and the live suite as graded at both
    for k in order:
        ms = list(res[k]["ms"].values())
        rep[f"ms_{k}"] = [round(pct(ms, .5), 1), round(pct(ms, .95), 1)]
        say.append(f"e) latency {k:6}: p50 {pct(ms, .5):.0f} ms  p95 {pct(ms, .95):.0f} ms  over {len(ms)} questions")
    if "before" in res:
        sys.path.insert(0, ".")
        from bench.gate import PAGE_RANK, RANK
        b, f = res["before"]["live"], res["after"]["live"]
        reg = [q for q in f if RANK[f[q]["s"]] < RANK[b[q]["s"]] or (
            f[q]["s"] == b[q]["s"] != "ok" and PAGE_RANK[f[q]["p"]] < PAGE_RANK[b[q]["p"]])]
        fix = [q for q in f if RANK[f[q]["s"]] > RANK[b[q]["s"]] or (
            f[q]["s"] == b[q]["s"] != "ok" and PAGE_RANK[f[q]["p"]] > PAGE_RANK[b[q]["p"]])]
        cs = {k: sum(v["c"] for v in res[k]["live"].values()) / max(1, len(res[k]["live"])) for k in order}
        rep["live_preview"] = {"regressed": reg, "fixed": fix, "chars": cs}
        say.append(f"   live suite preview: {len(fix)} fixed, {len(reg)} regressed {reg}; chars/q "
                   f"{cs['before']:.0f} -> {cs['after']:.0f}")
    print("\n".join(say))
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    (out / f"report-{stamp}.json").write_text(json.dumps(rep, indent=1) + "\n")
    (out / f"cases-{stamp}.json").write_text(json.dumps(
        {"followups": [f | {"no": x["no"], "carry": x["carry"]} for f, x in zip(fups, res["after"]["followups"])],
         "carried": [c | x for c, x in zip(carried, res["after"]["carried"])],
         "pairs": prs, "opened": list(zip(opened_q, opened_ref))}, indent=1) + "\n")


if __name__ == "__main__":
    main()
