"""A folder of markdown notes as an import source, with no git anywhere.

  [import] folders = ["~/notes:notes"]     (path:label; deltos import)

Most installs have one agent and, at most, a plain folder of notes: the agent's
own wiki, or the owner's. Nobody commits it, so the changes are found the way
git would show them, against a snapshot of what was imported last:

  <root>/.sources/<label>/files/<relpath>   the last imported copy of each note
  <root>/.sources/<label>/manifest.json     {relpath: sha1} of those copies

Every *.md under the folder (dot folders left out, and names with an [import]
skip part) whose sha1 differs from the manifest gives the lines that are new
against its copy (difflib's inserted and replaced lines), less blank lines and
the frontmatter lines the git source drops. The changed files of one day (each
file's mtime) are one note, like one commit: '<label>@<day>-<sha8>', its lines
packed into pieces the way the git source packs a commit's. A deleted file gives
nothing (removed lines are not facts) and its copy stays: back again, only its
new lines are read. The first run reads every file whole, oldest day first,
bounded by `deltos import --limit`.

The snapshot moves only with a promoted run: carry() writes it under the run's
stage/.carry/, which deltos/runs.py moves onto memory when the run is promoted.
A held, failed or discarded run leaves it where it was, so the next run finds
the same changes; a day's files advance only once every piece of that day's note
is in the log, so a piece cut by --limit or lost to a model error is read again.
"""
from __future__ import annotations

import datetime
import difflib
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from . import notes

SOURCES = ".sources"
# The frontmatter lines the git source (notes._git_jobs) drops from a commit's added lines, kept the same.
NOISE = re.compile(r"^\s*(updated|modified|ingested_at|date|originSessionId|node_type|---)\b")


def parse(spec: str) -> tuple[Path, str]:
    """'path:label' (a bare path is labelled with its folder's name)."""
    path, sep, label = spec.rpartition(":")
    if not sep or not label or "/" in label:
        path, label = spec, Path(spec).expanduser().name
    return Path(path).expanduser(), label


def added(old: str, new: str) -> list[str]:
    """The lines of new that are not in old, in order, as the import reads them. The common head
    and tail are cut off first, as git does: a note that only grew at its end is not matched
    line by line (a 20,000-line page with one line added: 2.4 s in difflib, 0 s cut first)."""
    a, b = old.splitlines(), new.splitlines()
    head = 0
    while head < min(len(a), len(b)) and a[head] == b[head]:
        head += 1
    tail = 0
    while tail < min(len(a), len(b)) - head and a[-1 - tail] == b[-1 - tail]:
        tail += 1
    a, b = a[head:len(a) - tail], b[head:len(b) - tail]
    out = []
    for tag, _, _, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("insert", "replace"):
            out += [x for x in b[j1:j2] if x.strip() and not NOISE.match(x)]
    return out


def _sha(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _exclude(root: Path) -> None:
    """The snapshot stays out of the store's history: .sources/ in .git/info/exclude."""
    if not (root / ".git").exists():
        return
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-path", "info/exclude"],
                             capture_output=True, text=True).stdout.strip()
    except OSError:   # no git installed: nothing to keep it out of
        return
    p = Path(out or ".git/info/exclude")
    p = p if p.is_absolute() else root / p
    text = p.read_text() if p.is_file() else ""
    if f"{SOURCES}/" not in {x.strip() for x in text.splitlines()}:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + ("" if not text or text.endswith("\n") else "\n") + f"{SOURCES}/\n")


class Folder:
    """One [import] folders entry, read once: what changed since the snapshot, as import jobs,
    and the snapshot those jobs advance it to (carry)."""

    def __init__(self, spec: str, root: Path | str, skip: list[str]):
        self.path, self.label = parse(spec)
        self.root = Path(root)
        self.snap = self.root / SOURCES / self.label
        _exclude(self.root)
        manifest = self._manifest(self.snap)
        self.batches: list[dict] = []   # a day's changed files: {day, note, files: {rel: (sha1, bytes)}, of}
        self.pieces: list[tuple] = []   # (day, day, note, i, text, of), like notes.git_jobs
        by_day: dict[str, dict] = {}
        for rel, p in self._notes(skip):
            try:
                data, mtime = p.read_bytes(), p.stat().st_mtime
            except OSError:   # gone or unreadable since the walk: the next run reads it
                continue
            sha = _sha(data)
            if manifest.get(rel) == sha:
                continue
            day = datetime.date.fromtimestamp(mtime).isoformat()
            by_day.setdefault(day, {})[rel] = (sha, data)
        for day in sorted(by_day):
            files = dict(sorted(by_day[day].items()))
            sha8 = _sha("".join(f"{rel}\t{sha}\n" for rel, (sha, _) in files.items()).encode())[:8]
            b = {"day": day, "note": f"{self.label}@{day}-{sha8}", "files": files}
            units = list(self._pack(b))
            b["of"] = len(units)
            self.batches.append(b)
            self.pieces += [(day, day, b["note"], i, text, len(units)) for i, text in enumerate(units)]

    @staticmethod
    def _manifest(snap: Path) -> dict:
        try:
            return json.loads((snap / "manifest.json").read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def _notes(self, skip: list[str]):
        """(relpath, path) of every *.md under the folder, dot folders and the store itself left out."""
        if not self.path.is_dir():
            return
        store = self.root.resolve()
        for top, dirs, names in os.walk(self.path):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and Path(top, d).resolve() != store)
            for n in sorted(names):
                p = Path(top, n)
                rel = p.relative_to(self.path).as_posix()
                if n.endswith(".md") and p.is_file() and not any(s and s in rel for s in skip):
                    yield rel, p

    def _pack(self, b: dict):
        """A day's new lines in pieces of about notes.MAX_CHARS, each file's block under its name
        (the git source's packing of one commit)."""
        text = ""
        for rel, (_, data) in b["files"].items():
            old = self.snap / "files" / rel
            lines = added(old.read_text(errors="replace") if old.is_file() else "", data.decode(errors="replace"))
            if not lines:
                continue
            block = f"--- {self.label}/{rel}\n" + "\n".join(lines) + "\n"
            for piece in notes._chunks(block, f"--- {self.label}/{rel} (continued)\n"):
                if text and len(text) + len(piece) > notes.MAX_CHARS:
                    yield text
                    text = ""
                text += piece
        if text:
            yield text

    def jobs(self, done: set | None = None) -> list[tuple]:
        """The pieces not logged yet (done: notes.done_sessions of live memory)."""
        done = done or set()
        return [j for j in self.pieces if (j[2], j[3]) not in done and (j[2], None) not in done]

    def carry(self, stage: Path | str) -> int:
        """After the run's units: the snapshot of every day whose note's pieces are all in the
        stage's log, under stage/.carry/ (it reaches memory only if the run is promoted).
        Returns the files it advanced."""
        stage = Path(stage)
        done = notes.done_sessions(stage)
        ready = [b for b in self.batches
                 if (b["note"], None) in done or all((b["note"], i) in done for i in range(b["of"]))]
        if not ready:
            return 0
        out = stage / ".carry" / SOURCES / self.label
        manifest = self._manifest(self.snap)   # live, read under the run lock
        n = 0
        for b in ready:
            for rel, (sha, data) in b["files"].items():
                p = out / "files" / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
                manifest[rel] = sha
                n += 1
        (out / "manifest.json").write_text(json.dumps(manifest, indent=0, sort_keys=True) + "\n")
        return n

    def summary(self) -> str:
        if not self.path.is_dir():
            return f"folder {self.label}: {self.path} is not a folder (nothing read)"
        files = sum(len(b["files"]) for b in self.batches)
        return f"folder {self.label}: {files} changed file(s) over {len(self.batches)} day(s), {len(self.pieces)} piece(s)"


def folder_jobs(spec: str, root: Path | str, skip: list[str], stage: Path | str | None = None,
                done: set | None = None) -> list[tuple]:
    """Import jobs for the notes of one folder that changed since its snapshot, shaped like
    notes.git_jobs' (day, day, note, piece, text, of); done: the logged pieces (default: live
    memory's log). stage: a run's stage after its units ran: the advanced snapshot goes
    under stage/.carry/ (Folder.carry). deltos import keeps the Folder it read the jobs from
    and carries that, so the snapshot is exactly the text the run read."""
    f = Folder(spec, root, skip)
    if stage is not None:
        f.carry(stage)
    return f.jobs(notes.done_sessions(Path(root)) if done is None else done)
