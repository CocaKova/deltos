"""Write path: the agent says what it learned, the store decides where it goes.

An agent never hand-edits a fact. It calls remember("TS-2", "location",
"the Annex closet", since="2026-05-09") and the store:
  - finds the thing by any of its names,
  - makes the new value the current line,
  - moves the old one to a dated "was:" trail under it (warm tier, last 2),
  - archives anything older in <thing>-history.md (cold tier, one [[link]]),
  - marks corrected values as never true instead of keeping them as history,
  - treats a repeat of the current value as a confirmation, not a change,
  - places a backdated value into the trail instead of over the current one,
  - keeps "another one" (another=True: a second item of the same kind, both
    true, deltos/items.py decides) beside the current line as "also current:",
  - keeps a value a person or agent set against lower-trust news that isn't
    dated after it (trust order: a person, then an agent, then a note import).

Markdown is the source of truth: every write parses the note and renders it
back, so people can read and edit memory with any editor.
"""
from __future__ import annotations

import difflib
import html
import json
import os
import shutil
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import fcntl
except ImportError:   # no advisory locks on this platform: one writer at a time is on the caller
    fcntl = None

PEOPLE = {"person", "people", "human", "client", "customer", "contact", "user"}   # kinds that never merge with things
WARM = 2          # past values kept under the current line
RETIRED = "retired"
VOLATILE = ("high", "low", "static")
TRASH = ".trash"           # forgotten things and entries, restorable (inside the memory folder, outside notes/)
FORGET_LOG = "forget-log.jsonl"
FORGET_PER_DAY = 10        # forgets one agent may make in a day; a person at the CLI has no budget
BIG_THING = 5              # entries at which an agent needs user_asked to forget a whole thing
HAND = "hand"              # the source of a person's own write (the CLI); an agent writes under its name
# Trust order: a person's word stands against an agent's and a note import's, an
# agent's against a note import's, unless the news is dated after it.
PERSON, AGENT, IMPORT = 2, 1, 0
COMMON = {"the", "service", "server", "project", "machine", "vendor", "team", "main", "new", "old"}


def slug(s: str) -> str:
    out = "".join(ch.lower() if ch.isalnum() else "-" for ch in s).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out


# A name identifies a thing. Versions, ports, sizes, paths, addresses and long
# phrases describe it instead, and as names they hijack unrelated questions.
NOT_A_NAME = re.compile(r"""^(
      v?\d+(\.\d+)+[a-z0-9.-]*            # 1.9.1, v2.17.2
    | :?\d+                               # 8890, :8890
    | .*\b\d+(\.\d+)?\s?(tb|gb|mb|kb)\b.* # 1 TB
    | .*[/~\\].*                          # paths, repos, urls
    | .*\b\d{1,3}(\.\d{1,3}){3}\b.*         # IPs
    | .*https?:.*
    | .*\s--?[a-z].*                         # commands with flags
)$""", re.X | re.I)


# A measurement, a time or a count is a value, not a name (10-19: GLM made "0.4 s", "511 tests",
# "pace 114 vs 137", "silences capped at 1 s" into things; 42 more were already in a live store,
# "15 min", "35.9%", "17:40 UTC"). No B among the units: "Qwen 27B" and "GLM 1B" are names.
UNITS = r"(?:%|s|sec|ms|µs|us|ns|min|mins|h|hr|hrs|d|wk|st|hz|khz|mhz|ghz|kb|mb|gb|tb|kib|mib|gib|tib|tok|tokens|px|fps|w|kw|v|°c|c|x)"
_NUM = r"[−+-]?\d[\d,]*(?:\.\d+)?"
_RANGE = rf"{_NUM}(?:\s*(?:–|—|-|\.\.|to)\s*{_NUM})?"
QUANTITY = re.compile(rf"""^(?:
      {_RANGE}\s?{UNITS}?                                   # 0.4 s, 15 min, 10%, 1.24 GiB, 60–1000 Hz, 5–6
    | \d{{1,4}}[-:/]\d{{1,2}}(?:[-:/]\d{{1,4}})?\b.*         # 09-27, 11:46, 2026-10-01, 05:00 import, 16:9 card
    | \d[\d,]*\s+[a-z]+s                                    # 511 tests, 12 sessions: a count
    | .*\d\s*vs\.?\s*[−+-]?\d.*                             # pace 114 vs 137: a comparison
    | [−+-]?\d+\.\d+[A-Za-z]{{0,2}}\s.*                      # starts with a decimal: 0.05–0.08 st median
    | {_RANGE}\s?{UNITS}\s.*                                # starts with an amount: 150–220 Hz tonal noise
    | .*\s{_RANGE}\s?{UNITS}                                # ends with one: silences capped at 1 s
)$""", re.X | re.I)


GENERIC = COMMON | {"it", "this", "that", "box", "app", "thing", "system", "we", "you", "me", "all", "kind"}
# Kinds of thing. Given as a name ("= Theater.kt | file"), one of these joins
# every later thing of that kind into the first: 14 companies became SambaNova.
KINDS = {"file", "files", "company", "person", "people", "repo", "repository", "commit", "entity", "model",
         "script", "timer", "container", "directory", "folder", "tool", "agent", "protocol", "component",
         "media", "document", "doc", "product", "org", "organization", "plugin", "skill", "machine", "device",
         "vm", "page", "note", "room", "channel", "group", "account", "branch", "task", "job", "cron", "config",
         "package", "library", "feature", "release", "setting", "settings", "unit", "process", "session",
         "website", "site", "api", "endpoint", "database", "db", "bot", "user", "key", "token", "pr", "issue"}


FILE_EXT = re.compile(r"\.[a-z][a-z0-9]{0,7}$", re.I)
TRAILING_NOTE = re.compile(r"^(?P<n>[^()]+?)\s*\([^()]*\)$")


def numbered_sibling(name: str, others: list[str]) -> str:
    """The other name `name` differs from only by a number ("Node 2" / "Node 1",
    "PR #5" / "PR #6", "Point-v18" / "Point-v19"): two things that share a
    maker or a series, not two names for one. "" when there is none."""
    num = lambda x: re.fullmatch(r"v?\d+[a-z]?", x) is not None
    a = [t for t in re.split(r"[\s\-_.#/]+", name.lower()) if t]
    for o in others:
        b = [t for t in re.split(r"[\s\-_.#/]+", o.lower()) if t]
        if len(a) == len(b) >= 2:
            diff = [i for i in range(len(a)) if a[i] != b[i]]
            if len(diff) == 1 and num(a[diff[0]]) and num(b[diff[0]]):
                return o
        # "Build Node 2" / "Node 1": the same word, numbered differently at the end
        if len(a) >= 2 and len(b) >= 2 and a[-2] == b[-2] and not num(a[-2]) \
                and num(a[-1]) and num(b[-1]) and a[-1] != b[-1]:
            return o
    return ""


def base_name(name: str) -> str:
    """"Brain (LLM)" is the thing Brain; the bracket only explains it. Not
    kept as an alias: words like "gateway" would match unrelated questions."""
    m = TRAILING_NOTE.match(name.strip())
    return m.group("n").strip() if m else name.strip()


def name_problem(name: str) -> str:
    n = name.strip()
    if not re.search(r"[^\W_]", n):
        return "empty"   # "-", "?", "n/a"-style placeholders would join every thing into one
    if n.lower() in ("n/a", "none", "unknown", "same"):
        return "empty"
    if "\n" in n:
        return "has a line break"
    if len(n) < 2:
        return "too short for a name"
    if n.startswith("-"):
        # "--no-sandbox", "--attempts": how a command is run. NOT_A_NAME's flag rule needs a word
        # before the flag, and 109 flags had become things in a live store (10-19).
        return "looks like a command flag (record it as a setting of the command), not a name"
    if re.search(r"\w['’]s\b.*\b[a-z]", n):
        # "Ava's plan", "Sam's action figure set": the possessive starts a phrase.
        # "Trader Joe's", "Sutton's Inc.", "Sam's Club" are names.
        return "is a phrase about something ('Ava's plan'), not a name"   # "S" would match inside every "Alex's"
    if re.match(r"(my|our|your|his|her|their) ", n):
        return "is what something is to someone ('my mom'), not a name: record it as relationship"
    if _key(n) in GENERIC or _key(n) in KINDS:
        return "too generic to be a name"
    if re.search(r"\w: ", n) or re.fullmatch(r"(kind|type|alias|name)\s*:.*", n, re.I):
        return "is a label ('kind: company'), not a name"
    if re.search(r"@[0-9a-f]{7,40}$", n, re.I):
        return "is a commit (where a fact was written), not a name"
    if not re.search(r"[^\W\d_]", n) and len(re.sub(r"\D", "", n)) >= 7:
        return "is a number (a phone, an id), not a name: record it as a fact of the thing"
    if NOT_A_NAME.match(n):
        return "looks like a value, not a name"
    if QUANTITY.match(n):
        return "is a measurement, a time or a count, not a name: record it as a value of the thing it measures"
    if len(n) > 48 or len(n.split()) > 6:
        return "too long for a name"
    if re.search(r"\s(v?\d+\.\d+(\.\d+)?)$", n) or re.search(r"(?<![\w.])v?\d+\.\d+\.\d+(?!\w|\.\d)", n) \
            or re.fullmatch(r"vc\d+", n, re.I):
        # "Notely 2.17.2 BACKUP FIX 10-04" is a release (a fact of Notely), not a name.
        return "has a version in it (record the version as a fact)"
    if any(ch in n for ch in "()[]=|*"):
        return "has brackets or symbols"
    if "_" in n and " " not in n and not FILE_EXT.search(n):
        return "looks like a setting key (record it as a setting of the thing it configures)"
    if re.fullmatch(r"[0-9a-f]{7,40}", n.lower()) and re.search(r"\d", n):
        return "looks like a commit hash"
    if n.startswith(".") or re.search(r"\.(md|bak|log|jsonl|db|tmp|orig)$", n, re.I):
        # Notes, backups and logs only say where text was. Scripts, units and
        # config files (gen_status.py, status.timer, config.yaml) are real things.
        return "looks like a note, backup or log file (record the facts on the thing they are about)"
    if re.search(r"(?<!\d)20\d{6}(?!\d)", n):
        return "has a date stamp in it, like a note or backup name (use the thing's own name)"
    return ""


MAX_VALUE_WORDS = 10

# Kind words (10-17, opt-in DELTOS_KINDS=1 until both models pass): an item's value
# can end with what kind of thing it is, "espresso machine from my sister [kitchen
# appliance]", so a question that asks by kind ("how many kitchen items") finds it.
# They are not the value: one value written with other kind words is the same value,
# and a value's length is counted without them.
ITEM_KINDS = os.environ.get("DELTOS_KINDS") == "1"
KIND_TAIL = re.compile(r"\s*\[([A-Za-z][A-Za-z ,'&-]{1,60})\]$")


def kind_words(value: str) -> list[str]:
    """The words of a value's kind tail ("[kitchen appliance]"), when kinds are on."""
    m = KIND_TAIL.search(value.strip()) if ITEM_KINDS else None
    return re.findall(r"[a-z]+", m.group(1).lower()) if m else []


def bare(value: str) -> str:
    """The value without its kind tail (kinds on), else the value."""
    return KIND_TAIL.sub("", value.strip()) if ITEM_KINDS else value


MAX_STATUS_WORDS = 4
# Words that join a phrase to what follows; a phrase never ends on one.
FUNCTION_WORDS = {"a", "an", "the", "at", "in", "on", "of", "to", "for", "from", "with", "within", "by", "vs", "via",
                  "per", "into", "than", "as", "and", "or", "but"}


CONFIG_KEY = re.compile(r"[_./=]|^-|[a-z][A-Z]|^[A-Z0-9_]{2,}$")   # a_b, a.b, a/b, -flag, camelCase, ALLCAPS


def junk_setting(attribute: str, value: str) -> bool:
    """'Setting 141 assistant turns: 27B run', 'Setting 09-07: date': a session
    log written as settings (the import's catch-all). A real setting is a
    config key, or a short label with a short value ('thinking: LOW')."""
    m = re.match(r"\s*setting\s+(.+)$", attribute, re.I)
    if not m or CONFIG_KEY.search(m.group(1).strip()):
        return False
    label = m.group(1).strip()
    return bool(re.search(r"\d", label)) or len(label.split()) > 3 or len(value.split()) > 6


def junk_status(value: str) -> bool:
    """A status is the state a thing is in, in a few words (live, paused,
    released, down). 'installed Notely 2.18.0', 'reduced from 1154 to 263
    lines', 'bot-dm-brake skipped (not armed)' are events: they replace the
    state ('PAUSED') and the next question about it finds an event."""
    v = re.sub(r"\s*\((since|confirmed)[^()]*\)\s*$", "", value).strip()
    v = re.sub(r"\b(20\d\d-)?\d\d-\d\d\b", "", v).strip()   # a date says when, not what
    words = v.split()
    # A state is a whole phrase: one that ends on "at", "within", "vs" is a piece cut out of a
    # sentence ("1 s | status | silences capped at", GLM 10-19), and a file name is where
    # something is, not what state it is in ("backoff | status | worker.py").
    cut = bool(words) and words[-1].lower() in FUNCTION_WORDS
    return (len(words) > MAX_STATUS_WORDS or bool(re.search(r"\d|[/~]", v)) or cut
            or (len(words) == 1 and bool(FILE_EXT.search(v))))


def value_problem(value: str, attribute: str = "") -> str:
    v = bare(value).strip()
    if not v:
        return "empty value"
    if "\n" in v:
        return "has a line break: record the short value; put the explanation in a note"
    if len(v.split()) > MAX_VALUE_WORDS or len(v) > 100:
        return (f"too long for a value (over {MAX_VALUE_WORDS} words): record the short value; "
                "put the explanation in a note")
    words, label = set(re.findall(r"[a-z0-9]+", v.lower())), set(re.findall(r"[a-z0-9]+", attribute.lower()))
    if words & label and words <= label | {"current", "previous", "my", "the"}:
        # "Employer: current employer", "Status: status": the label again, no value.
        return "the value only repeats the attribute: give the value itself"
    if junk_setting(attribute, v):
        return ("not a setting: a setting is a config key with a short value (compression.threshold_tokens: "
                "140000, thinking: low). Counts, timings and what happened in a run go in a note")
    if attribute.strip().lower() == "status" and junk_status(v):
        # Status is the state a thing is in. Events ("pushed round 4", "flagged
        # bugs on #123") churn it every day and bury the state under history.
        return ("not a status: a status is the state a thing is in, in 1-4 words without numbers or paths "
                "(live, paused, stopped, released). A version goes under version; what happened goes in a note")
    return ""


def attribute_problem(attribute: str) -> str:
    a = attribute.strip()
    if not a or not re.search(r"[^\W_]", a):
        return "empty attribute"
    if any(ch in a for ch in "\n*:,{}()[]|"):
        return "attribute has symbols in it (use plain words, e.g. \"latest release\")"
    if len(a) > 50 or len(a.split()) > 6:
        return "attribute too long (use a short label, e.g. \"latest release\")"
    if a.lower() in ("attribute", "value", "fact", "info", "note", "notes", "details", "detail", "description"):
        return f"{a!r} says nothing: name what the value is (status, host, version, ...)"
    if a.lower() in ("setting", "settings"):
        # The format's "setting <setting name>" with the name left off (10-19: 54 GLM lines like
        # "bar | setting | fewer confident mistakes than plain"): which setting is it?
        return "say which setting: \"setting <its name>\" (setting thinking: low)"
    return ""


def date_problem(d: str, today: str) -> str:
    """Dates are compared as text, so they must all be YYYY-MM-DD: "2026-9-5"
    sorts after "2026-10-01" and would quietly reorder history."""
    try:
        if not re.fullmatch(r"\d{4}-\d\d-\d\d", d):
            raise ValueError
        date.fromisoformat(d)
    except ValueError:
        return f"since {d!r} is not a date: give YYYY-MM-DD, or leave it out for today"
    if d > today and (date.fromisoformat(d) - date.fromisoformat(today)).days > 366:
        # A dated plan ("host Brisk from 01-25", said on 01-09) is a fact with its
        # date: refusing it made the writer log it as a note and lose the fact.
        return f"since {d} is more than a year after today ({today}): check the year"
    return ""


def _clean(text: str) -> str:
    return " ".join(text.split())


def _clean_source(text: str) -> str:
    # The meta group is "(since ...; confirmed ...; source: ...)": brackets or
    # semicolons inside a source would end it early and the line wouldn't parse.
    return _clean(text).replace("(", "[").replace(")", "]").replace(";", ",")


WINDOW = re.compile(r"\s+(?:(?:in|over|during|within)\s+(?:the\s+)?(?:last|past)\s+(?:\d+|few|couple(?: of)?|several|one|two|three|four|five|six|twelve)\s+(?:days?|weeks?|months?|years?)"
                    r"|so far|to date|recently|lately|currently|now)$", re.I)


def strip_window(attribute: str) -> str:
    """"MCU films watched in last 3 months", "... recently", "... so far": one
    count. The window is the time of saying it, which the since date holds;
    left in the label, each wording became its own entry (4 under one, 5
    refused as too long)."""
    a = attribute.strip()
    b = WINDOW.sub("", a).strip()
    return b if b else a


def _attr_key(attribute: str) -> str:
    return re.sub(r"^setting ", "", _key(strip_window(attribute)))


def _key(name: str) -> str:
    """"The portfolio-site", "portfolio site" and "Portfolio_Site" are one name
    (and, kinds on, a value with or without its kind words is one value)."""
    return re.sub(r"^the ", "", re.sub(r"[\s_-]+", " ", bare(name).strip().lower()))


@dataclass
class Past:
    value: str
    start: str
    end: str
    wrong: bool = False   # recorded by mistake, never true
    by: str = ""          # who marked it never true when a person or agent did ("hand", "Ava"); "" = an import


@dataclass
class Also:
    """Another current value under the same label: a second item of the same
    kind, not a past value ("Fundraising amount: $2,000", also $500)."""
    value: str
    since: str


@dataclass
class Attr:
    name: str             # as written, e.g. "Location"
    value: str
    since: str
    confirmed: str = ""   # last date someone saw it still true
    source: str = ""
    trail: list[Past] = field(default_factory=list)   # newest first, all tiers
    extra: list[str] = field(default_factory=list)    # hand-written sub-lines, kept as is
    also: list[Also] = field(default_factory=list)    # other current items of the same kind, newest first
    said: tuple[str, str] | None = None               # receipt: the words the current value came from, and their date


@dataclass
class Thing:
    name: str
    aliases: list[str]
    kind: str = ""
    created: str = ""
    updated: str = ""
    volatile: dict[str, str] = field(default_factory=dict)   # attr key -> high|low|static
    about: list[str] = field(default_factory=list)            # free-text lines
    attrs: dict[str, Attr] = field(default_factory=dict)      # attr key -> Attr
    extra: list[str] = field(default_factory=list)            # unrecognized lines under Details
    tail: str = ""                                            # sections after Details, verbatim
    file: str = ""                                            # note file stem, set by the store

    @property
    def slug(self) -> str:
        return self.file or slug(self.name)


def writer_trust(source: str, confirm: bool) -> int:
    """Who is writing: a note import (confirm=False), a person (source "hand"),
    or an agent (any other source, or none)."""
    if not confirm:
        return IMPORT
    return PERSON if source == HAND else AGENT


def held_trust(a: "Attr") -> tuple[int, str]:
    """Who stands behind the current value, and the last day they did: who set
    it (its source; a line written by hand without a date is a person's), raised
    to an agent's by a confirmation (only a person or agent confirms)."""
    level = PERSON if a.source == HAND or not (a.source or a.since) else AGENT if a.source else IMPORT
    if a.confirmed and level < AGENT:
        level = AGENT
    return level, max(a.since, a.confirmed)


def trust_hold(a: "Attr", since: str, source: str, confirm: bool) -> str:
    """Why a different value for `a`, from this writer and dated `since`, can't
    replace it: '' when it can. Only news dated after the day the more trusted
    source last stood behind the value replaces it."""
    level, day = held_trust(a)
    if writer_trust(source, confirm) >= level or since > day:
        return ""
    who = ("set by hand" if level == PERSON else f"set by {a.source}" if a.source and a.since >= a.confirmed
           else "confirmed")
    by = "a note import" if writer_trust(source, confirm) == IMPORT else "an agent"
    return f"{who} on {day}; {by} changes it only with news dated after {day}"


# ---- rendering and parsing ------------------------------------------------------

FACT = re.compile(r"^- \*\*(?P<a>.+?):\*\* ?(?P<rest>.*?)\s*$")
META = re.compile(r"^(?P<v>.*?)\s*\((?P<meta>(?:since|confirmed|source:)[^()]*)\)$")
RETIRED_TEXT = re.compile(r"^retired / decommissioned(?: since (?P<d>[0-9-]+))?$")
WAS = re.compile(r"^  - was: (?P<v>.*) \((?:(?P<s>[0-9-]*) to |until )(?P<e>[0-9-]+)\)\s*$")
# Not "since": the reading rule says the newest since wins, and these stay true beside it.
ALSO_ITEM = re.compile(r"^  - also current: (?P<v>.*) \(another one, from (?P<s>[0-9-]*)\)\s*$")
# A receipt: the sentence the current value was read from (an import writes it).
SAID = re.compile(r'^  - said: "(?P<q>.*)" \((?P<d>[0-9-]*)\)\s*$')
SAID_CHARS = 200
WRONG = re.compile(r"^  - was recorded as (?P<v>.*) by mistake \(corrected (?P<e>[0-9-]+)(?: by (?P<by>[^;()]+))?; "
                   r"never true\)\s*$")
OLDER = re.compile(r"^  - \d+ older value\(s\) in \[\[")
HIST = re.compile(r"^- (?P<a>.+?): (?P<v>.*) \((?:(?P<s>[0-9-]*) to |until )(?P<e>[0-9-]+)\)"
                  r"(?P<w> \(recorded by mistake, never true\))?\s*$")


def _merge_also(a: list[Also], b: list[Also]) -> list[Also]:
    seen, out = set(), []
    for x in sorted(a + b, key=lambda x: x.since, reverse=True):
        if _key(x.value) not in seen:
            seen.add(_key(x.value))
            out.append(x)
    return out


def _meta(text: str) -> dict:
    out = {}
    for part in (text or "").split(";"):
        k, _, v = part.strip().partition(" ")
        if k in ("since", "confirmed"):
            out[k] = v.strip()
        elif k == "source:":
            out["source"] = v.strip()
    return out


def _span(p: Past) -> str:
    return f"({p.start} to {p.end})" if p.start else f"(until {p.end})"


def _fact_line(a: Attr) -> str:
    retired = a.value.lower() == RETIRED
    meta = [f"since {a.since}"] if a.since and not retired else []
    if a.confirmed and a.confirmed != a.since:
        meta.append(f"confirmed {a.confirmed}")
    if a.source:
        meta.append(f"source: {a.source}")
    head = (f"- **{a.name}:** retired / decommissioned" + (f" since {a.since}" if a.since else "")
            if retired else f"- **{a.name}:** {a.value}")
    return head + (f" ({'; '.join(meta)})" if meta else "")


def render_thing(t: Thing) -> str:
    vol = ", ".join(f"{k}: {v}" for k, v in sorted(t.volatile.items()))
    fm = [f'title: "{t.name}"', f'kind: "{t.kind}"', f'date: "{t.created}"', f'updated: "{t.updated}"']
    if vol:
        fm.append(f"volatility: {{{vol}}}")
    lines = ["---", *fm, "---", "", f"# {t.name}", ""]
    lines.append(f"{t.name} (also called {', '.join(t.aliases)})." if t.aliases else f"{t.name}.")
    lines += t.about
    lines += ["", "## Details", ""]
    for key, a in t.attrs.items():
        lines.append(_fact_line(a))
        if a.said:
            lines.append(f'  - said: "{a.said[0]}" ({a.said[1]})')
        lines += [f"  - also current: {x.value} (another one, from {x.since})" for x in a.also]
        for p in a.trail[:WARM]:
            if p.wrong:
                by = f" by {p.by}" if p.by else ""
                lines.append(f"  - was recorded as {p.value} by mistake (corrected {p.end}{by}; never true)")
            else:
                lines.append(f"  - was: {p.value} {_span(p)}")
        if len(a.trail) > WARM:
            lines.append(f"  - {len(a.trail) - WARM} older value(s) in [[{t.slug}-history]]")
        lines += a.extra
    lines += t.extra
    text = "\n".join(lines) + "\n"
    return text + ("\n" + t.tail.strip("\n") + "\n" if t.tail.strip() else "")


def _as_of_line(a: Attr, day: str) -> str:
    """One entry on `day`: the value whose dates cover it (and the other items
    of the same kind current by then), or why there is none."""
    head = f"- **{a.name}:** "
    past = [p for p in a.trail if not p.wrong]
    start = a.since or (max(p.end for p in past) if past else "")
    also = "; ".join(f"{x.value} (from {x.since}, still current)" for x in a.also if not x.since or x.since <= day)
    found = ""
    if not start or start <= day:
        if a.value.lower() == RETIRED:
            return head + "retired / decommissioned" + (f" since {start}" if start else "")
        found = a.value + (f" (since {start}, still current)" if start else " (start unknown, still current)")
    for p in past if not found else []:
        if day < p.end and (not p.start or p.start <= day):
            found = p.value + (f" ({p.start} to {p.end})" if p.start else f" (until {p.end}; start unknown)")
            break
    if found or also:
        return head + "; also ".join(x for x in (found, also) if x)
    first = min([p.start for p in past if p.start] + [start] + [x.since for x in a.also if x.since])
    return head + (f"nothing known on that day (first value from {first})" if day < first
                   else "nothing known on that day (a gap between known values)")


def render_history(t: Thing) -> str | None:
    rows = []
    for key, a in t.attrs.items():
        for p in a.trail[WARM:]:
            tag = " (recorded by mistake, never true)" if p.wrong else ""
            rows.append(f"- {key}: {p.value} {_span(p)}{tag}")
    if not rows:
        return None
    last = max(p.end for a in t.attrs.values() for p in a.trail[WARM:])
    return (f'---\ntitle: "{t.name} history (archived values)"\ndate: "{last}"\nupdated: "{last}"\n---\n\n'
            f"# {t.name} history (archived values)\n\n"
            f"Past values for {t.name} that are no longer current. Not current facts.\n\n" + "\n".join(rows) + "\n")


def parse_thing(text: str, history: str = "") -> Thing:
    fm = dict(re.findall(r'^(\w+): "?(.*?)"?$', text.split("\n---", 1)[0], re.M))
    t = Thing(name=fm.get("title", ""), aliases=[], kind=fm.get("kind", ""),
              created=fm.get("date", ""), updated=fm.get("updated", ""))
    vol = re.search(r"^volatility: \{(.*)\}$", text, re.M)
    if vol:
        for part in vol.group(1).split(","):
            k, _, v = part.partition(":")
            if k.strip():
                t.volatile[k.strip()] = v.strip()
    body = text.split("\n---", 1)[1] if "\n---" in text else text
    head, _, details = body.partition("\n## Details\n")
    for ln in head.splitlines():
        m = re.match(r"^(.+?) \(also called (.+)\)\.$", ln)
        if m and not t.aliases:
            t.aliases = [x.strip() for x in m.group(2).split(",")]
        elif ln.strip() and not ln.startswith("# ") and ln.strip() != f"{t.name}." and not m:
            t.about.append(ln)
    # Facts end at the next section; anything a person added after that stays as written.
    m = re.search(r"^## ", details, re.M)
    if m:
        details, t.tail = details[:m.start()], details[m.start():]
    cur = None
    for ln in details.splitlines():
        m = FACT.match(ln)
        if m:
            v, meta = m.group("rest"), {}
            mm = META.match(v)
            if mm:
                v, meta = mm.group("v"), _meta(mm.group("meta"))
            r = RETIRED_TEXT.match(v)
            if r:
                v = RETIRED
                meta.setdefault("since", r.group("d") or "")
            cur = Attr(m.group("a").strip(), v, meta.get("since", ""), meta.get("confirmed", ""),
                       meta.get("source", ""))
            t.attrs[cur.name.lower()] = cur
            continue
        if not ln.strip():
            continue
        if cur is not None and ln.startswith("  "):
            m = SAID.match(ln)
            if m:
                cur.said = (m.group("q"), m.group("d"))
                continue
            m = ALSO_ITEM.match(ln)
            if m:
                cur.also.append(Also(m.group("v"), m.group("s")))
                continue
            m = WAS.match(ln)
            if m:
                cur.trail.append(Past(m.group("v"), m.group("s") or "", m.group("e")))
                continue
            m = WRONG.match(ln)
            if m:
                cur.trail.append(Past(m.group("v"), "", m.group("e"), wrong=True, by=(m.group("by") or "").strip()))
                continue
            if not OLDER.match(ln):
                cur.extra.append(ln)   # a person's sub-note under a fact: keep it there
            continue
        t.extra.append(ln)
        cur = None
    for ln in history.splitlines():   # cold tier, appended after the warm entries
        m = HIST.match(ln)
        if m and m.group("a") in t.attrs:
            t.attrs[m.group("a")].trail.append(Past(m.group("v"), m.group("s") or "", m.group("e"),
                                                    bool(m.group("w"))))
    return t


# ---- the store --------------------------------------------------------------------

class Store:
    def __init__(self, root: Path, today: str | None = None):
        self.root = Path(root)
        self.notes = self.root / "notes"
        self.notes.mkdir(parents=True, exist_ok=True)
        self.today = today or date.today().isoformat()
        self._depth, self._lockfh, self._stamp = 0, None, None
        self._sweep()
        self._index()

    def _sweep(self):
        """A writer killed between writing its temp file and the rename leaves
        the temp file (the note itself is whole). Remove those whose writer is gone."""
        for d in (self.root, self.notes):
            for tmp in d.glob(".*.tmp*"):
                pid = tmp.name.rsplit(".tmp", 1)[-1]
                try:
                    os.kill(int(pid), 0)
                except (ValueError, ProcessLookupError):
                    tmp.unlink(missing_ok=True)
                except PermissionError:
                    pass   # alive, someone else's

    # -- several writers (a bulk import, an agent, a person) share one folder --

    def _mtime(self):
        try:
            return (self.root / "MEMORY.md").stat().st_mtime_ns
        except FileNotFoundError:
            return None

    @contextmanager
    def _locked(self):
        """One write at a time across processes. If another writer changed
        memory since this store last looked, re-read the names first, so two
        writers can't each create the same thing or drop each other's index rows."""
        self._depth += 1
        try:
            if self._depth == 1:
                if fcntl:
                    self._lockfh = open(self.root / ".deltos.lock", "a")
                    fcntl.flock(self._lockfh, fcntl.LOCK_EX)
                if self._mtime() != self._stamp:
                    self._index()
            yield
        finally:
            self._depth -= 1
            if self._depth == 0 and self._lockfh:
                fcntl.flock(self._lockfh, fcntl.LOCK_UN)
                self._lockfh.close()
                self._lockfh = None

    @staticmethod
    def _write(path: Path, text: str):
        """Write via a temp file and rename: a kill or SIGSTOP mid-write leaves
        the old note, never half of one."""
        tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
        tmp.write_text(text)
        os.replace(tmp, path)

    def _new_file(self, name: str) -> str:
        """A file stem no other note uses. "Hollis & Grey" and "Hollis Grey"
        slug alike; "Log 2026-03-01" or "Tamsin history" would land on a daily
        log or a cold-history note."""
        base = slug(name)
        if base.startswith("log-"):
            base = "thing-" + base
        if base.endswith("-history") or base == "memory":
            base += "-thing"
        stem, i = base, 2
        while (self.notes / f"{stem}.md").exists() or stem in self.rows:
            stem, i = f"{base}-{i}", i + 1
        return stem

    def _index(self):
        self.by_name: dict[str, str] = {}   # name key -> slug
        self.rows: dict[str, str] = {}      # slug -> MEMORY.md line
        self._stamp = self._mtime()
        for p in self.notes.glob("*.md"):
            # <thing>-extra.md is a page of a thing, as doctor and health read it: indexed as a
            # thing it stayed behind when its thing was forgotten (10-20 lint apply: 301 such rows).
            if p.name.startswith("log-") or p.name.endswith(("-history.md", "-extra.md")):
                continue
            t = parse_thing(p.read_text())
            t.file = p.stem
            self.rows[p.stem] = self._row(t)
            for n in [t.name, *t.aliases]:
                self.by_name.setdefault(_key(n), p.stem)

    @staticmethod
    def _row(t: Thing) -> str:
        names = f" ({', '.join(t.aliases)})" if t.aliases else ""
        return f"- [{t.name}](notes/{t.slug}.md){names} — {t.kind or 'thing'}, updated {t.updated}"

    def find(self, name: str) -> Thing | None:
        s = self.by_name.get(_key(name))
        if not s:
            # The label recall prints ("[brain-2 · date]", "[[brain-2-history]]")
            # names the thing too: it is the note's file stem.
            stem = re.sub(r"\.md$|-history$", "", re.sub(r"^notes/", "", name.strip().strip("[]").split(" · ")[0].strip()))
            # Only a plain file name can be a stem: a model's sentence or a path ("../MEMORY") is not one.
            plain = stem and "/" not in stem and len(f"{stem}-history.md".encode()) <= 255
            if plain and (self.notes / f"{stem}.md").exists() and not stem.startswith("log-") and not stem.endswith("-extra"):
                s = stem
        if not s:
            return None
        hist, p = self.notes / f"{s}-history.md", self.notes / f"{s}.md"
        if not p.exists():   # deleted by hand since the index was read
            return None
        t = parse_thing(p.read_text(), hist.read_text() if hist.exists() else "")
        t.file = s
        return t

    def save(self, t: Thing):
        t.updated = max(t.updated or "", self.today)
        self._write(self.notes / f"{t.slug}.md", render_thing(t))
        hist, h = self.notes / f"{t.slug}-history.md", render_history(t)
        if h:
            self._write(hist, h)
        elif hist.exists():
            hist.unlink()
        for n in [t.name, *t.aliases]:
            self.by_name[_key(n)] = t.slug
        self.rows[t.slug] = self._row(t)
        self._write_index()

    def _write_index(self):
        head = ["# MEMORY", "", "One line per thing. Facts live in notes/<thing>.md; old values sit under each fact.", ""]
        self._write(self.root / "MEMORY.md", "\n".join(head + [self.rows[k] for k in sorted(self.rows)]) + "\n")
        self._stamp = self._mtime()

    def similar(self, name: str) -> list[str]:
        """Known things that share a word with this name: the likely duplicates
        when an agent is about to create something new ("shipping" vs
        "courier (the shipping vendor)")."""
        words = {w for w in re.findall(r"[a-z0-9]+", _key(name)) if len(w) > 3 and w not in COMMON}
        if not words:
            return []
        hits = {}
        for key, sl in self.by_name.items():
            if words & set(re.findall(r"[a-z0-9]+", key)):
                hits.setdefault(sl, None)
        return [self.label(sl) for sl in list(hits)[:3] if sl in self.rows]

    def label(self, sl: str) -> str:
        """"Tamsin (TS-2, the big GPU box)": a thing and its other names."""
        r = self.rows[sl]
        return r.split("](")[0].removeprefix("- [") + r.split(")", 1)[1].split(" — ")[0]

    def closest(self, name: str, n: int = 5) -> list[str]:
        """Known things an unknown name might mean: shared words, near
        spellings ("Brisk" / "brisk-01"), or one name inside the other."""
        k = _key(name)
        hits = {self.by_name[x]: None for x in self.by_name if len(k) > 2 and (k in x or x in k) and len(x) > 2}
        for x in difflib.get_close_matches(k, list(self.by_name), n=n, cutoff=0.6):
            hits.setdefault(self.by_name[x], None)
        out = [self.label(sl) for sl in hits if sl in self.rows]
        return list(dict.fromkeys(out + self.similar(name)))[:n]

    def names_list(self) -> str:
        """Every thing with its other names, one per line: what a writer needs
        to see before it decides whether "the edge box" is something new."""
        return "\n".join(self.label(sl) for sl in sorted(self.rows)) or "(nothing yet)"

    # -- operations (each returns a one-line receipt for the agent) --

    def name(self, thing: str, aliases: list[str] | None = None, kind: str = "") -> str:
        with self._locked():
            return self._name(thing, aliases, kind)

    def unname(self, thing: str, names: list[str]) -> str:
        """Take names off a thing they were wrongly merged into ('claimcheck.cc'
        is the website, not the API package). The thing keeps its facts; a
        removed name is free for name_thing to give to the right thing."""
        with self._locked():
            t = self.find(thing)
            if t is None:
                return f"nothing is called {thing!r}"
            drop = {_key(n) for n in names if n.strip()}
            if _key(t.name) in drop:
                return (f"{t.name} is the thing's own name; to rename it, name the new name and remove "
                        f"the old one from the right thing")
            gone = [a for a in t.aliases if _key(a) in drop]
            t.aliases = [a for a in t.aliases if _key(a) not in drop]
            for g in gone:
                self.by_name.pop(_key(g), None)
            self.save(t)
            missing = [n for n in names if _key(n) not in {_key(g) for g in gone}]
            return (f"{t.name}: removed names {', '.join(gone)}" if gone else f"{t.name}: none of those names") + \
                (f"; not its names: {', '.join(missing)}" if missing else "")

    def merge(self, thing: str, other: str) -> str:
        """Two profiles of one thing ('Beth' and 'Bethany Ann Marlowe') become
        one: `other`'s names, entries and history move into `thing`, and its
        files go (git keeps them). Every name still finds the thing. Where both
        have an entry, the newer value is current and the other joins the trail."""
        with self._locked():
            t, o = self.find(thing), self.find(other)
            if t is None or o is None:
                return f"nothing is called {thing if t is None else other!r}"
            if t.slug == o.slug:
                return f"{t.name} and {other} are already one thing"
            if t.kind and o.kind and (_key(t.kind) in PEOPLE) != (_key(o.kind) in PEOPLE):
                return f"not merged: {t.name} is a {t.kind} and {o.name} is a {o.kind}"
            names = {_key(x) for x in [t.name, *t.aliases]}
            for n in [o.name, *o.aliases]:
                if _key(n) not in names and not name_problem(n):
                    t.aliases.append(n)
                    names.add(_key(n))
            conflicts = []
            for k, b in o.attrs.items():
                k = next((x for x in t.attrs if _attr_key(x) == _attr_key(k)), k)
                a = t.attrs.get(k)
                if a is None:
                    t.attrs[k] = b
                    continue
                new, old = (b, a) if b.since > a.since else (a, b)
                if _key(new.value) == _key(old.value):
                    new.since = min(filter(None, [a.since, b.since]), default="")
                    new.confirmed = max(a.confirmed, b.confirmed)
                    past = []
                else:
                    conflicts.append(k)
                    past = [Past(old.value, old.since, new.since or self.today)]
                seen, trail = set(), []
                for p in sorted(new.trail + past + old.trail, key=lambda p: (p.end, p.start), reverse=True):
                    if (_key(p.value), p.start, p.end) not in seen:
                        seen.add((_key(p.value), p.start, p.end))
                        trail.append(p)
                new.trail, new.extra = trail, new.extra + [x for x in old.extra if x not in new.extra]
                new.also = _merge_also(new.also, old.also)
                t.attrs[k] = new
            t.about += [x for x in o.about if x not in t.about]
            t.extra += [x for x in o.extra if x not in t.extra]
            t.tail = "\n".join(x for x in (t.tail.strip("\n"), o.tail.strip("\n")) if x)
            t.kind, t.created = t.kind or o.kind, min(filter(None, [t.created, o.created]), default="")
            extra = self.notes / f"{o.slug}-extra.md"
            if extra.exists():
                body = extra.read_text().split("\n---", 1)[-1].split("not current facts.\n", 1)[-1]
                self.aside(t.name, [x for x in body.splitlines() if x.strip()])
            for f in (f"{o.slug}.md", f"{o.slug}-history.md", f"{o.slug}-extra.md"):
                (self.notes / f).unlink(missing_ok=True)
            self.rows.pop(o.slug, None)
            for key in [k for k, sl in self.by_name.items() if sl == o.slug]:
                del self.by_name[key]
            self.save(t)
            return f"merged {o.name} into {t.name}" + \
                (f"; both had {', '.join(conflicts)}: the newer value is current, the other is in its trail"
                 if conflicts else "")

    def fold(self, thing: str, attribute: str, into: str) -> str:
        """One fact written under two labels ("Bike count: 3" 02-22, "Total bikes
        owned: 4" 10-10) becomes one entry: the newer value is current, under its
        own label, and the older value and both histories are its trail."""
        with self._locked():
            t = self.find(thing)
            if t is None:
                return f"nothing is called {thing!r}"
            ka = next((k for k in t.attrs if _attr_key(k) == _attr_key(attribute)), None)
            kb = next((k for k in t.attrs if _attr_key(k) == _attr_key(into)), None)
            if ka is None or kb is None or ka == kb:
                return f"not folded: {t.name} has no separate {attribute!r} and {into!r} entries"
            a, b = t.attrs[ka], t.attrs[kb]
            new, old = (b, a) if b.since >= a.since else (a, b)
            past = [] if _key(new.value) == _key(old.value) else [Past(old.value, old.since, new.since or self.today)]
            if not past:
                new.since = min(filter(None, [a.since, b.since]), default="")
            seen, trail = set(), []
            for p in sorted(new.trail + past + old.trail, key=lambda p: (p.end, p.start), reverse=True):
                if (_key(p.value), p.start, p.end) not in seen:
                    seen.add((_key(p.value), p.start, p.end))
                    trail.append(p)
            new.trail, new.extra = trail, new.extra + [x for x in old.extra if x not in new.extra]
            new.also = _merge_also(new.also, old.also)
            new.confirmed = max(a.confirmed, b.confirmed)
            keep = kb if new is b else ka
            del t.attrs[ka if keep == kb else kb]
            t.volatile.pop(ka if keep == kb else kb, None)
            self.save(t)
            return f"{t.name}: {old.name} folded into {new.name} ({new.value}; was {old.value})"

    # -- forgetting: deletion that leaves a receipt and can be undone ---------------

    def _log(self, entry: dict):
        with open(self.root / FORGET_LOG, "a") as f:
            f.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "day": self.today, **entry}) + "\n")

    def _log_entries(self) -> list[dict]:
        try:
            lines = (self.root / FORGET_LOG).read_text().splitlines()
        except FileNotFoundError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def _pointing_at(self, t: Thing) -> list[str]:
        """Things with an entry whose value is one of t's names (runs on X, part of X)."""
        names = {_key(n) for n in [t.name, *t.aliases]}
        out = []
        for stem in self.rows:
            if stem == t.slug:
                continue
            o = parse_thing((self.notes / f"{stem}.md").read_text())
            if any(_key(a.value) in names for a in o.attrs.values()):
                out.append(o.name)
        return out

    def _trash_dir(self, t: Thing, attribute: str = "") -> Path:
        base = f"{self.today}-{t.slug}" + (f"--{slug(attribute)}" if attribute else "")
        d, i = self.root / TRASH / base, 2
        while d.exists():
            d, i = self.root / TRASH / f"{base}-{i}", i + 1
        d.mkdir(parents=True)
        return d

    def forget(self, thing: str, attribute: str = "", reason: str = "", source: str = "",
               user_asked: bool = False) -> str:
        """Delete a thing that should not be in memory at all (a test, junk, a
        mistake, something the user asked to forget), or one of its entries.
        Not for news (remember) or a wrong value (remember with correction).

        Nothing is destroyed: the files move to .trash/ and restore() brings them
        back; every forget is logged with who, when and why. An agent (source set)
        gets FORGET_PER_DAY forgets a day, and a whole thing with BIG_THING or more
        entries, or one other things point to, only with user_asked."""
        reason = _clean(reason)
        if not reason:
            return "not forgotten: say why (reason), it is kept with the receipt"
        with self._locked():
            t = self.find(thing)
            if t is None:
                near = self.closest(thing, 3)
                return f"nothing is called {thing!r}" + (f"; close: {', '.join(near)}" if near else "")
            if source:
                used = sum(1 for e in self._log_entries()
                           if e.get("action") == "forget" and e.get("source") == source and e.get("day") == self.today)
                if used >= FORGET_PER_DAY:
                    return (f"not forgotten: {source} has already forgotten {used} things today (the daily limit). "
                            f"Tell the user what you would forget; they can run `deltos forget` themselves")
            if attribute:
                k = next((x for x in t.attrs if _attr_key(x) == _attr_key(attribute)), None)
                if k is None:
                    return f"{t.name} has no entry {attribute!r}; its entries: {', '.join(a.name for a in t.attrs.values()) or 'none'}"
                a = t.attrs[k]
                d = self._trash_dir(t, a.name)
                for f in (f"{t.slug}.md", f"{t.slug}-history.md"):
                    if (self.notes / f).exists():
                        shutil.copy2(self.notes / f, d / f)
                del t.attrs[k]
                t.volatile.pop(k, None)
                self.save(t)
                what = f"{t.name}'s {a.name} ({a.value}" + (f", and {len(a.trail)} older value(s)" if a.trail else "") + ")"
            else:
                pointing = self._pointing_at(t)
                if source and not user_asked and (len(t.attrs) >= BIG_THING or pointing):
                    why = ", ".join(filter(None, [f"{len(t.attrs)} entries" if len(t.attrs) >= BIG_THING else "",
                                                  f"{', '.join(pointing[:5])} point to it" if pointing else ""]))
                    return (f"not forgotten: {t.name} has {why}. Forget a whole thing like that only when the user "
                            f"asked for this exact thing (user_asked=true), or forget the wrong entries one at a time")
                d = self._trash_dir(t)
                for f in (f"{t.slug}.md", f"{t.slug}-history.md", f"{t.slug}-extra.md"):
                    if (self.notes / f).exists():
                        shutil.move(str(self.notes / f), str(d / f))
                self.rows.pop(t.slug, None)
                for key in [x for x, sl in self.by_name.items() if sl == t.slug]:
                    del self.by_name[key]
                self._write_index()
                what = f"{t.name} ({len(t.attrs)} entries" + (f"; names {', '.join(t.aliases)}" if t.aliases else "") + ")"
            rel = str(d.relative_to(self.root))
            meta = {"thing": t.name, "names": [t.name, *t.aliases], "file": t.slug, "attribute": attribute and a.name,
                    "reason": reason, "source": source or "person", "user_asked": bool(user_asked), "trash": rel}
            (d / "forgotten.json").write_text(json.dumps(meta, indent=2) + "\n")
            self._log({"action": "forget", **meta})
            return f"forgot {what}; kept in {rel}, memory_restore brings it back"

    def forgotten(self, n: int = 20) -> list[dict]:
        """The latest forgets, newest first, with whether each was restored or purged."""
        entries = self._log_entries()
        restored = {e.get("trash") for e in entries if e.get("action") == "restore"}
        purged = {d for e in entries if e.get("action") == "purge" for d in e.get("trash", [])}
        return [{**e, "restored": e.get("trash") in restored, "purged": e.get("trash") in purged}
                for e in reversed(entries) if e.get("action") == "forget"][:n]

    def purge(self, thing: str) -> str:
        """Empty the trash of a forgotten thing for good (a person's call: there is
        no agent tool for it). Files the memory folder's git already committed stay
        in its history."""
        with self._locked():
            key = _key(thing)
            hits = [e for e in self.forgotten(10_000) if not e["restored"] and not e["purged"]
                    and (key in {_key(n) for n in e.get("names", [])} or key == _key(e.get("file", "")))]
            gone = []
            for e in hits:
                d = self.root / e["trash"]
                if d.is_dir() and d.resolve().parent == (self.root / TRASH).resolve():
                    shutil.rmtree(d)
                    gone.append(e["trash"])
            if not gone:
                return f"no trash for {thing!r}"
            self._log({"action": "purge", "thing": thing, "trash": gone, "source": "person"})
            return f"purged {', '.join(gone)}"

    def restore(self, thing: str, source: str = "") -> str:
        """Undo the newest forget of this thing (or of one of its entries)."""
        with self._locked():
            key = _key(thing)
            hit = next((e for e in self.forgotten(10_000) if not e["restored"]
                        and (key in {_key(n) for n in e.get("names", [])} or key == _key(e.get("file", "")))), None)
            if hit is None:
                return f"nothing forgotten is called {thing!r}"
            if hit["purged"]:
                return f"{hit['thing']} was purged for good; only the memory folder's git history may still have it"
            d = self.root / hit["trash"]
            if not d.is_dir():
                return f"the trash folder {hit['trash']} is gone; git may still have it"
            stem = hit["file"]
            if hit.get("attribute"):
                t = self.find(hit["thing"])
                if t is None:
                    return f"{hit['thing']} itself is gone now; restore it first"
                old = parse_thing((d / f"{stem}.md").read_text(),
                                  (d / f"{stem}-history.md").read_text() if (d / f"{stem}-history.md").exists() else "")
                k = next((x for x in old.attrs if _attr_key(x) == _attr_key(hit["attribute"])), None)
                if k is None:
                    return f"the forgotten copy has no {hit['attribute']!r} entry"
                if any(_attr_key(x) == _attr_key(k) for x in t.attrs):
                    return f"{t.name} has a {hit['attribute']} entry again; remember the value instead of restoring"
                t.attrs[k] = old.attrs[k]
                self.save(t)
                what = f"{t.name}'s {hit['attribute']}"
            else:
                taken = [n for n in hit["names"] if self.by_name.get(_key(n))]
                if taken or (self.notes / f"{stem}.md").exists():
                    return f"not restored: {', '.join(taken) or stem} is another thing now; rename or merge that first"
                for f in (f"{stem}.md", f"{stem}-history.md", f"{stem}-extra.md"):
                    if (d / f).exists():
                        shutil.move(str(d / f), str(self.notes / f))
                t = self.find(stem)
                t.file = stem
                self.save(t)
                what = t.name
            self._log({"action": "restore", "thing": hit["thing"], "attribute": hit.get("attribute", ""),
                       "trash": hit["trash"], "source": source or "person"})
            return f"restored {what}"

    def _name(self, thing: str, aliases: list[str] | None = None, kind: str = "") -> str:
        # "a, b" is two names: the profile line lists names with commas, so a
        # comma inside one name would read back as two and be re-added each time.
        thing = base_name(html.unescape(thing))
        aliases = [base_name(x) for a in aliases or [] for x in html.unescape(a).split(",")]
        t = self.find(thing)
        if t is None and name_problem(thing):
            return f"not created: {thing!r} {name_problem(thing)}"
        refused = [a for a in (aliases or []) if a.strip() and name_problem(a)]
        why: dict[str, str] = {}
        new, seen = [], {_key(thing)}
        for a in aliases or []:   # the model repeats names; the first spelling wins
            if a.strip() and a not in refused and _key(a) not in seen:
                seen.add(_key(a))
                new.append(a.strip())
        if t is None:
            for a in new:   # known under one of the new names? then thing is an alias
                t = self.find(a)
                if t and kind and t.kind and (_key(kind) in PEOPLE) != (_key(t.kind) in PEOPLE):
                    # "= deploy-helper | Ava | skill": a skill named after the agent it
                    # serves is not the agent. Merging a person with a non-person
                    # put a SKILL.md path on Ava's profile.
                    t = None
                    continue
                if t:
                    new = [thing] + [x for x in new if x != a]
                    break
        kept = [t.name, *t.aliases] if t else [thing]
        for a in list(new):
            sib = numbered_sibling(a, [x for x in kept if x != a])
            if sib:
                new.remove(a)
                refused.append(a)
                why[a] = f"differs from {sib!r} only by its number: another thing, name it on its own"
            else:
                kept.append(a)
        if t is None:
            clash = [a for a in new if _key(a) in self.by_name]
            near = [x for n in [thing, *new] for x in self.similar(n)]
            t = Thing(thing.strip(), [a for a in new if a not in clash], kind, self.today, self.today,
                      file=self._new_file(thing))
            self.save(t)
            return f"created {t.name}" + (f" (also called {', '.join(t.aliases)})" if t.aliases else "") + \
                (f"; refused as names: {', '.join(f'{a} ({why[a]})' if a in why else a for a in refused)}" if refused else "") + \
                (f"; skipped names already used by other things: {', '.join(clash)}" if clash else "") + \
                (f". Possibly the same as: {'; '.join(dict.fromkeys(near))}. If so, add this name to that "
                 f"thing with name_thing and record facts there." if near else "")
        if new and kind and t.kind and (_key(kind) in PEOPLE) != (_key(t.kind) in PEOPLE):
            # "= Sam | Waikiki Beach | place": names given as a place are another
            # thing, not more names for the person (40 hotels and airlines on Sam).
            return (f"not added: {t.name} is a {t.kind}, and {', '.join(new)} given as a {kind} is another "
                    f"thing; name it on its own")
        added, taken = [], []
        for a in new:
            owner = self.by_name.get(_key(a))
            if owner and owner != t.slug:
                taken.append(a)
                continue
            if _key(a) not in {_key(x) for x in [t.name, *t.aliases]}:
                t.aliases.append(a)
                added.append(a)
        if kind and not t.kind:
            t.kind = kind
        self.save(t)
        msg = f"{t.name}: added names {', '.join(added)}" if added else f"{t.name}: nothing new"
        return msg + (f"; refused as names: {', '.join(f'{a} ({why[a]})' if a in why else a for a in refused)}" if refused else "") + \
            (f"; skipped names already used by other things: {', '.join(taken)} (if that is the same thing in "
             f"two profiles, merge them)" if taken else "")

    def remember(self, thing: str, attribute: str, value: str, since: str = "", source: str = "",
                 correction: bool = False, volatility: str = "", create: bool = False, confirm: bool = True,
                 another: bool = False) -> str:
        """`create` lets a bulk import make things on the fly. An agent gets a
        refusal instead: a nickname nobody registered must not quietly become
        a second thing, so it has to say which thing it means.
        `confirm=False` (an import): a repeat of the current value is not a
        confirmation. Old notes repeat old values in recaps, and a "confirmed"
        date on a stale value reads as fresh evidence.
        `another=True`: the value is a second item of the same kind (another
        event, purchase, ...), not a new state: both stay current, the older
        one as an "also current:" line (an import: deltos/items.py decides; an agent
        says so with remember's another)."""
        with self._locked():
            return self._remember(thing, attribute, value, since, source, correction, volatility, create, confirm,
                                  another)

    def _remember(self, thing, attribute, value, since, source, correction, volatility, create, confirm=True,
                  another=False) -> str:
        thing, attribute, value = (html.unescape(x) for x in (thing, attribute, value))
        thing = base_name(thing)
        attribute = strip_window(attribute)
        problem = value_problem(value, attribute) or attribute_problem(attribute) or (since and date_problem(since.strip(), self.today))
        if problem:
            return f"not recorded: {problem}"
        attribute, value, source = _clean(attribute), _clean(value), _clean_source(source)
        t, created = self.find(thing), ""
        if t is None and not create:
            near = self.closest(thing)
            return (f"not recorded: nothing is called {thing!r}. "
                    + (f"Known things with close names: {'; '.join(near)}. " if near else "")
                    + "If it is a known thing, add this name to it with name_thing and repeat the call; "
                    "if it is new, register it with name_thing first.")
        if t is None:
            created = self._name(thing) + " | "
            t = self.find(thing)
            if t is None:
                return created.removesuffix(" | ")
        given, since = bool(since), since.strip() or self.today
        key = attribute.lower()
        # "Setting build host", "build host" and "build_host" are one fact:
        # written apart, the old value never moves into the trail.
        key = next((k for k in t.attrs if _attr_key(k) == _attr_key(key)), key)
        if value.lower() in ("retired", "decommissioned", "retired / decommissioned"):
            value = RETIRED
        if volatility in VOLATILE:
            t.volatile[key] = volatility
        a = t.attrs.get(key)
        if a is None:
            t.attrs[key] = Attr(attribute[:1].upper() + attribute[1:], value, since, "", source)
            self.save(t)
            return created + f"{t.name} {key}: {value} (since {since}), new"
        same = _key(value) == _key(a.value)
        item = next((x for x in a.also if _key(x.value) == _key(value)), None)
        if item is not None and not correction:
            # One of the other current items, said again: not news.
            if given and since < item.since:
                item.since = since
                self.save(t)
            return created + f"{t.name} {key}: still {item.value} (another one)"
        if another and not same and not correction:
            if since >= a.since:
                a.also.insert(0, Also(a.value, a.since))
                a.said = None   # the receipt was the earlier item's
                a.value, a.since, a.confirmed, a.source = value, since, "", source
                msg = f"{t.name} {key}: {value} (since {since}); another one beside {a.also[0].value}"
            else:
                a.also = _merge_also(a.also, [Also(value, since)])
                msg = f"{t.name} {key}: {value} recorded as another one (since {since}); current stays {a.value}"
            self.save(t)
            return created + msg
        if same and (correction or since >= a.since):
            if confirm:
                a.confirmed = max(a.confirmed or "", self.today)
            if since < a.since:
                a.since = since
            self.save(t)
            return created + f"{t.name} {key}: still {a.value}" + (f", confirmed {self.today}" if confirm else "")
        hold = trust_hold(a, since, source, confirm)
        if hold and (correction or since >= a.since):
            # Trust order: the value stands; older news still lands in the trail below.
            return created + f"not changed: {t.name} {key} stays {a.value}, {hold}"
        if correction and writer_trust(source, confirm) < held_trust(a)[0]:
            correction = False   # less trusted news, dated after it: a change, not "never true"
        if correction:
            # The current value was never true; the new one has been all along.
            # Who said so: a person or agent's correction holds against a note that
            # repeats the value (notes.Migrator); an import's own guess doesn't.
            a.trail.insert(0, Past(a.value, "", self.today, wrong=True, by=(source or "hand") if confirm else ""))
            a.said = None
            a.value, a.confirmed, a.source = value, "", source
            a.since = since if given else a.since
            self.save(t)
            return created + f"{t.name} {key}: corrected to {value}; {a.trail[0].value} marked never true"
        if since < a.since:
            return created + self._backdate(t, a, key, value, since)
        a.trail.insert(0, Past(a.value, a.since, since))
        a.said = None
        a.value, a.since, a.confirmed, a.source = value, since, "", source
        self.save(t)
        return created + f"{t.name} {key}: {value} (since {since}); was {a.trail[0].value}"

    def receipt(self, thing: str, attribute: str, value: str, words: str, day: str, keep: bool = False) -> bool:
        """Keep the words a current value came from under it ('said: ...'),
        so a reader can see why memory believes it. Only on the current value;
        a later value replaces the receipt with its own (or none). keep: leave
        an existing receipt alone (a value said again)."""
        words = " ".join((words or "").replace('"', "'").split())
        if not words:
            return False
        with self._locked():
            t = self.find(thing)
            a = next((v for k, v in t.attrs.items() if _attr_key(k) == _attr_key(attribute)), None) if t else None
            if a is None or _key(a.value) != _key(value) or (keep and a.said):
                return False
            said = (words if len(words) <= SAID_CHARS else words[:SAID_CHARS - 3].rstrip() + "...", day)
            if a.said == said:
                return False
            a.said = said
            self.save(t)
            return True

    def _backdate(self, t: Thing, a: Attr, key: str, value: str, since: str) -> str:
        """News about a time before the current value. It either says a known
        value started earlier than memory thought, repeats history memory
        already has, or is a past value memory never heard of."""
        i = 0   # where it goes in the trail (newest first; never-true entries carry no dates)
        while i < len(a.trail) and (a.trail[i].wrong or a.trail[i].start > since):
            i += 1
        newer = next((p for p in reversed(a.trail[:i]) if not p.wrong), None)   # the value that came after it
        older = next((p for p in a.trail[i:] if not p.wrong), None)             # the value before it
        if older and _key(older.value) == _key(value) and since < older.end:
            return f"{t.name} {key}: {value} already recorded ({older.start or '?'} to {older.end}); current stays {a.value}"
        nxt = newer or a
        if _key(nxt.value) == _key(value):
            # The same value, known from earlier on: move its start back.
            if newer:
                newer.start = since
            else:
                a.since = since
            if older:
                older.end = min(older.end, since)
            self.save(t)
            return f"{t.name} {key}: {value} held from {since}" + ("" if newer else f"; current stays {a.value}")
        end = newer.start if newer else a.since
        if older:
            older.end = min(older.end, since)
        a.trail.insert(i, Past(value, since, end))
        self.save(t)
        return f"{t.name} {key}: {value} recorded as a past value ({since} to {end}); current stays {a.value}"

    def aside(self, thing: str, lines: list[str]) -> str:
        """Lines that are about a thing but are not its current facts (a session
        log written as settings, an event written as a status) go to
        <thing>-extra.md: kept, searchable, out of the profile."""
        t = self.find(thing)
        if t is None or not lines:
            return ""
        p = self.notes / f"{t.slug}-extra.md"
        with self._locked():
            if not p.exists():
                self._write(p, f'---\ntitle: "{t.name} extra (moved out of the profile)"\n---\n\n'
                               "Lines about this thing that are not current facts.\n\n")
            with open(p, "a") as fh:
                fh.write("".join(ln.rstrip("\n") + "\n" for ln in lines))
        return f"{t.name}: {len(lines)} line(s) set aside"

    def note(self, text: str, day: str = "") -> str:
        """A dated line in the daily log, for things that aren't facts."""
        day = day or self.today
        if date_problem(day, "9999-12-31"):
            return f"not logged: day {day!r} is not YYYY-MM-DD"
        if not _clean(text):
            return "not logged: empty note"
        with self._locked():
            p = self.notes / f"log-{day}.md"
            if not p.exists():
                self._write(p, f'---\ntitle: "Log {day}"\ndate: "{day}"\nupdated: "{day}"\n---\n\n# Log {day}\n\n')
            with open(p, "a") as fh:
                fh.write(f"- {_clean(text)}\n")
        return f"logged on {day}"

    def as_of(self, thing: str, day: str) -> str:
        """Time travel: what memory holds as true on `day`, entry by entry, from
        the dates it already keeps (current line, trail, cold history). Values
        recorded by mistake are never offered. This is when values were true in
        the world, as memory knows it now; git history says what memory
        believed on a day."""
        if date_problem(day, "9999-12-31"):
            return f"not a date: {day!r} (YYYY-MM-DD)"
        t = self.find(thing)
        if t is None:
            return f"nothing known about {thing}"
        lines = [f"# {t.name} on {day}", "",
                 "The values memory has dates for on that day (one learned of late is still here; one never "
                 "noted is missing).", ""]
        for a in t.attrs.values():
            lines.append(_as_of_line(a, day))
        return "\n".join(lines) + "\n"

    def show(self, thing: str, history: bool = False) -> str:
        """The profile; its cold history (every older value) only when asked:
        the profile already points at it ("N older value(s) in [[x-history]]")."""
        t = self.find(thing)
        if t is None:
            return f"nothing known about {thing}"
        hist = self.notes / f"{t.slug}-history.md"
        return render_thing(t) + (("\n" + hist.read_text()) if history and hist.exists() else "")
