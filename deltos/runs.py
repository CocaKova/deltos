"""Staged runs: no model-made write reaches live memory unjudged.

A run (the import of changed notes, a sweep of finished chats) works on a copy
of the store. Its numbers are judged against the runs this store accepted
(deltos/health.py). Then it is either promoted, by applying its logged output
again onto live memory under the store lock, so a person's or an agent's write
made while it ran stands by the trust order; or held with its numbers, and live
memory never sees it. One command undoes one promoted run from its journal, with
or without git, and never undoes an entry a person or an agent changed since.
10-17 and 10-19: two of three brain nights were reverted by hand with git, and
both reverts took the agent's own lines out with the import's. Drilled 10-19 on
clones of a store's history, no model: that night's 20 pieces are held (loops
10, cut-offs 11, lost 7, output 294.5 lines a piece against 66) and memory is not
touched; the daytime run's 21 pieces promote in 7 s (memory locked 6.7 s) and
undo leaves notes/ byte for byte as before; the night promoted by hand, then
undone, changes 0 entries a person or agent wrote (the hand revert changed 13).

  <root>/.runs/<id>/   stage/ (the copy the run worked on), base/ (its notes as staged),
                       before/ and after/ (the files promote changed), journal.json, report.txt
  <root>/runs.jsonl    one row per decision: empty, failed, held, promoted, discarded, undone
  <root>/import.hold   a held or undone note run (learn.hold: chats); while it is there,
                       that kind does not run. Its first line says why.

  execute(root, kind, work, label=...)   stage, work(stage), judge, promote or hold
  promote / discard / undo (root, id)    by hand: deltos run promote|discard|undo <id>
  status(root)                           the last run per kind, holds, half-promoted runs
"""
from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

try:
    import fcntl
except ImportError:   # no advisory locks on this platform: one run at a time is on the caller
    fcntl = None

from . import health
from . import items as _items
from .sources import notes
from .store import Store, parse_thing, render_history, render_thing

RUNS = ".runs"
LEDGER = "runs.jsonl"
HOLDS = {"note": "import.hold", "chat": "learn.hold"}
LOGS = ("migrate-log.jsonl", "migrate-errors.jsonl", "migrate-texts.jsonl")
# The top-level files a run reads or writes beside notes/: copied into the stage, imaged before promote.
TOP = LOGS + ("items.json", "items-said.json", "deltos.toml", "MEMORY.md", "forget-log.jsonl")
COMMITTED = ("notes", "MEMORY.md", *LOGS, "items.json", "items-said.json", LEDGER)
GC_KEEP = 14   # promoted runs that keep their before/after images (two weeks of nightly runs): undo needs them
TEMP = shutil.ignore_patterns(".*.tmp*")   # a writer's temp file (store.Store._write)
# A thing's fields besides its entries; an undo puts each back the way it does an entry.
FIELDS = ("name", "aliases", "kind", "created", "updated", "volatile", "about", "extra", "tail")


class RunError(Exception):
    """A run command that can't apply to this run (the message says why)."""


# ---- small helpers -------------------------------------------------------------------

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _lines(p: Path) -> list[str]:
    try:
        return p.read_text().splitlines(keepends=True)
    except FileNotFoundError:
        return []


def _bytes(p: Path) -> bytes | None:
    try:
        return p.read_bytes()
    except (FileNotFoundError, IsADirectoryError):
        return None


def _sha(p: Path) -> str | None:
    b = _bytes(p)
    return None if b is None else hashlib.sha1(b).hexdigest()


def _put(p: Path, data: bytes | str | None) -> None:
    """Write via a temp file and rename (a kill mid-write leaves the old file); None deletes."""
    if data is None:
        p.unlink(missing_ok=True)
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp{os.getpid()}")
    tmp.write_bytes(data.encode() if isinstance(data, str) else data)
    os.replace(tmp, p)


def _first(p: Path) -> str:
    try:
        return next(iter(p.read_text().splitlines()), "")
    except FileNotFoundError:
        return ""


def _names(hold: Path, rid: str) -> bool:
    """Does this hold file's first line name the run (not a run whose id only starts the same)?"""
    return bool(re.search(rf"(?<![\w-]){re.escape(rid)}(?![\w-])", _first(hold)))


def _tkey(r: dict) -> tuple:
    """A unit's text in migrate-texts.jsonl: its note, piece and (chats) the last message it holds."""
    return r.get("note"), r.get("chunk", 0), r.get("upto")


def _files(d: Path) -> set[str]:
    """Every file under d, as a relative path (a writer's temp files left out)."""
    out = set()
    for top, _, names in os.walk(d):
        rel = os.path.relpath(top, d).replace(os.sep, "/")
        out.update(n if rel == "." else f"{rel}/{n}" for n in names if not fnmatch.fnmatch(n, ".*.tmp*"))
    return out


def _profile(rel: str) -> str | None:
    """The stem of the thing a notes/ file belongs to (its profile or its cold history); None for
    the append-only files (daily logs, set-aside lines) and anything else."""
    parts = rel.split("/")
    if len(parts) != 2 or parts[0] != "notes" or not parts[1].endswith(".md"):
        return None
    stem = parts[1][:-3]
    if stem.startswith("log-") or stem.endswith("-extra"):
        return None
    return stem.removesuffix("-history")


def _appended(rel: str) -> bool:
    """Files a run only appends to: the logs, a day's log, a thing's set-aside lines."""
    name = rel.rsplit("/", 1)[-1]
    return name.endswith(".jsonl") or (rel.startswith("notes/") and (name.startswith("log-") or name.endswith("-extra.md")))


# ---- git: a checkpoint before, one commit per decision; none at all without .git ------------

def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def _has_git(root: Path) -> bool:
    return (root / ".git").exists()


def _exclude(root: Path) -> None:
    """Run folders stay out of the store's history: .runs/ in .git/info/exclude, never a tracked file."""
    if not _has_git(root):
        return
    p = Path(_git(root, "rev-parse", "--git-path", "info/exclude").stdout.strip() or ".git/info/exclude")
    p = p if p.is_absolute() else root / p
    if f"{RUNS}/" not in {x.strip() for x in _lines(p)}:
        p.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(_lines(p))
        p.write_text(text + ("" if not text or text.endswith("\n") else "\n") + f"{RUNS}/\n")


def _commit(root: Path, msg: str, paths: list[str] | None = None) -> str:
    """Commit what changed (paths: only those), as deltos; the short hash, or "" when there was
    nothing to commit or no git."""
    if not _has_git(root):
        return ""
    if paths is None:
        _git(root, "add", "-A")
    else:
        tracked = _git(root, "ls-files", "--", *paths).stdout.splitlines()
        keep = [p for p in dict.fromkeys(paths)
                if (root / p).exists() or any(t == p or t.startswith(p + "/") for t in tracked)]
        if keep:
            _git(root, "-c", "advice.addIgnoredFile=false", "add", "-A", "--", *keep)
    if not _git(root, "status", "--porcelain").stdout.strip():
        return ""
    if _git(root, "-c", "user.name=deltos", "-c", "user.email=deltos@local", "commit", "-q", "-m", msg).returncode:
        return ""   # nothing staged (only untracked files outside paths)
    return _git(root, "rev-parse", "--short", "HEAD").stdout.strip()


# ---- the ledger and the journal --------------------------------------------------------------

def _ledger(root: Path, row: dict) -> None:
    with open(root / LEDGER, "a") as fh:
        fh.write(json.dumps({"at": _now(), **row}, default=str) + "\n")


def ledger(root: Path | str) -> list[dict]:
    out = []
    for ln in _lines(Path(root) / LEDGER):
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


def _dir(root: Path, rid: str) -> Path:
    d = root / RUNS / rid
    if not (d / "journal.json").is_file() or "/" in rid or rid.startswith("."):
        raise RunError(f"no run {rid!r} (deltos runs lists them)")
    return d


def journal(root: Path | str, rid: str) -> dict:
    return json.loads((_dir(Path(root), rid) / "journal.json").read_text())


def _save(d: Path, j: dict) -> None:
    _put(d / "journal.json", json.dumps(j, indent=1, default=str))


def _runs(root: Path) -> list[tuple[str, dict]]:
    out = []
    for p in sorted((root / RUNS).glob("*/journal.json")):
        try:
            out.append((p.parent.name, json.loads(p.read_text())))
        except ValueError:
            pass
    return out


def promoting(root: Path | str) -> list[str]:
    """Runs whose promote stopped part way (a crash, a kill): live memory holds part of them."""
    return [rid for rid, j in _runs(Path(root)) if j.get("status") == "promoting"]


def _new_id(root: Path, label: str) -> str:
    base = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + label
    used = {r.get("id") for r in ledger(root)}
    rid, i = base, 2
    while (root / RUNS / rid).exists() or rid in used:
        rid, i = f"{base}-{i}", i + 1
    return rid


@contextmanager
def run_lock(root: Path | str):
    """One run at a time per store (import and learn never overlap, so model requests never
    exceed one run's workers). Yields False when another run holds it."""
    d = Path(root) / RUNS
    d.mkdir(parents=True, exist_ok=True)
    fh = open(d / ".lock", "a")
    try:
        if fcntl:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
        yield True
    finally:
        fh.close()   # closing the file releases the lock


def _new(stage: Path, counts: dict) -> tuple[list[dict], list[dict], dict, dict]:
    """What the work added to the stage's logs: (rows, error rows, texts by unit, the raw lines)."""
    raw = {f: _lines(stage / f)[counts.get(f, 0):] for f in LOGS}
    rows = [json.loads(x) for x in raw["migrate-log.jsonl"] if x.strip()]
    errors = [json.loads(x) for x in raw["migrate-errors.jsonl"] if x.strip()]
    texts = {}
    for x in raw["migrate-texts.jsonl"]:
        if x.strip():
            t = json.loads(x)
            texts[_tkey(t)] = t.get("text") or ""
    return rows, errors, texts, raw


def _snapshot(root: Path, dest: Path, top: bool = True) -> None:
    shutil.copytree(root / "notes", dest / "notes", ignore=TEMP)
    for f in TOP if top else ():
        if (root / f).is_file():
            shutil.copy2(root / f, dest / f)


# ---- a run ---------------------------------------------------------------------------------

def execute(root: Path | str, kind: str, work, *, label: str, say=print, owner: str = "",
            fingerprint: str = "") -> int:
    """Stage a copy of the store, let work(stage) write into it, judge what it wrote, then
    promote it onto live memory or hold it. 0: promoted, empty or already held; 3: held now;
    1: failed or busy (live memory untouched)."""
    root = Path(root)
    _exclude(root)
    with run_lock(root) as free:
        if not free:
            say("another run in progress")
            return 1
        hold = root / HOLDS[kind]
        if hold.exists():
            say(f"{label} held: {_first(hold)}")
            return 0
        for rid in promoting(root):
            # Memory holds part of that run; a new run staged on top of it would be undone with it.
            say(f"run {rid} was left half-promoted: `deltos run undo {rid}` first (this run did not start)")
            return 1
        rid = _new_id(root, label)
        d = root / RUNS / rid
        d.mkdir(parents=True)
        try:
            with Store(root)._locked():
                # Writes made since the last commit (an agent's, a person's) get their own commit.
                _commit(root, f"writes before {label} run {rid}")
                _snapshot(root, d / "stage")
                _snapshot(root, d / "base", top=False)
                counts = {f: len(_lines(root / f)) for f in LOGS}
        except BaseException:
            shutil.rmtree(d, ignore_errors=True)
            raise
        j = {"id": rid, "kind": kind, "label": label, "status": "staged", "staged": _now(), "owner": owner,
             "fingerprint": fingerprint, "counts": counts}
        _save(d, j)
        try:
            work(d / "stage")
            rows, errors, _, _ = _new(d / "stage", counts)
            if not rows and not errors:
                shutil.rmtree(d)
                _ledger(root, {"id": rid, "kind": kind, "label": label, "decision": "empty", "fingerprint": fingerprint})
                say(f"{label} run {rid}: nothing new")
                return 0
            facts = sum(r.get("facts", 0) for r in rows)
            if not rows:
                j.update(status="failed", errors=len(errors))
                _save(d, j)
                _ledger(root, {"id": rid, "kind": kind, "label": label, "decision": "failed", "errors": len(errors),
                               "error": f"none of {len(errors)} unit(s) finished", "fingerprint": fingerprint})
                say(f"{label} run {rid} failed: none of {len(errors)} unit(s) finished (memory untouched)")
                return 1
            verdict = health.assess(rows, health.history(root, kind), health.store_delta(d / "base", d / "stage"),
                                    errors)
            report = health.render(verdict, rid, label)
            (d / "report.txt").write_text(report)
            j.update(verdict=verdict.decision, reasons=list(verdict.reasons), metrics=verdict.metrics,
                     rows=len(rows), facts=facts, errors=len(errors))
            if verdict.decision == "hold":
                _put(hold, report if report.endswith("\n") else report + "\n")
                j["status"] = "held"
                _save(d, j)
                _ledger(root, {"id": rid, "kind": kind, "label": label, "decision": "held",
                               "reasons": list(verdict.reasons), "metrics": verdict.metrics,
                               "fingerprint": fingerprint, "rows": len(rows), "facts": facts})
                say(report.rstrip("\n"))
                return 3
            _save(d, j)
            promote(root, rid, by="health", say=say)
            return 0
        except BaseException as e:
            try:
                j = journal(root, rid)
                if j.get("status") == "staged":
                    j.update(status="failed", error=repr(e)[:300])
                    _save(d, j)
            except (RunError, ValueError, OSError):
                pass
            _ledger(root, {"id": rid, "kind": kind, "label": label, "decision": "failed", "error": repr(e)[:300],
                           "fingerprint": fingerprint})
            raise


def promote(root: Path | str, rid: str, by: str = "health", say=print) -> dict:
    """Apply a staged run's logged output onto live memory, under the store lock for the whole
    step: the run's rows again in their logged order (no model; the items decisions it made are
    reused), then its log lines, then the files it carried. A person's or an agent's write made
    while the run worked stands by the trust order, which a copy of the stage would overwrite.
    A crash part way leaves the journal at 'promoting', and `run undo` takes it from there:
    no automatic rollback, which could delete writes made after the crash."""
    root = Path(root)
    d = _dir(root, rid)
    j = journal(root, rid)
    if j.get("status") not in ("staged", "held"):
        raise RunError(f"run {rid} is {j.get('status')}: only a held run can be promoted")
    stage, before, after = d / "stage", d / "before", d / "after"
    rows, _, texts, raw = _new(stage, j["counts"])
    t0 = time.time()
    m = notes.Migrator(root, owner=j.get("owner", ""))
    # Store's lock is re-entrant per Store object: every write the replay makes reuses it, and
    # another process (the agent's remember) waits seconds. No second Store may lock in here.
    with m.store._locked():
        t1 = time.time()
        _commit(root, f"writes during {j['label']} run {rid}")
        for x in (before, after):
            shutil.rmtree(x, ignore_errors=True)
        _snapshot(root, before)
        j.update(status="promoting", started=_now(), by=by)
        _save(d, j)
        if (stage / "items.json").exists():
            # The run's "another one?" decisions, cached in the stage: reused, nothing is asked again.
            m.items = _items.Items(_items.load(stage / "items.json"), asker=None)
            m._said = notes.load_said(root / "items-said.json")
        failed = []
        # One index write for the whole replay instead of one per thing saved: nobody can read it
        # while the lock is held, and it is written from the notes below. (10-19, the 21 pieces of
        # the daytime import: 1,897 rewrites of MEMORY.md, ~1.5 s of an 11 s promote.)
        m.store._write_index = lambda: None
        try:
            for r in rows:
                m.chat = r.get("kind") == "chat"
                try:
                    m.session(r["note"], r["day"], texts.get(_tkey(r), ""), r.get("created") or r["day"],
                              git_mode=not m.chat, replay=r)
                except Exception as e:
                    failed.append(f"{r.get('note')}#{r.get('chunk', 0)}: {e!r}"[:300])
        finally:
            del m.store._write_index
        for f in LOGS:   # the rows exactly as logged, not the replay's counts
            if raw[f]:
                old = _bytes(root / f) or b""
                with open(root / f, "a") as fh:
                    fh.write(("\n" if old and not old.endswith(b"\n") else "") + "".join(raw[f]))
        if m.items is not None:
            cache = _items.load(root / "items.json")
            cache.update(m.items.cache)
            _items.save(root / "items.json", cache)
            notes.save_said(root / "items-said.json", m._said, m.store)
        j["carried"] = sorted(_files(stage / ".carry"))   # named first, so a crash while moving them can be undone
        _save(d, j)
        carried = _carry(stage / ".carry", root, before)
        # The index from the notes as they are now (every save wrote it; this one is sure), before
        # the after-images are taken so they hold it.
        m.store._index()
        m.store._write_index()
        changed = _changed(root, before, carried)
        keep = _images(root, after, changed)
        # 'promoted' only once the after-images are whole: a crash before this leaves 'promoting'
        # and a whole before/, which is what the crash path of undo needs.
        j.update(status="promoted", promoted=_now(), changed=changed, carried=carried, replay_exceptions=failed)
        _save(d, j)
        for p in _files(before) - keep:
            (before / p).unlink()
        commit = _commit(root, f"{j['label']} run {rid}: " + notes.summary(rows), [*COMMITTED, *carried])
        t2 = time.time()
    j.update(commit=commit, seconds=round(time.time() - t0, 2), lock_seconds=round(t2 - t1, 2))
    _save(d, j)
    _ledger(root, {"id": rid, "kind": j["kind"], "label": j["label"], "decision": "promoted", "by": by,
                   "metrics": j.get("metrics", {}), "fingerprint": j.get("fingerprint", ""), "commit": commit,
                   "changed": len(changed), "rows": len(rows), "facts": sum(r.get("facts", 0) for r in rows),
                   "replay_exceptions": len(failed)})
    hold = root / HOLDS[j["kind"]]
    if _names(hold, rid):
        hold.unlink()
    for x in (stage, d / "base"):
        shutil.rmtree(x, ignore_errors=True)
    say(f"promoted {j['label']} run {rid}: {len(rows)} unit(s), {len(changed)} file(s) changed"
        + (f", {len(failed)} replay exception(s)" if failed else "") + (f", commit {commit}" if commit else "")
        + f" ({j['seconds']}s, memory locked {j['lock_seconds']}s)")
    gc(root)
    return j


def _carry(src: Path, root: Path, before: Path) -> list[str]:
    """Files the work left under stage/.carry/ (state that may only advance when the run is
    promoted, e.g. a folder snapshot): moved onto live memory at the same paths, their old
    copies kept as before-images."""
    out = []
    for rel in sorted(_files(src)):
        dst = root / rel
        if dst.exists() and not (before / rel).exists():
            (before / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, before / rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src / rel), str(dst))
        out.append(rel)
    return out


def _changed(root: Path, before: Path, carried: list[str]) -> dict:
    """{path: {before: sha1 | None, after: sha1 | None}} for every file promote changed."""
    paths = _files(before) | {f"notes/{p}" for p in _files(root / "notes")} | set(carried)
    paths |= {f for f in TOP if (root / f).is_file()}
    out = {}
    for p in sorted(paths):
        try:   # before/ was copied with its mtimes: same size and mtime is the same file
            sb, sa = (before / p).stat(), (root / p).stat()
            if sb.st_size == sa.st_size and sb.st_mtime_ns == sa.st_mtime_ns:
                continue
        except FileNotFoundError:
            pass
        b, a = _sha(before / p), _sha(root / p)
        if a != b:
            out[p] = {"before": b, "after": a}
    return out


def _images(root: Path, after: Path, changed: dict) -> set[str]:
    """after/ gets the changed files as promote left them; the paths before/ keeps. A thing's
    profile and its cold history travel together, so an undo can read the whole thing."""
    keep = set(changed)
    for p in changed:
        stem = _profile(p)
        if stem:
            keep |= {f"notes/{stem}.md", f"notes/{stem}-history.md"}
    for p in keep:
        if (root / p).is_file():
            (after / p).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / p, after / p)
    return keep


def discard(root: Path | str, rid: str, say=print) -> None:
    """Drop a held (or failed) run: its folder goes, and its units import again on the next run."""
    root = Path(root)
    d = _dir(root, rid)
    j = journal(root, rid)
    if j.get("status") in ("promoting", "promoted", "undone"):
        raise RunError(f"run {rid} is {j['status']}: only a held run is discarded (`deltos run undo` undoes a promoted one)")
    shutil.rmtree(d)
    hold = root / HOLDS[j["kind"]]
    if _names(hold, rid):
        hold.unlink()
    _ledger(root, {"id": rid, "kind": j["kind"], "label": j["label"], "decision": "discarded"})
    say(f"discarded {j['label']} run {rid}: its units import again on the next run")


# ---- undo ----------------------------------------------------------------------------------

def undo(root: Path | str, rid: str, hold: bool = True, say=print) -> dict:
    """Take one promoted run out of live memory, from its journal, no git needed. A file nobody
    touched since is restored byte for byte; a thing written since gets each entry back as it
    was before the run unless someone changed that entry after the run (kept, and listed); the
    logs lose exactly the lines the run added. A half-promoted run (crashed) has no after-images:
    only entries an import wrote (no source) that differ from before the run go back.
    hold: write the kind's hold file, so the undone units don't come straight back."""
    root = Path(root)
    d = _dir(root, rid)
    j = journal(root, rid)
    crash = j.get("status") == "promoting"
    if not crash and j.get("status") != "promoted":
        raise RunError(f"run {rid} is {j.get('status')}: only a promoted run can be undone")
    if not (d / "before").is_dir():
        raise RunError(f"run {rid} has no before-images any more (kept for the last {GC_KEEP} runs)")
    kept, skipped = [], []
    store = Store(root)
    with store._locked():
        _commit(root, f"writes before undo of {j['label']} run {rid}")
        if crash:
            exact = _undo_crash(root, d, j, kept, skipped)
        else:
            exact = _undo_changed(root, d, j["changed"], kept, skipped)
        mem = j.get("changed", {}).get("MEMORY.md")
        if exact and mem and _sha(root / "MEMORY.md") == mem["after"]:
            _put(root / "MEMORY.md", _bytes(d / "before" / "MEMORY.md"))   # nothing else moved: as it was
        else:
            store._index()
            store._write_index()
        j.update(status="undone", undone=_now(), kept=kept, skipped=skipped, crash=crash)
        _save(d, j)
        if hold:
            _put(root / HOLDS[j["kind"]], f"undone {j['label']} run {rid}; delete this file to import those units again\n")
        commit = _commit(root, f"undo {j['label']} run {rid}",
                         [*COMMITTED, *j.get("carried", []), *j.get("changed", {})])
    _ledger(root, {"id": rid, "kind": j["kind"], "label": j["label"], "decision": "undone", "kept": kept,
                   "skipped": skipped, "commit": commit, "crash": crash})
    say(f"undid {j['label']} run {rid}" + (" (half-promoted)" if crash else "")
        + (f"; kept {len(kept)} change(s) made after it" if kept else "")
        + (f"; left {len(skipped)} file(s) as they are" if skipped else "")
        + (f"; commit {commit}" if commit else "") + ("" if not hold else f"; {HOLDS[j['kind']]} written"))
    for x in kept + skipped:
        say(f"  {x}")
    return j


def _undo_changed(root: Path, d: Path, changed: dict, kept: list, skipped: list) -> bool:
    """The normal path; True when every file went back byte for byte."""
    exact, stems = True, set()
    for p, sh in changed.items():
        if p == "MEMORY.md":
            continue   # rebuilt (or restored) at the end
        stem = _profile(p)
        if stem:
            stems.add(stem)
        elif _appended(p):
            exact &= _unappend(root, d, p, kept)
        elif _sha(root / p) == sh["after"]:
            _put(root / p, _bytes(d / "before" / p))
        else:
            kept.append(f"{p}: changed after the run, left as is")
            exact = False
    for stem in sorted(stems):
        exact &= _undo_thing(root, d, stem, kept, skipped)
    return exact


def _versions(stem: str, *dirs: Path) -> list[tuple[bytes | None, bytes | None]]:
    return [(_bytes(x / f"notes/{stem}.md"), _bytes(x / f"notes/{stem}-history.md")) for x in dirs]


def _thing(stem: str, v: tuple, skipped: list):
    """The parsed thing, or None when absent; False when it doesn't render back to the same
    bytes (an undo would rewrite what it can't read)."""
    if v[0] is None:
        return None
    text, hist = v[0].decode(), (v[1] or b"").decode()
    t = parse_thing(text, hist)
    t.file = stem
    if render_thing(t) != text or (render_history(t) or "") != hist:
        skipped.append(f"notes/{stem}.md: left as is: does not round-trip")
        return False
    return t


def _write_thing(root: Path, stem: str, t) -> None:
    if t is None:
        for f in (f"{stem}.md", f"{stem}-history.md"):
            (root / "notes" / f).unlink(missing_ok=True)
        return
    _put(root / "notes" / f"{stem}.md", render_thing(t))
    _put(root / "notes" / f"{stem}-history.md", render_history(t))


def _undo_thing(root: Path, d: Path, stem: str, kept: list, skipped: list) -> bool:
    live, after, before = _versions(stem, root, d / "after", d / "before")
    if live == after:
        for i, f in enumerate((f"{stem}.md", f"{stem}-history.md")):
            _put(root / "notes" / f, before[i])
        return True
    # Written since the run: three ways, entry by entry. base = after the run, ours = now,
    # theirs = before the run. An entry nobody touched since goes back; one changed since stays.
    theirs, base, ours = (_thing(stem, v, skipped) for v in (before, after, live))
    if any(x is False for x in (theirs, base, ours)):
        return False
    where = f"notes/{stem}.md"
    if ours is None:
        if base is not None:
            kept.append(f"{where}: removed after the run, left removed")
            return False
        _write_thing(root, stem, theirs)
        return False
    out = copy.deepcopy(ours)
    if theirs is not None and base is not None:
        for f in FIELDS:
            if getattr(ours, f) == getattr(base, f):
                setattr(out, f, copy.deepcopy(getattr(theirs, f)))
    b_attrs, t_attrs = (base.attrs if base else {}), (theirs.attrs if theirs else {})
    attrs = {}
    for k in [*t_attrs, *(k for k in ours.attrs if k not in t_attrs)]:
        o, b, t = ours.attrs.get(k), b_attrs.get(k), t_attrs.get(k)
        if o == b:
            v = t
        else:
            v = o
            if b != t and o != t:   # the run changed this entry, and so did someone after it
                kept.append(f"{where} {k}: kept, changed after the run")
        if v is not None:
            attrs[k] = copy.deepcopy(v)
    out.attrs = attrs
    named = any(getattr(ours, f) != getattr(base, f) for f in FIELDS if f != "updated") if base else True
    if theirs is None and not attrs and named:
        kept.append(f"{where}: made by the run, named since: kept without the run's entries")
    _write_thing(root, stem, None if theirs is None and not attrs and not named else out)
    return False


def _unappend(root: Path, d: Path, rel: str, kept: list, only_notes: set | None = None) -> bool:
    """Remove from a live append-only file exactly the lines the run added (the after-image less
    the before-image, as a multiset). only_notes (the crash path, no after-image): the lines
    added since before the run that belong to this run's units. True when it is back byte for byte."""
    live = _bytes(root / rel)
    b = _bytes(d / "before" / rel)
    a = _bytes(d / "after" / rel) if only_notes is None else None
    if live is None:
        if b is not None:
            kept.append(f"{rel}: removed after the run, left removed")
        return b is None
    if only_notes is None and live == a:
        _put(root / rel, b)
        return True
    md = rel.endswith(".md")
    bt, lt = (b or b"").decode(), live.decode()
    if only_notes is None:
        at = (a or b"").decode()
        if at.startswith(bt) and lt.startswith(at) and not (md and b is None):
            new = bt + lt[len(at):]   # later lines were appended after the run's: splice it out
        else:
            drop = Counter(at.splitlines(keepends=True)) - Counter(bt.splitlines(keepends=True))
            new = "".join(_without(lt, drop, md))
    else:
        have = Counter(bt.splitlines(keepends=True))
        out = []
        for ln in lt.splitlines(keepends=True):
            if have[ln] > 0:
                have[ln] -= 1
            elif md and ln.startswith("- "):
                continue   # set-aside and log lines come only from imports and doctor
            elif not md and _note_of(ln) in only_notes:
                continue
            out.append(ln)
        new = "".join(out)
    if b is None and not any(ln.startswith("- ") if md else ln.strip() for ln in new.splitlines()):
        new = None   # the run made this file, and only its lines were in it
    _put(root / rel, new)
    return (new.encode() if new is not None else None) == b


def _without(text: str, drop: Counter, md: bool) -> list[str]:
    out = []
    for ln in text.splitlines(keepends=True):
        if drop[ln] > 0 and (not md or ln.startswith("- ")):   # a file's own header stays
            drop[ln] -= 1
            continue
        out.append(ln)
    return out


def _note_of(line: str):
    try:
        r = json.loads(line)
        return r.get("note") if isinstance(r, dict) else None
    except ValueError:
        return None


def _undo_crash(root: Path, d: Path, j: dict, kept: list, skipped: list) -> bool:
    """A promote that stopped part way: before/ is whole, after/ was never taken."""
    before = d / "before"
    rows = _new(d / "stage", j["counts"])[0] if (d / "stage").is_dir() else []
    mine = {r.get("note") for r in rows}
    paths = _files(before) | {f"notes/{p}" for p in _files(root / "notes")} | {f for f in LOGS if (root / f).is_file()}
    stems = set()
    for p in sorted(paths - set(j.get("carried", []))):
        if p == "MEMORY.md" or _bytes(root / p) == _bytes(before / p):
            continue
        stem = _profile(p)
        if stem:
            stems.add(stem)
        elif _appended(p):
            _unappend(root, d, p, kept, only_notes=mine)
        else:
            kept.append(f"{p}: changed since the run began, left as is")
    for rel in j.get("carried", []):   # files it carried before it stopped: back as they were
        _put(root / rel, _bytes(before / rel))
    for stem in sorted(stems):
        theirs, ours = (_thing(stem, v, skipped) for v in _versions(stem, before, root))
        if any(x is False for x in (theirs, ours)) or ours is None:
            continue
        out, n = copy.deepcopy(ours), 0
        for k in [*ours.attrs, *(k for k in (theirs.attrs if theirs else {}) if k not in ours.attrs)]:
            o, t = ours.attrs.get(k), (theirs.attrs.get(k) if theirs else None)
            if o == t or o is None:
                continue
            if o.source:   # a person's or an agent's entry: never touched
                kept.append(f"notes/{stem}.md {k}: kept, written by {o.source}")
                continue
            n += 1
            if t is None:
                del out.attrs[k]
            else:
                out.attrs[k] = copy.deepcopy(t)
        if theirs is None and n and not out.attrs:
            _write_thing(root, stem, None)   # made by the run, and only its entries were in it
        elif n:
            _write_thing(root, stem, out)
    return False


# ---- reading runs --------------------------------------------------------------------------

def status(root: Path | str, line: bool = False) -> str:
    """The last run of each kind, held runs, open hold files, and runs left half-promoted."""
    root = Path(root)
    last: dict[str, dict] = {}
    for r in ledger(root):
        if r.get("kind"):
            last[r["kind"]] = r
    runs = _runs(root)
    held = [(rid, j) for rid, j in runs if j.get("status") == "held"]
    holds = [(f, _first(root / f)) for f in HOLDS.values() if (root / f).exists()]
    stuck = [rid for rid, j in runs if j.get("status") == "promoting"]

    def says(r):
        return (f"{r.get('label', '')} run {r.get('id')} {r.get('decision')}"
                + (f" ({r['facts']} facts)" if "facts" in r else "") + (f" by {r['by']}" if r.get("by") == "hand" else ""))
    if line:
        parts = [says(last[k]) for k in HOLDS if k in last]
        parts += [f"HOLD {f}: {first}" for f, first in holds]
        parts += [f"HALF-PROMOTED {rid}: deltos run undo {rid}" for rid in stuck]
        return "deltos status: " + ("; ".join(parts) or "no runs yet")
    out = ["last runs:"] + [f"  {k}: {says(last[k])} at {last[k].get('at', '')}" for k in HOLDS if k in last]
    if len(out) == 1:
        out = ["no runs yet"]
    for rid, j in held:
        out.append(f"held: {j.get('label')} run {rid}: {'; '.join(j.get('reasons') or [])} "
                   f"(deltos run show|promote|discard {rid})")
    out += [f"hold file {f}: {first}" for f, first in holds]
    out += [f"half-promoted: run {rid} (a crash during promote): deltos run undo {rid}" for rid in stuck]
    return "\n".join(out)


def show(root: Path | str, rid: str) -> str:
    root = Path(root)
    d = _dir(root, rid)
    j = journal(root, rid)
    report = (d / "report.txt").read_text() if (d / "report.txt").exists() else ""
    out = [report.rstrip("\n")] if report else []
    out.append(f"run {rid}: {j.get('kind')} ({j.get('label')}), {j.get('status')}"
               + (f" by {j['by']}" if j.get("by") else ""))
    for k in ("staged", "rows", "facts", "errors", "promoted", "commit", "seconds", "lock_seconds", "undone"):
        if j.get(k) not in (None, ""):
            out.append(f"  {k}: {j[k]}")
    if j.get("changed"):
        out.append(f"  changed: {len(j['changed'])} file(s)")
    for k in ("replay_exceptions", "kept", "skipped"):
        for x in j.get(k) or []:
            out.append(f"  {k.replace('_', ' ')}: {x}")
    return "\n".join(out)


def gc(root: Path | str, keep: int = GC_KEEP) -> int:
    """Before/after images of all but the newest `keep` promoted runs go (those runs can no
    longer be undone from their journal); returns how many runs lost them."""
    root = Path(root)
    done = [rid for rid, j in _runs(root) if j.get("status") == "promoted"]
    n = 0
    for rid in done[:max(0, len(done) - keep)]:
        d = root / RUNS / rid
        if (d / "before").exists() or (d / "after").exists():
            for x in ("before", "after"):
                shutil.rmtree(d / x, ignore_errors=True)
            n += 1
    return n


# ---- drills: a logged run, applied again with no model --------------------------------------

def load_texts(path: Path | str) -> dict:
    """Unit texts by (note, chunk, upto): a migrate-texts.jsonl, or a JSON {"note#chunk": text}."""
    path = Path(path)
    if path.suffix == ".jsonl":
        out = {}
        for ln in _lines(path):
            if ln.strip():
                t = json.loads(ln)
                out[_tkey(t)] = t.get("text") or ""
        return out
    out = {}
    for k, text in json.loads(path.read_text()).items():
        note, _, chunk = k.rpartition("#")
        out[(note, int(chunk), None)] = text
    return out


def replay_work(rows: list[dict], texts: dict | None = None):
    """A work function that applies logged rows to the stage with no model, logging each row and
    its text there as a run would: drills and tests run the whole staged path on a logged night."""
    texts = texts or {}

    def work(stage: Path) -> None:
        stage = Path(stage)
        m = notes.Migrator(stage)
        if (stage / "items.json").exists():
            m.items = _items.Items(_items.load(stage / "items.json"), asker=None)
            m._said = notes.load_said(stage / "items-said.json")
        log, side = [], []
        for r in rows:
            m.chat = r.get("kind") == "chat"
            text = texts.get(_tkey(r), "")
            m.session(r["note"], r["day"], text, r.get("created") or r["day"], git_mode=not m.chat, replay=r)
            log.append(json.dumps(r) + "\n")
            if text:
                side.append(json.dumps({"note": r["note"], "chunk": r.get("chunk", 0), "text": text,
                                        **({"upto": r["upto"]} if "upto" in r else {})}) + "\n")
        for f, lines in (("migrate-log.jsonl", log), ("migrate-texts.jsonl", side)):
            if lines:
                with open(stage / f, "a") as fh:
                    fh.write("".join(lines))
        if m.items is not None:
            notes.save_said(stage / "items-said.json", m._said, m.store)
    return work


def dated(work, jobs: list[tuple]):
    """work, with the day each unit's note was written put into the rows it logs as 'created'
    (jobs: notes.run's (created, day, note, chunk, ...)), where that is not the row's day. The
    stage gave a fact with no date of its own the day its note was written; promote replays a
    row with its 'created', and without one it would use the row's day, when the note was last
    edited: a later date, which can put the note's value over a person's or an agent's that the
    judged stage kept (the trust order keeps theirs only against a note not dated after it)."""
    written = {(name, chunk): created for created, day, name, chunk, *_ in jobs if created and created != day}

    def run(stage: Path) -> None:
        log = Path(stage) / "migrate-log.jsonl"
        start = len(_lines(log))
        work(stage)
        if not written:
            return
        lines, n = _lines(log), 0
        for i in range(start, len(lines)):
            if not lines[i].strip():
                continue
            r = json.loads(lines[i])
            created = written.get((r.get("note"), r.get("chunk", 0)))
            if created and "created" not in r:
                r["created"] = created
                lines[i], n = json.dumps(r) + "\n", n + 1
        if n:
            _put(log, "".join(lines))
    return run
