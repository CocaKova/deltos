"""Fixed-text search over folders of markdown: ripgrep when it is installed, the same
search in Python when it is not.

Recall and pages read memory with one ripgrep pass. ripgrep is not on every machine an
agent runs on, and a missing binary must not take memory down with it, so `run` answers
the same question either way: it takes ripgrep's arguments and returns ripgrep's output
("path:line:text"), and the callers do not know which one ran.

The Python search covers the arguments deltos uses and no others (-i -n --no-heading -F
--with-filename, --glob, -e): an argument it does not know is an error, so a caller that
starts using one has to teach this file too. Like ripgrep it skips hidden files and
folders (but not a hidden file a --glob names), symlinks below the folders it is given,
and binary files. Unlike ripgrep it does not read .gitignore files. $DELTOS_RG=0 uses it even when ripgrep is installed.
"""
from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess

FLAGS = {"-i", "-n", "--no-heading", "-F", "--with-filename"}


def have() -> bool:
    return os.environ.get("DELTOS_RG", "1") != "0" and shutil.which("rg") is not None


def run(args: list[str], cwd: str | os.PathLike | None = None, timeout: float | None = None) -> str:
    """What `rg <args>` prints, run in `cwd`."""
    if have():
        return subprocess.run(["rg", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout).stdout
    return search(args, cwd)


def _split(args: list[str]) -> tuple[list[str], list[str], list[str], bool]:
    pats, globs, paths, named = [], [], [], False
    it = iter(args)
    for a in it:
        if a == "-e":
            pats.append(next(it))
        elif a == "--glob":
            globs.append(next(it))
        elif a in FLAGS:
            named = named or a == "--with-filename"
        elif a.startswith("-"):
            raise ValueError(f"deltos/rg.py does not know the ripgrep argument {a!r}")
        else:
            paths.append(a)
    return pats, globs, paths, named


def _files(top: str, keep: list[str], skip: list[str]):
    """The files ripgrep would search under `top`, in name order."""
    for here, dirs, files in os.walk(top):
        kept = lambda name: any(fnmatch.fnmatchcase(name, g) for g in keep)
        dirs[:] = sorted(d for d in dirs if (not d.startswith(".") or kept(d))
                         and not os.path.islink(os.path.join(here, d))
                         and not any(fnmatch.fnmatchcase(d, g) for g in skip))
        for f in sorted(files):
            # a hidden file a --glob names is searched (ripgrep: a glob given by hand beats the hidden rule)
            if (f.startswith(".") and not kept(f)) or any(fnmatch.fnmatchcase(f, g) for g in skip):
                continue
            if keep and not kept(f):
                continue
            p = os.path.join(here, f)
            if not os.path.islink(p):
                yield p


def search(args: list[str], cwd: str | os.PathLike | None = None) -> str:
    """The same lines ripgrep prints for `args`, found in Python (files in name order)."""
    pats, globs, paths, named = _split(args)
    pats = [p for p in pats if p]
    if not pats or not paths:
        return ""
    rx = re.compile("|".join(re.escape(p) for p in sorted(pats, key=len, reverse=True)), re.IGNORECASE)
    keep = [g for g in globs if not g.startswith("!")]
    skip = [g[1:] for g in globs if g.startswith("!")]
    base = os.fspath(cwd) if cwd else ""
    named = named or len(paths) > 1
    out = []
    for arg in paths:
        full = os.path.join(base, arg)
        if os.path.isdir(full):
            found = [(p, os.path.join(arg, os.path.relpath(p, full))) for p in _files(full, keep, skip)]
            label = True
        else:
            found, label = [(full, arg)], named
        for path, shown in found:
            try:
                with open(path, encoding="utf-8", errors="replace", newline="") as fh:
                    text = fh.read()
            except OSError:
                continue
            if "\0" in text or not rx.search(text):
                continue
            for no, line in enumerate(text.split("\n"), 1):
                if rx.search(line):
                    out.append(f"{shown}:{no}:{line}" if label else f"{no}:{line}")
    return "".join(x + "\n" for x in out)
