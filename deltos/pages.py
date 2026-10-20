"""Pages: the documents beside the facts (wiki pages, notes, project memories).

Facts answer "what is X now"; pages hold what does not fit a fact (how-tos,
decisions, incident write-ups). Pages are plain markdown files in folders:
writable ones and read-only ones (configured in deltos.toml). No
index, no embeddings: ripgrep over a few hundred files takes milliseconds.

A page ref is its path relative to its folder; read-only folders carry their
name as a prefix ("memory/project_backups.md").
"""
from __future__ import annotations

import datetime
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .recall import STOP

FM = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.S)
DATE = re.compile(r"^(?:updated|modified|date|last_updated):\s*['\"]?(\d{4}-\d{2}-\d{2})", re.M)
STRUCTURAL = {"brain-current.md", "index.md", "README.md"}
SKIP = ("*.bak*", "ARCHIVE-*", "MEMORY.md")   # rg also skips hidden dirs (.trash) and .gitignore'd files
TOP_LINES = 3     # lines shown per page in a top() result
LINE_CHARS = 300
SEARCH_PER_PAGE = 3   # page_search lines per page (40 in all)
# Previews of chat pages (DELTOS_PREVIEW=1; opt-in until both models are scored).
# 10-16: 71% of preview lines were the assistant's advice ("keep me updated on your
# appointments") while the person's facts are in their own lines, and a chat turn
# is one long line whose first LINE_CHARS can stop before the part that matches.
# On: a chat's own-words lines ("user: ", a digest's "- asked:") come first,
# unless the question asks what was said to the person ("what did you
# recommend", "our previous conversation"); a long line shows the sentences that
# match best instead of its start (a chat turn's only: a note's line starts with
# its headline, and its later part is often older history).
PREVIEW = os.environ.get("DELTOS_PREVIEW") == "1"
SAID = re.compile(r"^\s*(user: |- asked: |- then \()")
TOLD = re.compile(r"\byou (said|told|mentioned|recommended|suggested|gave|provided|listed|shared|wrote|created|"
                  r"explained|described|named|came up with|helped)\b|\b(our|the|a|that) (previous|last|earlier|past) "
                  r"(conversation|chat|discussion)\b|\bremind me\b|\bwe (talked|discussed|spoke|chatted)\b", re.I)
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
ROLE = re.compile(r"^(user|assistant|system): ")
# A line that starts with a date is a dated entry ("**✅ 09-28 TURNED OFF**",
# "- 10-06 RESOLVED UPSTREAM: ..."): notes grow by appending them, so the newest
# one is the page's latest word on its subject.
ENTRY_DATE = re.compile(r"(?<![\d.:-])(?:(20\d\d)-)?(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])(?![\d-])")
FRONT = re.compile(r"(name|title|description|updated|modified|date|last_updated)\s*:", re.I)
# A page's age is in its label ("[ref · 2026-07-10 · 3 months old]"), not in its score.
# 10-19: "Where are the cron jobs?" put a 101-day-old July page first and the agent
# answered from it. An age decay on the score (x 0.5 + 0.5 * 2^(-age/60)) was tried,
# pre-registered to go if it regressed any suite: it buried old pages that are still
# true (the gate's live suite regressed 2; a 66-day-old page holding the answer fell
# from #1 to #4), so it is not in the ranking.


def query_terms(text: str, skip: tuple[str, ...] = ()) -> list[str]:
    """The words top() looks pages up by: 3+ characters, no stop words."""
    return sorted({w for w in re.findall(r"[a-z0-9][a-z0-9._-]{2,}", text.lower())
                   if w not in STOP and w not in skip})


def _days(day: str, today: str) -> int | None:
    """Days from `day` to `today` (both YYYY-MM-DD), None when either isn't a date."""
    try:
        return (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(day)).days
    except ValueError:
        return None


def age(day: str, today: str) -> str:
    """How old a page dated `day` is, for its label: "today", "N days old" (under
    14 days), "N weeks old" (under 60), else "N months old"; "" with no date."""
    n = _days(day, today)
    if n is None:
        return ""
    if n <= 0:
        return "today"
    if n < 14:
        return f"{n} day{'s' if n > 1 else ''} old"
    if n < 60:
        return f"{n // 7} weeks old"
    return f"{n // 30} months old"


class PageError(Exception):
    pass


@dataclass
class Folder:
    path: Path
    prefix: str = ""        # ref prefix; "" for the writable folder
    writable: bool = False
    searchable: bool = True  # False: opens by ref, stays out of search (deltos's own notes)


def _window(line: str, weight: dict[str, float], width: int = LINE_CHARS) -> str:
    """The part of a long line that matches best, at most `width` chars: the
    best-matching sentence, grown by the sentences after and before it while
    they fit, with "…" where text is left out. A line whose start already holds
    every word it matches is shown from its start, as before."""
    s = line.strip()
    head = s[:width]
    if not PREVIEW or len(s) <= width:
        return head
    have = lambda t: sum(v for w, v in weight.items() if w in t.lower())
    if have(head) >= have(s):
        return head
    role = ROLE.match(s)
    pre = role.group(0) if role else ""
    body = s[len(pre):]
    room = width - len(pre) - 4   # the two "… " marks
    cuts = [0] + [m.end() for m in SENTENCE_END.finditer(body)] + [len(body)]
    spans = [(cuts[k], cuts[k + 1]) for k in range(len(cuts) - 1) if cuts[k] < cuts[k + 1]]
    k = max(range(len(spans)), key=lambda k: (have(body[spans[k][0]:spans[k][1]]), -k))
    lo = hi = k
    a, b = spans[k]
    grew = True
    while grew:
        grew = False
        if hi + 1 < len(spans) and spans[hi + 1][1] - a <= room:
            hi += 1
            b, grew = spans[hi][1], True
        if lo > 0 and b - spans[lo - 1][0] <= room:
            lo -= 1
            a, grew = spans[lo][0], True
    if b - a > room:   # one sentence longer than the room: from a little before its first match
        low = body.lower()
        first = min((i for w in weight if (i := low.find(w, a, b)) >= 0), default=a)
        a = max(a, first - room // 4)
        b = min(b, a + room)
    return pre + ("… " if a > 0 else "") + body[a:b].strip() + (" …" if b < len(body) else "")


def _write(path: Path, text: str):
    """Temp file + rename: a crash mid-edit leaves the old page, never half of it."""
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    tmp.write_text(text)
    os.replace(tmp, path)


def _entry_date(line: str, page_date: str) -> str:
    """The date a dated entry starts with (YYYY-MM-DD), "" for other lines. A
    month-day takes the page's year, or the year before when that would put it
    after the page."""
    head = line.lstrip()
    m = ENTRY_DATE.search(head[:30])
    if not m or FRONT.match(head):
        return ""   # no date, or front matter: the page's summary, not an entry
    year = m.group(1) or page_date[:4]
    d = f"{year}-{m.group(2)}-{m.group(3)}"
    return d if m.group(1) or d <= page_date else f"{int(year) - 1}-{m.group(2)}-{m.group(3)}"


def _date(path: Path) -> str:
    with open(path, errors="replace") as f:   # the front matter is in the head
        head = f.read(2000)
    m = FM.match(head)
    d = DATE.search(m.group(1)) if m else None
    return d.group(1) if d else datetime.date.fromtimestamp(path.stat().st_mtime).isoformat()


class Pages:
    def __init__(self, writable: Path, readonly: dict[str, Path] | None = None, hidden: dict[str, Path] | None = None,
                 today: str = ""):
        """hidden: read-only folders that open by ref but stay out of search,
        so a [[link]] memory prints (a fact note, its cold history) opens.
        today: YYYY-MM-DD that page ages count to (a replayed world); default the real date."""
        self.today = today
        self.home = Folder(Path(writable).expanduser().resolve(), "", True)
        self.folders = [self.home] + [Folder(Path(p).expanduser().resolve(), f"{name}/") for name, p in (readonly or {}).items()]
        self.folders += [Folder(Path(p).expanduser().resolve(), f"{name}/", False, False) for name, p in (hidden or {}).items()]

    # -- refs ----------------------------------------------------------------

    def ref(self, path: Path) -> str:
        for f in self.folders:
            try:
                return f.prefix + str(path.relative_to(f.path))
            except ValueError:
                continue
        return str(path)

    def resolve(self, ref: str) -> tuple[Path, Folder]:
        """A page ref (with or without .md, or the full `[ref · date]` label) to its file."""
        ref = (ref or "").strip().strip("[]").split(" · ")[0].strip()
        if not ref:
            raise PageError("ref is required: the [page] shown by memory_recall or page_search.")
        for f in sorted(self.folders, key=lambda f: -len(f.prefix)):
            if f.prefix and not ref.startswith(f.prefix):
                continue
            rel = ref[len(f.prefix):]
            for cand in (f.path / rel, f.path / (rel + ".md")):
                real = cand.resolve()
                if real.is_file() and real.is_relative_to(f.path) and real.suffix == ".md":
                    if any(part.startswith(".") for part in real.relative_to(f.path).parts):
                        raise PageError(f"{ref!r} is in a hidden or trash folder.")
                    return real, f
        hits = [self.ref(p) for f in self.folders for p in f.path.rglob(Path(ref).name.removesuffix(".md") + ".md")
                if not any(x.startswith(".") for x in p.relative_to(f.path).parts)]
        if len(hits) == 1:
            return self.resolve(hits[0])
        raise PageError(f"No page {ref!r}." + (f" Did you mean: {', '.join(hits[:5])}?" if hits else ""))

    # -- read ----------------------------------------------------------------

    def _rg(self, args: list[str]) -> list[tuple[Path, int, str]]:
        cmd = ["rg", "-i", "-n", "--no-heading", "--with-filename", "--glob", "*.md"]
        for g in SKIP:
            cmd += ["--glob", "!" + g]
        where = [str(f.path) for f in self.folders if f.searchable and f.path.is_dir()]
        if not where:   # rg with no paths would search the current directory
            return []
        out = subprocess.run(cmd + args + where,
                             capture_output=True, text=True, timeout=20).stdout
        rows = []
        for ln in out.splitlines():
            m = re.match(r"^(.*?\.md):(\d+):(.*)$", ln)
            if m and not any(part.startswith(".") for part in Path(m.group(1)).parts[-3:-1]):
                rows.append((Path(m.group(1)), int(m.group(2)), m.group(3)))
        rows.sort(key=lambda r: (str(r[0]), r[1]))   # rg's file order changes run to run
        return rows

    def search(self, text: str, limit: int = 40) -> str:
        """Exact (case-insensitive) text across all pages, newest page first."""
        if not text.strip():
            raise PageError("search text is empty.")
        rows = self._rg(["-F", "-e", text.strip()])
        if not rows:
            return "(no matches)"
        dates = {p: _date(p) for p in {r[0] for r in rows}}
        rows.sort(key=lambda r: (dates[r[0]], -r[1]), reverse=True)
        # A common word filled all 40 lines from the newest few pages, and an
        # older page that says it too never showed ("how many times did I ...":
        # the older times). A few lines a page, so the lines cover more pages.
        # Lines a page: the 40 shared by the pages that match, at least 3 each.
        pages = list(dict.fromkeys(r[0] for r in rows))
        cap = max(SEARCH_PER_PAGE, limit // len(pages))
        shown, per = [], {}
        for p, i, t in rows:
            if len(shown) < limit and per.get(p, 0) < cap:
                shown.append((p, t))
                per[p] = per.get(p, 0) + 1
        out = [f"[{self.ref(p)} · {dates[p]}] {_window(t, {text.strip().lower(): 1.0})}" for p, t in shown]
        # Name where the rest is, so page_read can open it: a page cut short
        # with how many lines it has left, and pages not shown at all.
        total = {p: sum(1 for r in rows if r[0] == p) for p in pages}
        more = [f"{self.ref(p)} (+{total[p] - per.get(p, 0)})" for p in pages if total[p] > per.get(p, 0)]
        if more:
            out.append(f"... {len(rows) - len(shown)} more lines, in: {', '.join(more[:20])}" + (" ..." if len(more) > 20 else ""))
        return "\n".join(out)

    def top(self, question: str, n: int = 3, skip_terms: tuple[str, ...] = (),
            min_cover: int = 1, extra_terms: dict[str, float] | None = None) -> list[tuple[str, str, list[str]]]:
        """Pages that match the most (and rarest) words of a question:
        [(ref, date, best lines)], the date being the page's newest (its own, or
        the newest dated entry among the lines that matched). Brain-free ranking,
        for the per-turn block.
        min_cover: words of the question that must meet in one line of a page.
        extra_terms: more words to look for, each weighing its multiplier times
        a question word (the previous message's words, when this one is a short
        follow-up: "I mean in the app"). They rank pages, never let one in: a
        page still needs the question's own words, min_cover of them."""
        terms = query_terms(question, skip_terms)
        if not terms:
            return []
        extra = {w: x for w, x in (extra_terms or {}).items()
                 if w not in terms and w not in STOP and w not in skip_terms}
        every = sorted(set(terms) | set(extra))
        rows = self._rg(["-F"] + [x for t in every for x in ("-e", t)])
        per: dict[Path, dict[int, set[str]]] = {}
        heads: dict[Path, set[int]] = {}
        line_of: dict[tuple[Path, int], str] = {}
        for p, i, t in rows:
            tl = t.lower()
            per.setdefault(p, {})[i] = {w for w in every if w in tl}
            line_of[p, i] = t
            if i < 15 and re.match(r"\s*(title|description|name):", tl):
                heads.setdefault(p, set()).add(i)
        # A chat digest records the question being asked right now: that section
        # echoes the question, it doesn't answer it.
        echo = " ".join(question.lower().split())[:60]
        if len(echo) >= 20:
            for p in {q for q, _, t in rows if echo in " ".join(t.lower().split())}:
                text = p.read_text(errors="replace").splitlines()
                start = 0
                for i, ln in enumerate(text, 1):
                    if ln.startswith("## "):
                        start = i
                    elif echo in " ".join(ln.lower().split()):
                        end = next((j for j in range(i + 1, len(text) + 1) if text[j - 1].startswith("## ")), len(text) + 1)
                        for j in range(start or i, end):
                            per[p].pop(j, None)
                if not per[p]:
                    del per[p]
        df = {w: sum(1 for lines in per.values() if any(w in s for s in lines.values())) for w in every}
        weight = {w: (1.0 if w in terms else extra[w]) / df[w] for w in every if df[w]}

        # Words that meet in one line (or in the file name) say what a page is
        # about; a long page that has each word somewhere does not.
        def score(p: Path) -> float:
            line = max(sum(weight[w] for w in ws) for ws in per[p].values())
            name = sum(weight[w] for w in every if w in weight and w in p.stem.lower())
            # The title / description line is the page's own summary, so it counts
            # again: when it is also the best line, that line counts twice. Counting
            # it once was tried (10-19): the gate's live suite regressed 2, the page
            # whose title says it fell out of the top 8 behind a newer stale one.
            head = max((sum(weight[w] for w in ws) for i, ws in per[p].items() if i in heads.get(p, ())), default=0)
            return line + name + head

        if min_cover > 1:   # whole words here: "rain" is not in "brain"
            whole = {(p, i) for p, i, t in rows
                     if sum(1 for w in terms if re.search(rf"\b{re.escape(w)}\b", t.lower())) >= min_cover}
            per = {p: ls for p, ls in per.items() if any((p, i) in whole for i in ls)}
        elif extra:   # the previous message's words alone don't let a page in
            per = {p: ls for p, ls in per.items() if any(ws.intersection(terms) for ws in ls.values())}
        sc = {p: score(p) for p in per}
        # ties: the newest page first, then by name (never by rg's thread order,
        # which changed the block run to run). Only pages at or above the cut can
        # be picked, so only they need their date read.
        cut = sorted(sc.values(), reverse=True)[n - 1] if len(sc) > n else min(sc.values(), default=0)
        near = [p for p in per if sc[p] >= cut]
        # A page's date: its own, or the newest dated entry among the lines that
        # matched when that is later ("- 2026-10-06 RESOLVED"), never after today.
        today = self.today or datetime.date.today().isoformat()
        own_date = {p: _date(p) for p in near}
        dates = {p: min(today, max([own_date[p]] + [_entry_date(line_of[p, i], own_date[p]) for i in per[p]]))
                 for p in near}
        best = sorted(near, key=lambda p: (-sc[p], tuple(-ord(c) for c in dates[p]), str(p)))[:n]
        own = PREVIEW and not TOLD.search(question)   # the person's own lines first
        out = []
        for p in best:
            if sc[p] <= 0:
                continue
            text, day = p.read_text(errors="replace").splitlines(), own_date[p]
            hit = lambda i: sum(weight[w] for w in per[p][i])
            said = lambda i: own and i <= len(text) and bool(SAID.match(text[i - 1]))
            lines = sorted(per[p], key=lambda i: (-said(i), -hit(i), i))[:TOP_LINES]
            # The newest dated entry that matches gets a slot when the picked lines
            # are older: a note's first lines describe the problem, its last entry
            # says how it ended. It replaces an undated line first (a summary),
            # else the oldest entry, and only if it matches half as well.
            dated = {i: d for i in per[p] if i <= len(text) and (d := _entry_date(text[i - 1], day))}
            if dated and len(lines) == TOP_LINES:
                newest = max(dated, key=lambda i: (dated[i], i))
                if newest not in lines and all(dated.get(i, "") < dated[newest] for i in lines):
                    out_ = min(lines, key=lambda i: (dated.get(i, ""), hit(i)))
                    if hit(newest) >= hit(out_) / 2:
                        lines = [i for i in lines if i != out_] + [newest]
            show = lambda ln, ws: (_window(ln, {w: weight[w] for w in ws if w in weight})
                                   if ROLE.match(ln) or SAID.match(ln) else ln.strip()[:LINE_CHARS])
            out.append((self.ref(p), dates[p], [show(text[i - 1], per[p][i]) for i in sorted(lines)]))
        return out

    def recent(self, since: str, n: int = 15, skip: tuple[str, ...] = (),
               per_day_max: int = 4) -> list[tuple[str, str, str]]:
        """Pages updated on or after `since`, newest first: [(date, ref, what it
        is)], where "what it is" is the page's description or title."""
        rows = []
        for f in self.folders:
            if not f.searchable or f.prefix in skip:
                continue
            for p in f.path.rglob("*.md"):
                if any(part.startswith(".") for part in p.relative_to(f.path).parts) or p.stem.split("-")[0].isupper():
                    continue   # hidden, or an index (MEMORY.md, ARCHIVE-index-...)
                d = _date(p)
                if d >= since:
                    rows.append((d, p))
        out, per_day = [], {}
        for d, p in sorted(rows, key=lambda r: (r[0], r[1].stat().st_mtime), reverse=True):
            if len(out) >= n or per_day.get(d, 0) >= per_day_max:
                continue   # a busy day must not hide the rest of the week
            per_day[d] = per_day.get(d, 0) + 1
            head = FM.match(p.read_text(errors="replace")[:3000])
            m = head and (re.search(r'^description:\s*[>|][-+]?\s*\n\s+(.+)$', head.group(1), re.M)
                          or re.search(r'^description:\s*["\']?(.+?)["\']?\s*$', head.group(1), re.M)
                          or re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', head.group(1), re.M))
            what = m.group(1) if m else next((ln.lstrip("# ") for ln in p.read_text(errors="replace").splitlines()
                                             if ln.strip()), "")
            out.append((d, self.ref(p), what[:LINE_CHARS // 2]))
        return out

    def read(self, ref: str, limit: int = 20000) -> str:
        path, _ = self.resolve(ref)
        text = path.read_text(errors="replace")
        return text if len(text) <= limit else text[:limit] + f"\n... ({len(text) - limit} more chars)"

    # -- write (the writable folder only) -----------------------------------

    def _writable(self, ref: str) -> Path:
        path, f = self.resolve(ref)
        if not f.writable:
            raise PageError(f"{self.ref(path)} is read-only (it belongs to {f.prefix.rstrip('/')}). "
                            "Do not retry: record the correct value with remember, or tell the user which page is stale.")
        return path

    def write(self, content: str, now: datetime.datetime | None = None) -> str:
        """A new page under notes/; returns its ref."""
        content = (content or "").strip()
        if not content:
            raise PageError("content is empty.")
        now = now or datetime.datetime.now()
        words = re.sub(r"[^a-z0-9 ]", "", content.lower())[:60].split()
        path = self.home.path / "notes" / f"{now:%Y-%m-%d-%H%M%S}-{'-'.join(words[:6]) or 'note'}.md"
        title = content.splitlines()[0][:120].replace('"', "'")
        path.parent.mkdir(parents=True, exist_ok=True)
        _write(path, f'---\ntitle: "{title}"\nupdated: {now:%Y-%m-%d}\n---\n\n{content}\n')
        return self.ref(path)

    def edit(self, ref: str, new_text: str, old_text: str = "", today: str = "") -> str:
        """Replace one exact span (or the whole body) of a writable page."""
        new_text = (new_text or "").rstrip()
        if not new_text:
            raise PageError("new_text is empty (use page_forget to remove a page).")
        path = self._writable(ref)
        text = path.read_text()
        m = FM.match(text)
        fm, body = (text[: m.end()], text[m.end():]) if m else ("", text)
        old_text = (old_text or "").strip()
        if old_text:
            k = body.count(old_text)
            if k != 1:
                raise PageError(f"old_text matches {k} places in {self.ref(path)}; it must match exactly one."
                                + (f" Current page:\n{body.strip()[:1500]}" if k == 0 else ""))
            body = body.replace(old_text, new_text, 1)
        else:
            body = "\n" + new_text + "\n"
        today = today or datetime.date.today().isoformat()
        if fm and re.search(r"^updated:.*$", fm, re.M):
            fm = re.sub(r"^updated:.*$", f"updated: '{today}'", fm, count=1, flags=re.M)
        elif fm:
            fm = fm.replace("\n---", f"\nupdated: '{today}'\n---", 1)
        _write(path, fm + body if (fm + body).endswith("\n") else fm + body + "\n")
        return self.ref(path)

    def forget(self, ref: str) -> str:
        """Move a writable page to .trash/ (search skips it; git keeps it)."""
        path = self._writable(ref)
        if path.name in STRUCTURAL:
            raise PageError(f"{path.name} is structural: edit it instead of forgetting it.")
        dest = self.home.path / ".trash" / path.relative_to(self.home.path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest))
        return self.ref(path)
