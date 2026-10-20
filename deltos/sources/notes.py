"""Import an existing pile of notes into deltos, with the model as the writer.

Each note (big ones split at headings) becomes ONE model call that returns the
facts it states as JSON; the store applies them. No agent loop: an agent
re-sends the whole conversation every tool call, which cost ~5 minutes and
~50k tokens per note in a trial. Notes go oldest first, but order matters less than it
seems: each fact carries the date it became true, and the store puts late
news into the trail, not over the current value.

Two ways in: a folder of notes, or the git history of note repos (each
commit's added lines, dated by the commit: the day memory learned them).
deltos import runs it from deltos.toml; bench.migrate is the benchmark's CLI.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import ground
from .. import items as _items
from ..llm import chat, cut_off, thinking, thinking_stays_on
from ..recall import FIRST_PERSON, STOP, _stem
from ..store import (KINDS, Store, _attr_key, _key, attribute_problem, base_name, strip_window, trust_hold,
                     value_problem)

MAX_CHARS = 9000
OUTAGE_TRIES, OUTAGE_WAIT = 30, 60   # ~30 min of retries per session before it is logged as an error

PROMPT = """You are moving an old memory note into a structured memory.
{source} List the facts it states about concrete things
(machines, services, models, apps, repos, releases, settings, people, accounts, ...).

Reply with lines only, no JSON, no commentary:
= <thing> | <other names, separated by ;> | <kind>
<thing> | <attribute> | <value> | <since YYYY-MM-DD>
<thing> | <attribute> | <value> | <since YYYY-MM-DD> | correction

Things and names
- "=" lines register a thing and other names people use for that ONE thing (only things with facts).
  Not names: versions, ports, IPs, paths, URLs, sizes, chips/parts it contains, commands.
Attributes: use these when they fit, exactly as written:
  model, version, latest release, status, host, port, location, owner, repo, url, container,
  service, schedule, kind, setting <setting name>
  Make a new one only when none fits. Values are short: the value only, no explanation.
  A status is a state in 1-4 words ("running", "released"), not an event or a reason.
Dates
- "since" is the date the text gives for when the value became true. Dates without a year
  ("10-04", "09-27") are {year} dates. If the text gives no date for that fact, write - (memory then uses {created}).
{passing}- If a value replaced an older one, give both, each with its own date. If a value was wrong all
  along, give the right one and end the line with "| correction".
- Use "| correction" only when the text says the earlier value was wrong or a mistake.
- One value per attribute at a time. Only facts someone would ask about later: what a thing is,
  where it runs, its model/version/status/port/owner/host. Skip measurements, benchmark numbers,
  timings, lists (features, tags, members), how-to steps, opinions, and plans that did not happen.

Example with made-up names (never write its lines). Memory holds: box-7 (b7) | Owner: Dana
Note: "Nimbus 4.1 shipped 03-02, now on box-7 (b7); build took 9 min. b7 serves it on :9100. Maybe k8s later."
Reply:
= Nimbus | | app
Nimbus | latest release | 4.1 | {year}-03-02
Nimbus | host | box-7 | {year}-03-02
Nimbus | port | 9100 | -
Not written: box-7's owner (memory holds it; the note doesn't say it), the build time, k8s (a plan).
The thing is "box-7", not "box-7 (b7)".

What memory already holds about things this note mentions. It is reference, not part of the note:
write a line only for what the note itself says. Reuse these names exactly when it is the same
thing, and an entry's label exactly when it is the same fact; a new value for an entry is an
update with its date, not a correction{skip}.
<memory>
{known}
</memory>"""

# v4 (10-17, opt-in until scored on two models): v3 with what to keep said first. DeepSeek
# V4.1 Flash's first nightly import (10-17, undone) wrote ~2x the lines of Flash-Next, a third
# of them refused: test numbers, a command's flags and one-time states as "setting" entries,
# note file names as things, all of which v1/v3 already ask to skip, in their last rule.
PROMPT_V4 = PROMPT.replace(
    """(machines, services, models, apps, repos, releases, settings, people, accounts, ...).
""",
    """(machines, services, models, apps, repos, releases, settings, people, accounts, ...).
Write only facts someone would ask about in a month: what a thing is, where it runs, its
model/version/status/port/owner/host, a value it is configured with. Skip the rest however exact:
measurements, benchmark and test numbers, counts, timings, one-time events, lists, how-to steps,
opinions, and plans that did not happen. Most notes hold only a few such facts.
""").replace(
    """  Not names: versions, ports, IPs, paths, URLs, sizes, chips/parts it contains, commands.
""",
    """  Not names: versions, ports, IPs, paths, URLs, sizes, chips/parts it contains, commands,
  file names and note titles.
""").replace(
    """  A status is a state in 1-4 words ("running", "released"), not an event or a reason.
""",
    """  A status is a state in 1-4 words ("running", "released"), not an event or a reason.
  A setting is a value something is configured with and keeps (an address, a mode, a limit),
  not a command's flag, a one-time state ("restarted", "deleted"), a count or a test result.
""")
assert PROMPT_V4 != PROMPT and PROMPT_V4.count("Most notes hold") == 1 and "note titles" in PROMPT_V4 and "A setting is" in PROMPT_V4


# The first prompt (to 10-14): what memory holds sat at the end of the instructions,
# unmarked, and one model (GLM-5.3-Flash) wrote that list back out as the note's
# facts. The current one fences it, says it is reference, and shows a worked example.
# (A try with the list moved into the message before the note made Flash-Next restate
# it more and dump sentences as statuses: bench.importtest 10-14, dropped.)
PROMPT_V1 = """You are moving an old memory note into a structured memory.
{source} List the facts it states about concrete things
(machines, services, models, apps, repos, releases, settings, people, accounts, ...).

Reply with lines only, no JSON, no commentary:
= <thing> | <other names, separated by ;> | <kind>
<thing> | <attribute> | <value> | <since YYYY-MM-DD>
<thing> | <attribute> | <value> | <since YYYY-MM-DD> | correction

Things and names
- "=" lines register a thing and other names people use for that ONE thing (only things with facts).
  Not names: versions, ports, IPs, paths, URLs, sizes, chips/parts it contains, commands.
  "Keryx 2.17.2" is not a thing: it is Keryx | latest release | 2.17.2.
Attributes: use these when they fit, exactly as written:
  model, version, latest release, status, host, port, location, owner, repo, url, container,
  service, schedule, kind, setting <setting name>
  Make a new one only when none fits. Values are short: the value only, no explanation.
Dates
- "since" is the date the text gives for when the value became true. Dates without a year
  ("10-04", "09-27") are {year} dates. If the text gives no date for that fact, write - (memory then uses {created}).
{passing}- If a value replaced an older one, give both, each with its own date. If a value was wrong all
  along, give the right one and end the line with "| correction".
- Use "| correction" only when the text says the earlier value was wrong or a mistake.
- One value per attribute at a time. Only facts someone would ask about later: what a thing is,
  where it runs, its model/version/status/port/owner/host. Skip measurements, benchmark numbers,
  timings, lists (features, tags, members), how-to steps, opinions, and plans that did not happen.
Things memory already knows that this text mentions, with what memory believes now (reuse these
names exactly when it is the same thing, and an entry's label exactly when it is the same fact; a
new value for an entry is an update with its date, not a correction{skip}):
{known}"""


# The store refused some lines (a name that is a phrase, a label or value that
# is a sentence). One more call, with the store's reasons, instead of losing them.
REPAIR_PROMPT = """Memory refused these lines from you, each followed by the reason:

{refused}

Rewrite each one so memory can take it: <thing> | <attribute> | <value> | <since YYYY-MM-DD>.
Keep the line's facts: the same thing, the same value and the same date. Change only what the
reason names: labels are a few plain words (no brackets or symbols); values are the value only,
ten words at most; a thing has a real name ("Sam's car" is not a name: it is Sam | car | <value>).
If it is the same fact as an entry memory already holds, use that entry's label, but keep the
new value: never copy a value from memory. What memory holds:{labels}
Drop a line that is not a fact worth remembering. Reply with lines only."""
REPAIR_TOKENS = 1024


def _dates(text: str, name: str, git: dict) -> tuple[str, str]:
    """(written, last edited). Undated facts default to when the note was
    written: content in an old note is usually as old as the note, and its
    last-edit date (a later banner, a typo fix) would make old values current."""
    m = (re.search(r'^\s*(?:updated|modified): "?(\d{4}-\d\d-\d\d)', text, re.M)
         or re.search(r"(\d{4}-\d\d-\d\d)", name))
    edited = m.group(1) if m else git.get("edited", {}).get(name, "2026-01-01")
    c = re.search(r'^\s*(?:date|created): "?(\d{4}-\d\d-\d\d)', text, re.M)
    created = c.group(1) if c else git.get("created", {}).get(name, edited)
    if re.search(r"MEMORY\.md|brain-current|daily-todo|NOW", name):   # live status pages
        created = edited
    return min(created, edited), edited


def _pieces(part: str) -> list[str]:
    """A part longer than MAX_CHARS cut at line ends; a single line longer than that at the
    last sentence end (or space) before the limit, never inside a word."""
    if len(part) <= MAX_CHARS:
        return [part]
    out, cur = [], ""
    for ln in part.splitlines(keepends=True):
        while len(ln) > MAX_CHARS:
            cut = max(ln.rfind(". ", 0, MAX_CHARS), ln.rfind("; ", 0, MAX_CHARS)) + 2
            if cut < MAX_CHARS // 2:
                cut = ln.rfind(" ", 0, MAX_CHARS) + 1 or MAX_CHARS
            if cur:
                out.append(cur)
                cur = ""
            out.append(ln[:cut])
            ln = ln[cut:]
        if cur and len(cur) + len(ln) > MAX_CHARS:
            out.append(cur)
            cur = ""
        cur += ln
    return out + ([cur] if cur else [])


def _chunks(text: str, header: str = "") -> list[str]:
    """Pieces of about MAX_CHARS, cut at headings, else at line ends. Every piece after the
    first starts with `header` (which note it is). Until 10-19 a part was cut every MAX_CHARS
    characters, mid-sentence, and only the first piece said which note it came from: 8 of the
    20 pieces of GLM's 10-19 import had no file name, and the model filed their lines under
    whatever thing it had in mind (one note's lines became "settings" of a model)."""
    if len(text) <= MAX_CHARS:
        return [text]
    out, cur = [], ""
    for part in re.split(r"(?m)^(?=#{1,3} )", text):
        for piece in _pieces(part):
            if len(cur) + len(piece) > MAX_CHARS and cur:
                out.append(cur)
                cur = ""
            cur += piece
    out += [cur] if cur.strip() else []
    return out[:1] + [header + x for x in out[1:]]


def _git_jobs(repo: Path, commit: str, prefix: str, skip: list[str], since: str = ""):
    """Each commit's added lines, grouped per commit and packed into chunks: the
    day a line appeared is the day memory learned it, so dates come for free."""
    out = subprocess.run(["git", "-C", str(repo), "log", "--reverse", "-p", "--no-color", "--unified=0",
                          "--format=@@COMMIT %h %cs", commit, "--", "*.md"],
                         capture_output=True, text=True, errors="replace").stdout
    day, sha, fname, per = "", "", "", {}
    order = []
    for ln in out.splitlines():
        if ln.startswith("@@COMMIT "):
            _, sha, day = ln.split()
            order.append((day, sha))
        elif ln.startswith("+++ "):
            fname = ln[6:] if ln.startswith("+++ b/") else ""
        elif ln.startswith("+") and fname and not any(x and x in fname for x in skip):
            body = ln[1:]
            if not body.strip() or re.match(r"^\s*(updated|modified|ingested_at|date|originSessionId|node_type|---)\b", body):
                continue
            per.setdefault((day, sha), {}).setdefault(fname, []).append(body)
    for day, sha in order:
        if day < since:
            continue
        files = per.get((day, sha), {})
        text, i = "", 0
        for fname, lines in files.items():
            block = f"--- {prefix}/{fname}\n" + "\n".join(lines) + "\n"
            for piece in _chunks(block, f"--- {prefix}/{fname} (continued)\n"):
                if text and len(text) + len(piece) > MAX_CHARS:
                    yield day, f"{prefix}@{sha}", i, text
                    text, i = "", i + 1
                text += piece
        if text:
            yield day, f"{prefix}@{sha}", i, text


FULL_PROMPT = {"passing": '- A value mentioned in passing (a recap, an index line, "was X before") is not news: give it\n'
                          '  only if it is still true, or with the dates it held.\n',
               "skip": "; a fact that only repeats a belief needs no line"}
LEAN_PROMPT = {"passing": "", "skip": ""}

CORE = {"status", "kind", "location", "host", "owner", "model", "version", "latest release", "service",
        "container", "port", "url", "repo", "schedule"}


MAX_TOKENS = 8192
NAMES_ONLY_MIN = 3    # "=" lines with no fact line: the answer stopped at the names
NAMES_ONLY = ("You registered things but wrote no facts. Now write the fact lines for them, only what the note "
              "itself states: <thing> | <attribute> | <value> | <since YYYY-MM-DD>. Lines only.")
# The missed pass (GraphRAG calls it gleaning): one more turn for the facts the answer left out.
# 10-19: GLM at its own temperature (1.0) found 47 of 71 hand facts (3 draws) against 58 at 0.6, and
# what it dropped were exact details: ports, service names, paths, states.
MISSED = ("Read the note again. Write the fact lines your answer left out, only what the note itself states, "
          "in the same format; exact names, numbers, versions, paths, states and dates count. "
          "Lines only. If nothing is missing, write nothing.")
LOOP_REPEATS = 10    # one line said this often = the model looped (5% of 1,938 Flash-Next import sessions)
SPLIT_DEPTH = 2      # a cut-off output is retried as halves, then quarters
# The repeat guard: an answer is streamed and stopped once one line has been said REPEAT_STOP times,
# instead of running on to the token cap (GLM at temperature 0, 10-19: 7 of 20 test pieces looped to
# 8,192 tokens, minutes each). Of 1,259 logged import answers (Flash-Next, GLM, DSV), 146 looped
# (a line 10+ times); of the rest, 88 said a line 4+ times, all cycling blocks of the same lines.
REPEAT_STOP = 4
MIN_SPLIT = 1500     # chars: below this a cut-off piece is not split again
KNOWN_ENTRIES = 12   # entries shown per known thing
PACKED = re.compile(r"[\w .-]{1,40}: [^;]+(?:; ?[\w .-]{1,40}: [^;]+)*")
MAX_VALUES = 3   # more values than this for one fact on one date = a dump, not news
# Kinds that say the "thing" is a value: the format's own labels and kinds of number. GLM 10-19:
# "= deployed | | status", "= tonal noise | | measurement", "= temp data_dir | | setting",
# "= exit 255 | | error". Not "event" or "plan": a wedding or a trip is something people ask about.
VALUE_KINDS = {"status", "state", "setting", "settings", "version", "latest release", "port", "schedule",
               "measurement", "metric", "number", "count", "amount", "date", "time", "duration", "quote",
               "result", "score", "error", "value", "percentage"}


class _Line(list):
    """A line's columns, and what the model wrote (a refused line goes back
    to the model as written: cleanup puts "-" where a date was expected)."""
    raw = ""


def _repeating(text: str) -> str:
    """The stop check while an answer streams in: "repeat" once one finished line
    has been said REPEAT_STOP times."""
    done = text.rpartition("\n")[0]
    n = Counter(ln.strip() for ln in done.splitlines() if ln.strip())
    return "repeat" if n and max(n.values()) >= REPEAT_STOP else ""


def _unloop(msg: dict, usage: dict) -> dict:
    """An answer the repeat guard stopped, without its loop: the finished lines
    before the stop, less every line said REPEAT_STOP - 1 or more times (the loop
    cycles through several lines; a loop's lines were junk 16 of 16 times on 10-19).
    What was dropped stays in the log (usage loop_body); the text is then asked
    again in halves like any cut-off answer."""
    if usage.get("finish_reason") != "repeat":
        return msg
    done = [ln for ln in (msg.get("content") or "").rpartition("\n")[0].splitlines() if ln.strip()]
    n = Counter(ln.strip() for ln in done)
    body = {ln for ln, k in n.items() if k >= REPEAT_STOP - 1}
    usage["loop"], usage["loop_body"] = max(n.values(), default=0), sorted(body)
    return {**msg, "content": "\n".join(ln for ln in done if ln.strip() not in body)}


def _plan(content: str, capped: bool) -> tuple[list[list[str]], dict]:
    """The model's lines, cleaned up before the store sees them. A loop at
    temperature 0 repeats lines hundreds of times (and flips a value back and
    forth with "correction"); a dump lists every tag of a note as a status.
    Apply each line once; when one session gives several values for the same
    fact on the same date it can't be news in order, so keep the plain lines
    in the order written, the one flagged as a correction last and unflagged,
    or drop them all past MAX_VALUES."""
    raw = [ln.strip() for ln in (content or "").splitlines() if ln.strip()]
    if capped and raw:
        raw = raw[:-1]   # the output was cut off: the last line is a fragment
    # A loop's body is where the model got stuck, not something the text said ten times:
    # GLM's 10-19 import cycled "Hermes | setting lsp pyright result | ..." through three
    # values from one sentence 82 times each. Applied once, those lines were wrong facts.
    said_n = Counter(raw)
    stuck = {ln for ln, n in said_n.items() if n >= LOOP_REPEATS}
    lines, seen, displayed = [], set(), 0
    for ln in raw:
        if ln in stuck:
            continue
        if ln.startswith("+"):
            # "+ 2023-05-23 | baked sourdough bread (didn't rise)": something that
            # happened, for the daily log. Counts and totals over time ("how many
            # times did I bake?") are made of these, not of current facts.
            d, _, what = ln.lstrip("+ ").partition("|")
            ev = _Line(["+", d.strip() if re.fullmatch(r"\d{4}-\d\d-\d\d", d.strip()) else "-", what.strip()])
            ev.raw = ln
            if ev[2] and tuple(x.lower() for x in ev) not in seen:
                seen.add(tuple(x.lower() for x in ev))
                lines.append(ev)
            continue
        parts = _Line(x.strip() for x in ln.strip("`").split("|"))
        parts.raw = ln
        packed = [x for x in parts[1:3] if PACKED.fullmatch(x)] if len(parts) in (2, 3) else []
        if packed and not parts[0].startswith("="):
            # "Hermes | status: running; port: 8000": several undated facts in one line.
            for kv in dict.fromkeys(y.strip() for x in packed for y in x.split(";") if y.strip()):
                a, _, v = kv.partition(":")
                fact = _Line([parts[0], a.strip(), v.strip(), "-"])
                fact.raw = ln
                if tuple(x.lower() for x in fact) not in seen:
                    seen.add(tuple(x.lower() for x in fact))
                    lines.append(fact)
            continue
        if len(parts) == 3 and not parts[0].startswith("=") and len([x for x in parts[1].split(";") if x.strip()]) > 1:
            # "GLM-5.3 | GLM; glm53-head | model": a registration with its "=" left off
            # (GLM-5.3-Flash, 11 store entries by 10-20). A label is one attribute,
            # never a list of names.
            parts[0] = "= " + parts[0]
        if parts[0].startswith("=") and len(parts) in (2, 3) and ":" in parts[1]:
            # "= Alex | Location: Germany; Times met: 2 | person": facts packed into
            # the names slot (80 lines of the real import, 147 of LongMemEval's).
            # They are undated facts about the thing; the names before them stay names.
            # A piece after a fact continues its value, a list cut at its own ";"
            # ("consideration: American Airlines; Delta"), not another name: read
            # as one, "Delta" became the user's name (LongMemEval gpt4_f420262c).
            segs = [x.strip() for x in parts[1].split(";") if x.strip()]
            kind_seg = lambda x: re.match(r"(kind|type)\s*:", x, re.I)
            facts, left = [], []
            for x in segs:
                if re.fullmatch(r"[\w .'/-]{1,40}: ?\S.*", x) and not kind_seg(x):
                    facts.append(x)
                elif facts and not kind_seg(x):
                    facts[-1] += "; " + x
                else:
                    left.append(x)
            name = parts[0].lstrip("= ").strip()
            for kv in dict.fromkeys(facts):
                a, _, v = kv.partition(":")
                fact = _Line([name, a.strip(), v.strip(), "-"])
                fact.raw = ln
                if tuple(x.lower() for x in fact) not in seen:
                    seen.add(tuple(x.lower() for x in fact))
                    lines.append(fact)
            if facts:
                parts = _Line([parts[0], "; ".join(left)] + parts[2:])
                parts.raw = ln
        if parts[0].startswith("=") and len(parts) >= 4 and parts[1]:
            # "= vLLM | setting MTP | 3 | 2026-09-08": a fact line with the
            # registration marker copied onto it (registrations have 3 columns).
            parts[0] = parts[0].lstrip("= ").strip()
        if len(parts) >= 5 and parts[1].lower() in ("attribute", "attr") and not parts[0].startswith("="):
            # "Rachel | attribute | current employer | TechCorp | since ...": the
            # template's column name copied in; the label and value follow it.
            del parts[1]
        if len(parts) == 3 and not parts[0].startswith("=") and all(parts):
            # "Sam | fitness activity | spin classes at the gym": a fact with the
            # date column left off (DeepSeek-V4.1-Flash: 2% of its note lines, 7% of
            # its chat lines, 10-17). It still states the fact: dated like "-".
            parts.append("-")
        if len(parts) >= 4 and not parts[0].startswith("=") and base_name(parts[0]) != parts[0]:
            # "box-7 (b7, metrics box) | port | 9100": the thing as the reference list
            # shows it, names in brackets. The store files it under box-7 anyway; the
            # checks here (repeats, backdating) need that name too.
            parts[0] = base_name(parts[0])
            displayed += 1
        if len(parts) >= 4 and not parts[0].startswith("="):
            # The prompt shows "<since YYYY-MM-DD>" and the model mostly copies the
            # word: "since 2026-09-24". Read the date, or "-" when there is none.
            d = re.sub(r"^since\s*", "", parts[3], flags=re.I).strip()
            parts[3] = d if re.fullmatch(r"\d{4}-\d\d-\d\d", d) else "-"
            kv = re.fullmatch(r"([A-Za-z_][\w.]*)=(\S+)", parts[2])
            if parts[1].lower() == "status" and kv:
                # "Flash-Next | status | ABLIT=0": a setting, not a status (the
                # next status line would overwrite it, and "setting ABLIT" holds 1).
                parts[1], parts[2] = f"setting {kv.group(1)}", kv.group(2)
        key = tuple(x.lower() for x in parts)
        if key not in seen:
            seen.add(key)
            lines.append(parts)
    st = {"duplicates": len(raw) - len(lines), "conflicts": 0}
    if stuck:
        st["loop_dropped"] = len(stuck)
    if displayed:
        st["display_names"] = displayed
    said: dict[tuple, int] = {}   # how often each (thing, attribute, value) was written
    for ln in raw:
        c = [x.strip().lower() for x in ln.strip("`").split("|")]
        if len(c) >= 3:
            k = (c[0].lstrip("= ").strip(), c[1], c[2])
            said[k] = said.get(k, 0) + 1
    groups: dict[tuple, list[int]] = {}
    for i, parts in enumerate(lines):
        if not parts[0].startswith("=") and len(parts) >= 4:
            groups.setdefault((parts[0].lower(), parts[1].lower(), parts[3]), []).append(i)
    drop = set()
    for idx in groups.values():
        values = {lines[i][2].lower() for i in idx}
        if len(values) > MAX_VALUES:
            drop.update(idx)
        elif len(values) > 1 and any(said.get((lines[i][0].lower(), lines[i][1].lower(), lines[i][2].lower()), 1) > 1
                                     for i in idx):
            # A loop flipping between values (each said many times): its order and
            # flags mean nothing; keep the plain lines in the order written.
            drop.update(i for i in idx if len(lines[i]) > 4 and lines[i][4].lower() == "correction")
        elif len(values) > 1:
            # Said once each, "$350,000" then "$400,000 | correction": the flagged
            # value is the one the text ends on. Apply it last, as plain news
            # (dropping the line lost the $400,000).
            for i in idx:
                if len(lines[i]) > 4 and lines[i][4].lower() == "correction":
                    moved = _Line(lines[i][:4])
                    moved.raw = lines[i].raw
                    lines.append(moved)
                    drop.add(i)
        if len(values) > 1:
            st["conflicts"] += len(values)
    # "Hermes | verify_on_stop | false | 09-28" next to "... | true | 09-27 | correction":
    # the model flags the OLD value as the correction and the store would mark the
    # new one never true. When a session gives one fact several values, its dates
    # order them; a correction flag there is unreliable, so it is dropped.
    per_fact: dict[tuple, set] = {}
    for i, parts in enumerate(lines):
        if i not in drop and not parts[0].startswith("=") and len(parts) >= 4:
            per_fact.setdefault((parts[0].lower(), parts[1].lower()), set()).add(parts[2].lower())
    for i, parts in enumerate(lines):
        if (len(parts) > 4 and parts[4].lower() == "correction" and not parts[0].startswith("=")
                and len(per_fact.get((parts[0].lower(), parts[1].lower()), ())) > 1):
            lines[i] = parts[:4]
            st["unflagged"] = st.get("unflagged", 0) + 1
    # "Notely | build host | buildbox | 09-17" next to "... | Node 1 | - | correction":
    # an undated value beside a dated one for the same fact is background (often
    # the old value); with the session's date it would come out newest.
    dated: dict[tuple, set] = {}
    for i, parts in enumerate(lines):
        if i not in drop and not parts[0].startswith("=") and len(parts) >= 4 and parts[3] != "-":
            dated.setdefault((parts[0].lower(), parts[1].lower()), set()).add(parts[2].lower())
    for i, parts in enumerate(lines):
        if (i not in drop and not parts[0].startswith("=") and len(parts) >= 4 and parts[3] == "-"
                and dated.get((parts[0].lower(), parts[1].lower()), {parts[2].lower()}) - {parts[2].lower()}
                and parts[2].lower() not in dated[(parts[0].lower(), parts[1].lower())]):
            drop.add(i)
            st["undated_beside_dated"] = st.get("undated_beside_dated", 0) + 1
    return [x for i, x in enumerate(lines) if i not in drop], st


# Counts a split session adds up from its halves.
SUMMED = ("prompt_tokens", "completion_tokens", "facts", "things", "bad", "repeats", "refused", "held", "duplicates",
          "conflicts", "repaired", "restated", "events", "followed_up", "ungrounded", "copied")


def _halves(text: str) -> list[str]:
    """Split at the line nearest the middle (a "--- file" or heading line when one
    is close); the second half keeps the file header it continues."""
    lines = text.splitlines(keepends=True)
    if len(lines) < 2:
        mid = text.rfind(" ", 0, len(text) // 2) + 1 or len(text) // 2
        return [text[:mid], text[mid:]]
    total, run, best = len(text), 0, (None, None)
    for i in range(1, len(lines)):
        run += len(lines[i - 1])
        cost = abs(run - total / 2) - (total / 10 if lines[i].startswith(("--- ", "#")) else 0)
        if best[0] is None or cost < best[0]:
            best = (cost, i)
    i = best[1]
    first, second = "".join(lines[:i]), "".join(lines[i:])
    header = next((x for x in reversed(lines[:i]) if x.startswith("--- ")), "")
    if header and not second.startswith("--- "):
        second = header + second
    return [first, second]


def _storable(p: list[str]) -> bool:
    """Would the store take this fact line's label and value (the checks remember makes first)?"""
    a = strip_window(html.unescape(p[1]))
    return not (value_problem(html.unescape(p[2]), a) or attribute_problem(a))


def _same(a: str, b: str) -> bool:
    return a.strip().lower().removeprefix("v") == b.strip().lower().removeprefix("v")


class Migrator:
    def __init__(self, out: Path, lean: bool = True, owner: str = "", prompt: str = "", chat: bool = False,
                 items: "_items.Items | None" = None, notes_prompt: str = "v3", split: bool = True,
                 followup: bool = True, temperature: float | None = None, missed: bool = False,
                 ground: str | None = "auto"):
        """lean: the prompt without the 'mentioned in passing' rules, the best
        measured import (10-07 slice replays: 23 gold on a current line, 3 stale,
        vs 21/3 full).
        items: before a new value replaces one under the same label, ask it
        whether it is another one of the same kind (deltos/items.py); off
        (None) by default: every new value replaces, as before.
        notes_prompt: "v3" (default since 10-18) fences what memory holds and adds a
        worked example; "v1" is the first notes prompt, "v4" is v3 plus what to keep
        first. Of the 71 facts (bench/importtest.py): DeepSeek v1 62 / v3 61 / v4 58,
        GLM 55 / 60 / 60, and only v1 saved forbidden lines (2 on each); v3 writes a
        third fewer lines than v1 on DeepSeek (1,862 vs 2,740), 9% fewer on GLM
        (Flash-Next 10-14: 49 vs 54 facts, the one model where v1 led). split: a
        cut-off output is retried
        as halves (SPLIT_DEPTH), so the facts past the cut are not lost.
        followup: an answer that only registers things gets one more turn for the facts.
        ground: which grounding rule a unit's lines must pass (deltos/ground.py): "auto" = "chat"
        for a conversation, "note" otherwise (decided per unit: a replay may switch self.chat);
        None = off. Only a unit with its text is checked: a replay without it applies as before."""
        self.ground = ground
        self.notes_prompt = notes_prompt
        # The sampling temperature of the import's own calls (reading a note, repairing, follow-up);
        # None sends 0. GLM-5.3-Flash at 0 (10-19, 20 import-test pieces): 7 loops, 1,973 lines, 70 min,
        # 60 of 71 hand facts. At 0.6, three draws: 1-2 loops, 729-1,078 lines, 54-62 facts, 7-12 min;
        # blind, 30 of 40 lines worth keeping instead of 15. At 0.3: 4-6 loops, 1,399-1,863 lines,
        # 50-59 facts. At 1.0: 0 loops, 48 facts. Set per model in [import] temperature.
        self.sample = {} if temperature is None else {"temperature": temperature}
        self.split = split
        self.followup = followup
        self.missed = missed   # the missed pass (MISSED) after each answer that finished
        self.max_tokens = MAX_TOKENS
        self.store = Store(out)
        self.lock = threading.Lock()
        self.lean = lean
        self.owner = owner   # whose memory this is: "I" and "my" in a text are them
        self.prompt = prompt  # another source's prompt ({day}, {known}): chats instead of notes
        self.chat = chat or bool(prompt)   # the source is a conversation: it speaks about now
        self.items = items
        self._said: dict[tuple, tuple] = {}   # with items: where each value was said (session, words)

    def known(self, text: str) -> str:
        """Only the things this text mentions (and the owner, when it speaks in
        the first person), each with its core lines and the entries whose label
        words the text says: a list of every name memory has grows to tens of
        thousands of tokens and still doesn't say what memory believes. Showing
        "Short stories written: 4" is what makes the model write 7 there, not
        under a new label."""
        tl = re.sub(r"[\s_-]+", " ", text.lower())   # name keys fold - _ and spaces the same way
        said = {_stem(w) for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 2}
        with self.lock:
            s = self.store
            hits: dict[str, int] = {}
            for key, sl in s.by_name.items():
                if len(key) > 2:
                    n = len(re.findall(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", tl))
                    if n:
                        hits[sl] = hits.get(sl, 0) + n
            mine = s.by_name.get(_key(self.owner)) if self.owner and FIRST_PERSON.search(text) else None
            if mine:
                hits[mine] = max(hits.values(), default=0) + 1
            out = []
            for sl in sorted(hits, key=lambda x: (-hits[x], x))[:40]:   # the things the text is most about
                t = s.find(s.label(sl).split(" (")[0])
                if t is None:
                    continue
                def fit(k):
                    words = [w for w in re.findall(r"[a-z]+", k.lower()) if len(w) > 2 and w not in STOP]
                    n = sum(_stem(w) in said for w in words)
                    return n if words and 2 * n >= len(words) else 0
                ranked = sorted((k for k in t.attrs if k in CORE or fit(k)), key=lambda k: -fit(k))
                facts = [f"{t.attrs[k].name}: {t.attrs[k].value}" for k in ranked[:KNOWN_ENTRIES]]
                out.append(s.label(sl) + (" | " + "; ".join(facts) if facts else ""))
        return "\n".join(out) or "(none of them yet)"

    def repair(self, refused: list[tuple[str, str]]) -> tuple[str, dict]:
        """One call: the refused lines with the store's reasons, rewritten."""
        known = self.known("\n".join(ln for ln, _ in refused))
        msg, usage = chat([{"role": "user", "content": REPAIR_PROMPT.format(
            refused="\n".join(f"{ln}  <- {why}" for ln, why in refused), labels="\n" + known)}],
            max_tokens=MAX_TOKENS if thinking_stays_on() else REPAIR_TOKENS, extra={**thinking(False), **self.sample})
        return msg.get("content") or "", usage

    def _repair_lines(self, raw: str, refused: list[tuple[str, str]], st: dict) -> list[list[str]]:
        """A repair rewrites refused lines; it may not bring in a thing that neither memory nor
        those lines named. 10-19: GLM's repairs made 15 new things out of words in the refused
        lines ("confirm.log | location | out/" came back as a thing called "confirm", and "game",
        "Plan", "softeners" the same way)."""
        named = {_key(base_name(ln.split("|")[0].strip().lstrip("= ").strip())) for ln, _ in refused}
        keep = []
        with self.lock:
            for p in _plan(raw, False)[0]:
                thing = base_name(p[0].lstrip("= ").strip())
                if p[0] == "+" or _key(thing) in named or self.store.find(thing) is not None:
                    keep.append(p)
                else:
                    st["repair_new_dropped"] = st.get("repair_new_dropped", 0) + 1
        return keep

    def session(self, name: str, day: str, text: str, created: str = "", git_mode: bool = False,
                replay: dict | None = None, redo_repair: bool = False, _depth: int = 0,
                _thinking: bool = False) -> dict:
        """One model call, then the store. `replay` = a logged session: apply
        its recorded output again, no model (tests store/import changes on the
        exact same model output). A cut-off output (finish_reason "length": the
        model looped, or the text held more facts than the budget; or an empty
        answer after reasoning: the budget went to the thought) keeps what it said
        and the text is asked again in halves, with twice the budget when it was
        thinking, logged under "subs" so a replay applies them too."""
        off = {**thinking(False), **self.sample} if replay is None else {}
        # A server that can't turn thinking off spends part of the budget on the thought.
        budget = self.max_tokens * (3 if replay is None and thinking_stays_on() else 1) * (2 if _thinking else 1)
        if replay is not None:
            msg = {"content": replay.get("raw", "")}
            usage = {k: replay.get(k, 0) for k in ("prompt_tokens", "completion_tokens", "finish_reason", "reasoning_tokens", "loop")}
            usage["loop_body"] = replay.get("loop_body", [])
            if "call_tokens" in replay:   # this call's own output (completion_tokens adds the repair and the halves)
                usage["completion_tokens"] = replay["call_tokens"]
            budget = replay.get("budget", MAX_TOKENS)
        elif self.prompt:
            msgs = [{"role": "system", "content": self.prompt.format(day=day, known=self.known(text))},
                    {"role": "user", "content": text}]
            msg, usage = chat(msgs, max_tokens=budget, extra=off, stop=_repeating)
            msg = _unloop(msg, usage)
        else:
            source = (f"These lines were added to memory notes on {day}." if git_mode else
                      f"The note was written {created or day} and last edited {day}.")
            fmt = dict(source=source, day=day, year=day[:4], created=created or day, known=self.known(text),
                       **LEAN_PROMPT if self.lean else FULL_PROMPT)
            msgs = [{"role": "system", "content": {"v1": PROMPT_V1, "v4": PROMPT_V4}.get(self.notes_prompt, PROMPT).format(**fmt)},
                    {"role": "user", "content": f"Note {name}:\n\n{text}"}]
            msg, usage = chat(msgs, max_tokens=budget, extra=off, stop=_repeating)
            msg = _unloop(msg, usage)
        cut = cut_off(msg, usage, budget)
        capped = bool(cut)
        reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or usage.get("reasoning_tokens") or 0
        st = {"note": name, "day": day, "prompt_tokens": usage.get("prompt_tokens", 0),
              "completion_tokens": usage.get("completion_tokens", 0), "facts": 0, "things": 0, "bad": 0,
              "repeats": 0, "refused": 0, "capped": capped}
        st["call_tokens"] = usage.get("completion_tokens", 0)
        if cut:
            st["cut_off"] = cut   # "length" or "thinking": stopped before it was done, never "no facts"
        if usage.get("finish_reason"):
            st["finish_reason"] = usage["finish_reason"]
        if reasoning:
            st["reasoning_tokens"] = reasoning
        if budget != MAX_TOKENS:
            st["budget"] = budget
        st["raw"] = msg.get("content") or ""   # kept in the log: what the model said, before the store judged it
        loop = max(Counter(x.strip() for x in st["raw"].splitlines() if x.strip()).values(), default=0)
        if cut == "repeat":   # stopped by the repeat guard: the raw kept is the answer without its loop
            loop = usage.get("loop") or REPEAT_STOP
            st["loop_body"] = usage.get("loop_body", [])
        if loop >= LOOP_REPEATS or cut == "repeat":
            st["loop"] = loop   # the most one line was said
        lines, plan = _plan(st["raw"], capped and cut != "repeat")   # a stopped answer ends on a whole line
        st.update(plan)
        refused = self._apply(lines, st, day, created, text, name)
        # Lines the store refused get one more try, with its reasons.
        if replay is not None and not redo_repair:
            # The logged repair answered the lines refused back then; when the
            # store takes them now, re-applying it only adds its guesses.
            st["repair_raw"] = replay.get("repair_raw", "") if refused else ""
        elif refused:
            st["repair_raw"], usage = self.repair(refused)
            st["prompt_tokens"] += usage.get("prompt_tokens", 0)
            st["completion_tokens"] += usage.get("completion_tokens", 0)
        if st.get("repair_raw"):
            before = st["facts"]
            self._apply(self._repair_lines(st["repair_raw"], refused, st), st, day, created, text, name)
            st["repaired"] = st["facts"] - before
        # Names only: some answers register every thing ("= ..." lines) and stop
        # without one fact (Flash-Next and GLM both, 10-14). Ask once for the facts.
        names = sum(1 for p in lines if p[0].startswith("="))
        if replay is not None:
            st["followup_raw"] = replay.get("followup_raw", "")
        elif (self.followup and not capped and names >= NAMES_ONLY_MIN
              and not any(not p[0].startswith(("=", "+")) for p in lines)):
            fmsg, usage = chat(msgs + [{"role": "assistant", "content": st["raw"]},
                                       {"role": "user", "content": NAMES_ONLY}], max_tokens=budget, extra=off,
                              stop=_repeating)
            st["followup_raw"] = _unloop(fmsg, usage).get("content") or ""
            st["prompt_tokens"] += usage.get("prompt_tokens", 0)
            st["completion_tokens"] += usage.get("completion_tokens", 0)
        if st.get("followup_raw"):
            st["names_only"] = names
            before = st["facts"]
            self._apply(_plan(st["followup_raw"], False)[0], st, day, created, text, name)
            st["followed_up"] = st["facts"] - before
        # The missed pass: an answer that finished gets one more turn for what it left out.
        if replay is not None:
            st["missed_raw"] = replay.get("missed_raw", "")
        elif self.missed and not capped and lines:
            gmsg, usage = chat(msgs + [{"role": "assistant", "content": st["raw"]},
                                       {"role": "user", "content": MISSED}], max_tokens=budget, extra=off,
                               stop=_repeating)
            gmsg = _unloop(gmsg, usage)
            st["missed_raw"] = gmsg.get("content") or ""
            st["missed_cut"] = cut_off(gmsg, usage, budget)
            st["prompt_tokens"] += usage.get("prompt_tokens", 0)
            st["completion_tokens"] += usage.get("completion_tokens", 0)
        if st.get("missed_raw"):
            before = st["facts"]
            self._apply(_plan(st["missed_raw"], st.get("missed_cut") not in (None, "", "repeat"))[0],
                        st, day, created, text, name)
            st["missed_added"] = st["facts"] - before
        # The cut. A replay applies the logged halves; a live call asks for them.
        if replay is not None:
            # With the unit's text, the halves are cut again from it the way the live run cut
            # them, so they replay grounded and with receipts, as they ran.
            logged = replay.get("subs") or []
            texts = _halves(text) if text and len(logged) == 2 else [""] * len(logged)
            pieces = [dict(r, raw_text=h) for r, h in zip(logged, texts)]
        elif capped and self.split and _depth < SPLIT_DEPTH and len(text) >= MIN_SPLIT:
            pieces = [{"raw_text": h} for h in _halves(text)]
        else:
            pieces = []
        if capped and not pieces:
            st["lost"] = 1   # cut off and not asked again: facts past the cut are missing
        subs = []
        for piece in pieces:
            sub = self.session(name, day, piece["raw_text"], created, git_mode,
                               replay=piece if replay is not None else None, redo_repair=redo_repair, _depth=_depth + 1,
                               _thinking=_thinking or bool(reasoning))
            subs.append(sub)
            for k in SUMMED:
                if sub.get(k):
                    st[k] = st.get(k, 0) + sub[k]
        if subs:
            st["subs"] = subs
            st["lost"] = sum(x.get("lost", 0) for x in subs)
        return st

    def _apply(self, lines: list[list[str]], st: dict, day: str, created: str, text: str = "",
               name: str = "") -> list[tuple[str, str]]:
        """Apply planned lines to the store; returns the lines it refused, with why.
        text and name are this session's own (several sessions run at once)."""
        refused = []
        # The grounding rule for this unit (deltos/ground.py), decided per call: a replay may
        # switch self.chat row by row. Without the unit's text there is nothing to check against.
        g = (("chat" if self.chat else "note") if self.ground == "auto" else self.ground) if text else None
        ok = {id(p): ground.grounded(p[2], text, g, day) for p in lines
              if g and not p[0].startswith(("=", "+")) and len(p) >= 4}
        if g == "chat" and ok:
            # A chat's rule checks only its numbers, paths and dates (speech paraphrases), so a
            # worded value memory's reference states and the chat never does passed it: memory's
            # own value copied back, often onto another thing (10-19: 21 of 25 wrong chat lines).
            ref = self.known(text)
            for p in lines:
                if ok.get(id(p)) and ground.stated(p[2], ref) and not ground.stated(p[2], text, day):
                    ok[id(p)] = False
                    st["copied"] = st.get("copied", 0) + 1
        with self.lock:   # one store, several sessions
            s = self.store
            s.today = day
            # Register a thing only if a fact line about it can be stored: no empty profiles.
            # (10-19: GLM registered "= 10-16 | | date" and "= 1339 tests | | test" with one
            # fact line each that memory refused; 27 of the night's 243 new things held nothing.)
            # Grounded, a thing also needs a fact the text states other than its kind: "X | kind |
            # service" alone was 33 lines of DeepSeek's logged import-test run and 14 of one GLM run
            # (bench/groundcheck.py, 10-19), in whatever order the lines came.
            about = {_key(p[0]) for p in lines if not p[0].startswith("=") and len(p) >= 4 and _storable(p)
                     and (not g or (ok[id(p)] and _attr_key(p[1]) != "kind"))}
            # Registered as a kind of value: not a new thing, and no fact line makes it one.
            given = {}
            for p in lines:
                if p[0].startswith("=") and len(p) > 2 and _key(p[2]) in VALUE_KINDS:
                    nm = base_name(p[0].lstrip("= ").strip())
                    if s.find(nm) is None:
                        given[_key(nm)] = p[2].strip()
            values: dict[tuple, set] = {}
            for p in lines:
                if not p[0].startswith("=") and len(p) >= 4:
                    values.setdefault((_key(p[0]), _attr_key(p[1])), set()).add(_key(p[2]))
            for parts in lines:
                ln = "|".join(parts)
                try:
                    if parts[0] == "+":
                        when = parts[1] if parts[1] != "-" and parts[1] <= day else day
                        if s.note(parts[2], when).startswith("logged"):
                            st["events"] = st.get("events", 0) + 1
                        continue
                    if parts[0].startswith("=") and len(parts) >= 2 and not (
                            {_key(parts[0].lstrip("= "))} | {_key(x) for x in parts[1].split(";")}) & about:
                        continue
                    if parts[0].startswith("=") and _key(base_name(parts[0].lstrip("= ").strip())) in given:
                        st.setdefault("refusals", []).append(
                            f"{ln[:160]} -> not created: given as a {parts[2].strip()}, which is a value, not a thing")
                        continue
                    if parts[0].startswith("=") and len(parts) >= 2:
                        names, kind = [x for x in parts[1].split(";") if x.strip(" -")], parts[2] if len(parts) > 2 else ""
                        # The kind in the names slot ("= Helsing | kind: company"): it is the kind.
                        for x in list(names):
                            m = re.fullmatch(r"\s*(?:kind|type)\s*:\s*(.+)", x, re.I)
                            if m or _key(x) in KINDS:
                                names.remove(x)
                                kind = kind or (m.group(1) if m else x).strip()
                        if g == "note":
                            # A note names what it is about: a new thing and each new name it gets
                            # are in its text (a name from the reference list is memory's, not news).
                            nm = parts[0].lstrip("= ").strip()
                            have = s.find(nm)
                            if have is None and not ground.named(nm, text):
                                st.setdefault("refusals", []).append(
                                    f"{ln[:160]} -> not created: the text never names {base_name(nm)}")
                                continue
                            for x in [x for x in names if not ground.named(x, text)]:
                                owner = have or s.find(x)
                                if not (owner and _key(base_name(x)) in {_key(n) for n in [owner.name, *owner.aliases]}):
                                    names.remove(x)
                                    st["alias_ungrounded"] = st.get("alias_ungrounded", 0) + 1
                        s.name(parts[0].lstrip("= ").strip(), names, kind)
                        st["things"] += 1
                    elif len(parts) >= 4 and parts[0] and parts[1]:
                        dated = bool(re.fullmatch(r"\d{4}-\d\d-\d\d", parts[3]))
                        if g and not ok.get(id(parts), True):
                            # The unit doesn't state this value: the model's guess, or memory's own
                            # value copied back from the reference list. Not a refusal to repair or
                            # to set aside: no rewording makes the text say it.
                            st["ungrounded"] = st.get("ungrounded", 0) + 1
                            st.setdefault("refusals", []).append(f"{ln[:160]} -> not recorded: not in the text")
                            continue
                        t = s.find(parts[0])
                        if t is None and _key(parts[0]) in given:
                            st["refused"] += 1
                            st.setdefault("refusals", []).append(
                                f"{ln[:160]} -> not created: {parts[0]} was given as a {given[_key(parts[0])]}, a value")
                            continue
                        if g and t is None and _attr_key(parts[1]) == "kind" and _key(parts[0]) not in about:
                            st["kind_only"] = st.get("kind_only", 0) + 1
                            st.setdefault("refusals", []).append(
                                f"{ln[:160]} -> not created: a kind alone doesn't make a thing")
                            continue
                        if g == "note" and t is None and not ground.named(parts[0], text):
                            # (A chat needs no such check: the owner's own facts name no thing in it.)
                            st["unnamed"] = st.get("unnamed", 0) + 1
                            st.setdefault("refusals", []).append(
                                f"{ln[:160]} -> not created: the text never names {parts[0]}")
                            continue
                        a = next((v for k, v in t.attrs.items() if _attr_key(k) == _attr_key(parts[1])), None) if t else None
                        if not dated and a and any(_same(parts[2], p.value) for p in a.trail if not p.wrong):
                            st["repeats"] += 1   # an old value mentioned again, with no date: not news
                            continue
                        mistake = (next((p for p in a.trail if p.wrong and p.by and _same(parts[2], p.value)), None)
                                   if a else None)
                        if mistake and not (dated and parts[3] > mistake.end):
                            # A value a person or agent marked as recorded by mistake, said
                            # again by a note (undated, or dated before the correction): the
                            # note still says it, it isn't news (10-16: a plan doc filed as
                            # Deltos's location came back after it was corrected). One dated
                            # after the correction can be a real change. An import's own
                            # corrections don't hold: replaying a live store's 581 sessions, some were
                            # wrong (gbrain "active", the wiki's location). A person or agent
                            # saving the value directly is never stopped here.
                            st["known_mistakes"] = st.get("known_mistakes", 0) + 1
                            st.setdefault("refusals", []).append(
                                f"{ln[:160]} -> not recorded: {parts[2]} was recorded by mistake "
                                f"(corrected {mistake.end})")
                            continue
                        if (self.chat and dated and a and a.since and parts[3] < a.since <= day
                                and len(values.get((_key(parts[0]), _attr_key(parts[1])), ())) == 1
                                and not _same(parts[2], a.value)):
                            # "I recently went to Paris" (05-28, model dated it 04-28) after
                            # "recent trip: Hawaii" (05-26): one value, said later than the
                            # current one, is the newer news; its guessed start would file
                            # it under the value it replaces.
                            parts = _Line(parts[:3] + [day] + parts[4:])
                            st["backdate_now"] = st.get("backdate_now", 0) + 1
                        fix = len(parts) > 4 and parts[4].lower() == "correction"
                        if fix and dated and a and a.since and parts[3] > a.since:
                            # A value wrong all along was right since before the wrong one
                            # was recorded; one that became true later is an update ("Team
                            # size: 5" after 4 is not 4 "never true").
                            fix = False
                            st["unflagged"] = st.get("unflagged", 0) + 1
                        since = parts[3] if dated else (created or day)
                        held = (a is not None and not _same(parts[2], a.value) and _key(parts[2]) != _key(a.value)
                                and not any(_key(parts[2]) == _key(x.value) for x in a.also)
                                and (fix or since >= a.since) and trust_hold(a, since, "", False))
                        if held:
                            # Trust order: a value a person or agent set (or confirmed) stands
                            # against a note that isn't dated after it. Not a refusal to repair.
                            st["held"] = st.get("held", 0) + 1
                            st.setdefault("refusals", []).append(f"{ln[:160]} -> not changed: {a.value} stays, {held}")
                            continue
                        another = bool(self.items is not None and a is not None and not fix
                                       and self._another(t, a, parts, since, text, name))
                        r = s.remember(parts[0], parts[1], parts[2], since=since,
                                       correction=fix, create=True, confirm=False, another=another)
                        if r.startswith("not changed:"):
                            st["held"] = st.get("held", 0) + 1
                            st.setdefault("refusals", []).append(f"{ln[:160]} -> {r[:160]}")
                            continue
                        if not r.startswith("not ") and text:
                            # A receipt: the sentence this value was read from (a value
                            # said again keeps the receipt it has). From a note, the words
                            # around the line that states it, at most SAID_CHARS of them.
                            words = (ground.receipt(text, parts[2]) if g == "note"
                                     else _items.evidence(text, parts[2], parts[1]))
                            if words:
                                s.receipt(parts[0], parts[1], parts[2], words, day, keep=": still " in r)
                        if self.items is not None and not r.startswith("not "):
                            t2 = s.find(parts[0])
                            k = (_key(t2.name if t2 else parts[0]), _attr_key(parts[1]), _key(parts[2]))
                            self._said.setdefault(k, (name, _items.evidence(text, parts[2], parts[1])))
                        st["refused" if r.startswith("not ") else "facts"] += 1
                        if ": still " in r:
                            st["restated"] = st.get("restated", 0) + 1   # no change: memory had it
                        if r.startswith(("not recorded: not a setting", "not recorded: not a status")):
                            # Not a current fact, still about the thing: keep it beside the profile.
                            s.aside(parts[0], [f"- {parts[1]}: {parts[2]} ({parts[3] if dated else created or day})"])
                        if r.startswith("not "):
                            st.setdefault("refusals", []).append(f"{ln[:160]} -> {r[:100]}")
                            if not r.startswith(("not recorded: not a setting", "not recorded: not a status")):
                                refused.append((getattr(parts, "raw", "") or ln, r))
                    elif ln.strip():
                        st["bad"] += 1
                        st.setdefault("malformed", []).append(ln[:160])
                except Exception as e:   # a store bug must show up in the log, not just as a count
                    st["bad"] += 1
                    st.setdefault("exceptions", []).append(f"{ln[:120]} -> {e!r}"[:300])
        return refused


    def _another(self, t, a, parts, since: str, text: str, name: str) -> bool:
        """Would this line move a different value into the trail? Then ask the
        decider whether it is another one of the same kind instead."""
        value = parts[2]
        if _items.single(parts[1]) or _same(value, a.value) or _key(value) == _key(a.value):
            return False
        if any(_key(value) == _key(x.value) for x in a.also):
            return False
        if any(_key(value) == _key(p.value) for p in a.trail if not p.wrong):
            return False   # a value memory already had: history, not a new item
        old = self._said.get((_key(t.name), _attr_key(a.name), _key(a.value)), ("", ""))
        if not old[1] and a.said:
            # A value stored while the check was off has no words here, but its receipt
            # in the note is the same sentence.
            old = ("", _items.evidence(a.said[0], a.value, a.name) or a.said[0])
        return self.items.decide({"thing": t.name, "label": a.name, "old": a.value, "old_since": a.since,
                                  "old_ev": old[1], "new": value, "new_since": since,
                                  "new_ev": _items.evidence(text, value, parts[1]),
                                  "old_sid": old[0], "new_sid": name})


def load_texts(texts) -> dict:
    """Each unit's text by "note#chunk" (and "note#chunk@upto" for a chat's later turns), from
    a migrate-texts.jsonl (one {"note", "chunk", "text", "upto"?} per line) or a json object."""
    if not texts:
        return {}
    if isinstance(texts, dict):
        return texts
    if not str(texts).endswith(".jsonl"):
        return json.loads(Path(texts).read_text())
    rows = [json.loads(l) for l in Path(texts).read_text().splitlines() if l.strip()]
    return {_unit(r): r["text"] for r in rows}


def _unit(r: dict) -> str:
    return f"{r['note']}#{r.get('chunk', 0)}" + (f"@{r['upto']}" if r.get("upto") is not None else "")


def replay(log: Path, out: Path, texts=None):
    """Rebuild a store from a logged run, in the order the sessions were applied.
    texts: each unit's text (load_texts), so the replay is grounded and leaves receipts
    the way the run did; without it the logged lines are applied as they are."""
    if (out / "notes").exists():
        raise SystemExit(f"{out} already has notes; replay into an empty folder")
    m = Migrator(out)
    rows = [json.loads(l) for l in open(log)]
    by_unit = load_texts(texts)
    with open(out / "migrate-log.jsonl", "w") as fh:
        for r in rows:
            st = m.session(r["note"], r["day"], by_unit.get(_unit(r), ""), r["day"], git_mode=True, replay=r)
            st["chunk"] = r.get("chunk", 0)
            fh.write(json.dumps(st) + "\n")
    keys = ("facts", "refused", "repeats", "duplicates", "conflicts", "bad")
    print(json.dumps({k: sum(json.loads(l).get(k, 0) for l in open(out / "migrate-log.jsonl")) for k in keys}))


def git_jobs(specs: list[str], skip: list[str], since: str = "", done: set | None = None) -> list[tuple]:
    """Sessions from note repos: specs are "repo:commit:label" (commit may be HEAD)."""
    jobs = []
    for spec in specs:
        repo, commit, prefix = spec.rsplit(":", 2)
        found = list(_git_jobs(Path(repo).expanduser(), commit, prefix, skip, since))
        of = Counter(name for _, name, _, _ in found)
        for day, name, i, text in found:
            if (name, i) not in (done or set()) and (name, None) not in (done or set()):
                jobs.append((day, day, name, i, text, of[name]))
    return sorted(jobs)


def folder_jobs(corpus: Path, skip: list[str], done: set | None = None) -> list[tuple]:
    """Sessions from a folder of notes (corpus/notes/*.md, dates from the notes or corpus/dates.json)."""
    gp = Path(corpus) / "dates.json"
    git = json.loads(gp.read_text()) if gp.exists() else {}
    jobs = []
    for p in (Path(corpus) / "notes").glob("*.md"):
        if any(s and s in p.name for s in skip):
            continue
        text = p.read_text(errors="replace")
        created, day = _dates(text, p.name, git)
        cs = _chunks(text)
        for i, c in enumerate(cs):
            if (p.name, i) not in (done or set()) and (p.name, None) not in (done or set()):
                jobs.append((created, day, p.name, i, c, len(cs)))
    return sorted(jobs)


def done_sessions(out: Path) -> set:
    """Resumable: a session per (note, chunk) is logged when finished, with how many pieces
    its note had ("of"). A note is done when all its pieces are; a note logged before "of"
    (10-19) is done as a whole, so a change in how notes are cut never imports old ones again.
    The set holds (note, chunk) pairs and (note, None) for whole notes."""
    log = Path(out) / "migrate-log.jsonl"
    if not log.exists():
        return set()
    done, of = set(), {}
    for r in map(json.loads, open(log)):
        done.add((r["note"], r.get("chunk", 0)))
        of[r["note"]] = max(of.get(r["note"], 0), r.get("of", 0)) if "of" in r else -1
    for n, k in of.items():
        if k < 0 or all((n, i) in done for i in range(k)):
            done.add((n, None))
    return done


REFUSED_WARN = 0.20   # Flash-Next's imports refused ~5% of lines; GLM's first night 44%


def _flags(st: dict) -> str:
    """What the nightly log line should shout about: lines lost, loops, splits."""
    f = []
    if st.get("capped"):
        f.append("CUT-OFF " + st.get("cut_off", "length"))
    if st.get("loop"):
        f.append(f"LOOP x{st['loop']}")
    if st.get("names_only"):
        f.append(f"NAMES-ONLY +{st.get('followed_up', 0)}")
    if st.get("subs"):
        f.append(f"SPLIT {len(st['subs'])}")
    if st.get("lost"):
        f.append(f"LOST {st['lost']} cut-off piece(s)")
    return (" " + " ".join(f)) if f else ""


def summary(rows: list[dict]) -> str:
    """One line for a run, with a WARNING when the model is not following the
    format: many refused lines, loops, or cut-offs that lost facts."""
    def tree(r):
        yield r
        for x in r.get("subs") or []:
            yield from tree(x)
    ok = [r for r in rows if "error" not in r]
    facts, refused = sum(r.get("facts", 0) for r in ok), sum(r.get("refused", 0) for r in ok)
    share = refused / max(1, facts + refused)
    loops = sum(1 for r in ok for x in tree(r) if x.get("loop"))
    cuts = Counter(x.get("cut_off", "length") for r in ok for x in tree(r) if x.get("capped"))
    lost = sum(r.get("lost", 0) for r in ok)
    split = sum(1 for r in ok if r.get("subs"))
    ungrounded = sum(r.get("ungrounded", 0) for r in ok)   # a row's count holds its halves' (SUMMED)
    line = (f"import summary: {len(ok)} sessions, {len(rows) - len(ok)} errors, facts {facts} "
            f"(restated {sum(r.get('restated', 0) for r in ok)}), refused {refused} ({share:.0%}), "
            f"loops {loops}, cut-offs {sum(cuts.values())} ({', '.join(f'{k} {v}' for k, v in sorted(cuts.items())) or '-'}), "
            f"split {split}, lost {lost}, ungrounded {ungrounded}")
    held = sum(r.get("held", 0) for r in ok)
    if held:
        line += f", held {held} (a person's or agent's value kept against a note not dated after it)"
    why = [w for w, bad in ((f"refused {share:.0%} of lines (> {REFUSED_WARN:.0%})", share > REFUSED_WARN),
                            (f"{loops} looped output(s)", loops), (f"{lost} cut-off piece(s) lost facts", lost),
                            (f"{len(rows) - len(ok)} session(s) failed", len(rows) > len(ok))) if bad]
    return line + (" | WARNING: " + "; ".join(why) + ": the model may not follow the import format" if why else "")


def load_said(path: Path) -> dict[tuple, tuple]:
    """Where each current value was said, kept beside a store built night by
    night: the items check needs the words behind the earlier value too, and
    that value was usually filed on an earlier night."""
    if not path.exists():
        return {}
    return {tuple(r[:3]): tuple(r[3:]) for r in json.loads(path.read_text())}


def save_said(path: Path, said: dict[tuple, tuple], store: Store) -> None:
    """Only values still current are kept: a past value is never the earlier
    side of a new event, so the file stays the size of memory."""
    keep = []
    for (tk, ak, vk), (sid, ev) in sorted(said.items()):
        t = store.find(tk)
        a = next((v for k, v in t.attrs.items() if _attr_key(k) == ak), None) if t else None
        if a and (vk == _key(a.value) or any(vk == _key(x.value) for x in a.also)):
            keep.append([tk, ak, vk, sid, ev])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(keep, indent=0))
    tmp.replace(path)


def run(out: Path, jobs: list[tuple], workers: int = 2, git_mode: bool = True, lean: bool = True, say=print,
        owner: str = "", notes_prompt: str = "v3", split: bool = True,
        items: "_items.Items | None" = None, said: Path | None = None, temperature: float | None = None,
        missed: bool = False) -> int:
    """Apply sessions to the store at out, logging each to migrate-log.jsonl.
    items: the items check (Migrator); said: where its evidence is kept between runs;
    temperature: the import calls' sampling temperature (None: 0)."""
    out = Path(out)
    m, lock, t0 = (Migrator(out, lean, owner, notes_prompt=notes_prompt, split=split, items=items,
                            temperature=temperature, missed=missed),
                   threading.Lock(), time.time())
    if items is not None and said:
        m._said = load_said(said)
    rows: list[dict] = []
    say(f"{len(jobs)} sessions")

    def go(job):
        created, day, name, i, text, *of = job   # of: how many pieces the note has
        for attempt in range(OUTAGE_TRIES):
            try:
                st = m.session(name, day, text, created, git_mode=git_mode)
                break
            except Exception as e:
                # Overnight the model can restart or get shed; wait it out rather
                # than fail every remaining session in seconds.
                st = {"note": name, "day": day, "error": repr(e)[:200], "attempts": attempt + 1}
                say(f"{day} {name}#{i} error ({attempt + 1}/{OUTAGE_TRIES}): {repr(e)[:120]}")
                if attempt + 1 < OUTAGE_TRIES:
                    time.sleep(OUTAGE_WAIT)
        st["chunk"] = i
        if of:
            st["of"] = of[0]
        with lock, open(out / ("migrate-log.jsonl" if "error" not in st else "migrate-errors.jsonl"), "a") as fh:
            fh.write(json.dumps(st) + "\n")
            rows.append(st)
            if "error" not in st:
                # The unit's text beside its row: a replay grounds the logged lines against it.
                with open(out / "migrate-texts.jsonl", "a") as tf:
                    tf.write(json.dumps({"note": name, "chunk": i, "text": text}) + "\n")
        say(f"{day} {name}#{i} things={st.get('things')} facts={st.get('facts')} bad={st.get('bad')} "
            f"repeats={st.get('repeats')} refused={st.get('refused')} dup={st.get('duplicates')} "
            f"conflicts={st.get('conflicts')}{_flags(st)} {round(time.time() - t0)}s")

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(go, jobs))
    if items is not None and said:
        save_said(said, m._said, m.store)
    if jobs:
        say(summary(rows))
    return len(jobs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="out/real/corpus")
    ap.add_argument("--out", default="out/real/migrated")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0)
    # News feeds (an agent's daily news brief, an arXiv digest) are ~22% of the text and
    # say nothing about the lab; backups repeat their note.
    ap.add_argument("--skip", default="ARCHIVE-index,intel/latest.md,arxiv-digest.md,.bak",
                    help="comma list of name parts to skip")
    ap.add_argument("--git", default="", help="replay history instead: repo:commit:prefix[,...]")
    ap.add_argument("--since", default="", help="git mode: only commits on or after YYYY-MM-DD")
    ap.add_argument("--replay", default="", help="a migrate-log.jsonl: re-apply its logged output, no model")
    ap.add_argument("--texts", default="", help="with --replay: the units' texts (migrate-texts.jsonl or json)")
    ap.add_argument("--full", action="store_true", help="the full prompt instead of the lean one")
    ap.add_argument("--notes-prompt", default="v3", choices=("v1", "v3", "v4"),
                    help="v3 (default) = fenced reference + worked example; v1 = the first notes prompt")
    ap.add_argument("--no-split", action="store_true", help="don't ask a cut-off output again in halves")
    args = ap.parse_args()
    if args.replay:
        replay(Path(args.replay), Path(args.out), args.texts or None)
        return
    skip, done = args.skip.split(","), done_sessions(Path(args.out))
    jobs = (git_jobs(args.git.split(","), skip, args.since, done) if args.git
            else folder_jobs(Path(args.corpus), skip, done))
    run(Path(args.out), jobs[: args.limit] if args.limit else jobs, args.workers, bool(args.git),
        lean=not args.full, say=lambda m: print(m, flush=True), notes_prompt=args.notes_prompt, split=not args.no_split)


if __name__ == "__main__":
    main()
