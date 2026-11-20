"""Seen: memory reads what the agent's tools return, the way it reads a message.

Why (11-10): memory reached an agent only through what the user's message names. The
output of the agent's own tools is a second stream of text about the world, and memory
never read it. An agent asked to print a system summary saw one Python version on a
machine memory had at another, three runs of three, and said nothing: the question
named no thing, so the block was empty and nothing told it memory held a value.

One mechanism, every stage:
- read: `seen(recall, call, output)` finds the current entries a tool call looked at
  whose number moved. With no model memory can tell that two numbers differ (a
  version, a port, a count, the 4.1 in a model's name), not that two phrases do. The
  thing is the one the call names (a command, a path, a URL), or the place the tools
  run (`here`) when the call names no other thing of its kind. The entry is the one
  whose number has a slot in the text, holding another number of the same shape:
  after the value's own word ("ubuntu 24.04" for "Ubuntu 22.04", "vps-3" for
  "vps-2"), after the label's last word ("Python version: 3.12", "python3.12",
  "port 9000"), after the thing's own name when the label is only "version"
  ("orchard 3.6.0"), or alone as the answer to a call that used the label's words
  ("orchard --version" -> "3.6.0"); while the text nowhere shows the value memory
  holds. A call that is an entry's recorded `check:` looks at that entry, whatever
  its value. A JSON result is read by its values (`said`). No model; ~5 ms.
  Not read: a thing named only in an output (a listing is about none of them),
  values without a number (a status, a host name), decisions, rules and events (no
  look shows those). 11-11, two weeks of real tool results (4,216): counting any
  word of a label or a value beside any number, 40% of results got a note; with
  slots, 3.7%, a median of two notes in a chat that gets any.
- told: `note()` is a few lines for the harness to put after the tool's result: what
  memory holds. Each entry once per chat (`shown`).
- write: nothing here. The agent saw the output and decides; `remember(..., check=how)`
  does the rest (deltos/checks.py: confirm or replace, how to look kept, the look logged).
- import, learn, forget, lint, pages: unchanged. An entry forgotten or retired is not
  held, so it is not shown.
- measured: `<root>/seen/<YYYY-MM>.jsonl` keeps one row per note (things and labels,
  never text); bench/checkloop.py part "passing" scores it on a made-up world.

Off by default ([seen] on = true, or DELTOS_SEEN=1). It needs a harness that hands
memory its tools' results (the Hermes adapter does); over MCP alone nothing calls it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .entrytypes import IDENTITY_LABEL, STATE_WORDS, entry_type
from .recall import STOP, Recall, _value
from .store import RETIRED

CALL_CHARS = 1000                     # of the call (a command, a path, a URL)
HEAD_CHARS, TAIL_CHARS = 3000, 1000   # of its output: the opening and the end
MAX_LINES = 4          # entries in one note
VALUE_WORDS, VALUE_CHARS = 6, 60   # a value longer than this is prose: no look shows it right or wrong
BARE_LINES, BARE_WORDS = 3, 3   # a bare answer: this few lines, the number's line this few words
CHECK_CHARS = 8        # a recorded check shorter than this names no call
SKIP = STOP | {"setting"}


@dataclass(frozen=True)
class Held:
    """A current entry a tool call bears on."""
    profile: str
    thing: str
    label: str
    value: str
    line: str    # "Thing · Label: value (since ...; not confirmed in N weeks)"
    why: int     # 3: the call is the entry's own check; else how many ways the text names the entry
    by: str = ""   # those ways: check, value, label, name, call


def clip(output: str) -> str:
    """What memory reads of a tool's output: its opening and its end."""
    if len(output) <= HEAD_CHARS + TAIL_CHARS:
        return output
    return output[:HEAD_CHARS] + "\n" + output[-TAIL_CHARS:]


def said(x, keys: bool = True) -> str:
    """What a tool's result (or its arguments) says, as text. Plain text is itself. JSON, or
    a dict or list, is read value by value: a one-line value as "key: value", a longer text
    as its own lines. keys=False leaves the keys out: an argument's key ("command",
    "timeout") is the tool's word, not something the call said."""
    if isinstance(x, str):
        t = x.strip()
        if not (t[:1] in "{[" and t[-1:] in "}]"):
            return x
        try:
            x = json.loads(t)
        except ValueError:
            return x
    out: list[str] = []

    def walk(v, key: str = "") -> None:
        if isinstance(v, dict):
            for k, w in v.items():
                walk(w, str(k))
        elif isinstance(v, (list, tuple)):
            for w in v:
                walk(w, key)
        elif isinstance(v, str) and v or keys and v is not None and not isinstance(v, str):
            v = str(v)
            out.append(f"{key}: {v}" if keys and key and "\n" not in v.strip() else v)
    walk(x)
    return "\n".join(out)


def _squash(s: str) -> str:
    return " ".join(s.lower().split())


def _entries(r: Recall, prof: str) -> list[tuple[str, str, str, str]]:
    """A profile's current entries that have one value: (label, value, line, check)."""
    cache = r.__dict__.setdefault("_seen_entries", {})
    if prof not in cache:
        out: list[list] = []
        cur = None
        try:
            lines = (r.root / prof).read_text().splitlines()
        except OSError:
            lines = []
        for ln in lines:
            if ln.startswith("- **") and ":**" in ln:
                cur = [ln.split(":**")[0].removeprefix("- **").strip(), _value(ln.rstrip()).strip(), ln.rstrip(), "", True]
                out.append(cur)
            elif not ln.startswith("  "):
                cur = None
            elif cur and ln.startswith("  - check: "):
                cur[3] = ln[len("  - check: "):].strip()
            elif cur and ln.startswith("  - also current: "):
                cur[4] = False   # several current items: not one value a look can show
        cache[prof] = [tuple(e[:4]) for e in out if e[0] and e[4]]
    return cache[prof]


def _checks(r: Recall) -> list[tuple[str, str, str]]:
    """Every recorded check that can name a call: (the check, profile, label)."""
    if "_seen_checks" not in r.__dict__:
        out = []
        for prof in sorted({f[0] for f in r.facts}):
            for label, _, _, how in _entries(r, prof):
                h = _squash(how)
                if len(h) >= CHECK_CHARS and re.search(r"[\s/.~-]", h):
                    out.append((h, prof, label))
        r.__dict__["_seen_checks"] = out
    return r.__dict__["_seen_checks"]


def seeable(label: str, value: str, kind: str = "") -> bool:
    """An entry a look at the world can show right or wrong: state, or anything that is
    not a rule, a decision, an event, or who and what a thing is; with a short value."""
    v = value.strip()
    if not v or v.lower() == RETIRED or len(v) > VALUE_CHARS or len(v.split()) > VALUE_WORDS:
        return False
    t = entry_type(label, v, kind)
    plain = label.lower().removeprefix("setting ").strip()
    return t == "state" or (t == "identity" and plain != "kind" and not IDENTITY_LABEL.match(plain))


def shows(text: str, value: str) -> bool:
    """The text shows this value: the value as written, or each of its words, standing
    alone ("3.9.2" is not in "3.9.20" or "13.9.2"; "v3.5.0" shows "3.5.0")."""
    def has(v: str) -> bool:
        v = v.strip(" \t,;:()[]\"'`")
        if not v:
            return True
        before = r"(?<![0-9.])" if v[0].isdigit() else r"(?<![a-z0-9])"
        after = r"(?![a-z0-9])(?!\.[0-9])"
        return re.search(before + re.escape(v) + after, text) is not None
    v = _squash(value)
    return has(v) or has(v.removeprefix("v")) or all(has(w) for w in v.split())


NUM = r"\d+(?:\.\d+)*"            # a number as written: 8443, 3.12.3, the 4.1 of v4.1-flash
NUMBER = re.compile(NUM)
GAP = r"[^a-z0-9\n]"              # what stands between a name and its value: "port: 80", "PORT=80", "python 3"
SCHEME = {"http", "https", "ssh", "ftp", "ws", "wss", "www"}   # before a number these name no thing
FILLER = {"is", "v", "version"}


def _agree(a: str, b: str) -> bool:
    """The same number, or one cut short ("3.12" for "3.12.3")."""
    return a == b or a.startswith(b + ".") or b.startswith(a + ".")


def _loose(s: str) -> str:
    """`s` as a pattern: a space, a hyphen or an underscore may be any of them or none
    ("ubuntu 22" ~ "ubuntu-22" ~ "ubuntu22"); any other mark is itself."""
    return "".join(re.escape(p) if p.isalnum() else "[ _-]*" if not p.strip(" _-") else
                   r"\s*".join(re.escape(q) for q in re.split(r"\s+", p))
                   for p in re.findall(r"[a-z0-9]+|[^a-z0-9]+", s))


def _value_slots(value: str) -> list[tuple[re.Pattern, str]]:
    """Where a value's numbers stand: each number with the word it follows ("ubuntu " 22.04,
    "vps-" 2, "-flash-" 1017), else the word that follows it (15 " dollars"). (pattern, number)."""
    v = _squash(value)
    ms, out = list(NUMBER.finditer(v)), []
    for i, m in enumerate(ms):
        before = v[ms[i - 1].end() if i else 0:m.start()]
        after = v[m.end():ms[i + 1].start() if i + 1 < len(ms) else len(v)]
        words = list(re.finditer(r"[a-z]{3,}", before))
        w = re.search(r"[a-z]{3,}", after)
        if words and words[-1].group() not in SCHEME:
            out.append((re.compile(r"(?<![a-z])" + _loose(before[words[-1].start():]) + f"({NUM})"), m.group()))
        elif w and w.group() not in SCHEME:
            out.append((re.compile(rf"(?<![0-9.a-z])({NUM})" + _loose(after[:w.end()]) + r"(?![a-z])"), m.group()))
    return out


def _label_slot(last: str, others: list[str]) -> re.Pattern:
    """A label's last word, then its value: separators and the label's other words between."""
    fill = "|".join([GAP] + sorted({re.escape(w) for w in [*others, *FILLER]}, key=len, reverse=True))
    return re.compile(r"(?<![a-z])" + re.escape(last) + rf"(?:{fill})*?(?<![0-9.])({NUM})")


def _name_slot(r: Recall, prof: str, names: frozenset) -> re.Pattern:
    """A thing's name, then a number: how text gives a version ("orchard 3.6.0", "orchard/3.6.0")."""
    cache = r.__dict__.setdefault("_seen_names", {})
    if (prof, names) not in cache:
        fill = "|".join([GAP] + sorted(FILLER, key=len, reverse=True))
        any_name = "|".join(_loose(x) for x in sorted(names, key=len, reverse=True))
        cache[prof, names] = re.compile(rf"(?<![a-z0-9])(?:{any_name})(?:{fill})*?(?<![0-9.])({NUM})")
    return cache[prof, names]


def _plain_names(r: Recall, call: str, words: set) -> list[tuple[int, int, int]]:
    """Things whose name is a plain word ("Gateway"): a sentence names one only by its capital,
    and a command has none ("gateway status"). In a call the word as it stands names it."""
    if "_seen_plain" not in r.__dict__:
        r.__dict__["_seen_plain"] = {k: gi for index in r._index for k, hits in index.items()
                                     for gi, _, plain in hits if plain}
    out = []
    for k, gi in r.__dict__["_seen_plain"].items():
        if (k in words or " " in k or "-" in k) and re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", call):
            out.append((gi, 0, 0))
    return out


def _slots(r: Recall, prof: str, label: str, value: str) -> tuple:
    """(value slots, label words, own label words, label slot by its own words, by all) of an entry."""
    cache = r.__dict__.setdefault("_seen_slots", {})
    k = (prof, label, value)
    if k not in cache:
        lab = [w for w in re.findall(r"[a-z]+", label.lower()) if len(w) > 1 and w not in SKIP]
        own = [w for w in lab if not STATE_WORDS.fullmatch(w)]
        cache[k] = (_value_slots(value), lab, own,
                    _label_slot(own[-1], lab) if own else None, _label_slot(lab[-1], lab) if lab else None)
    return cache[k]


def seen(r: Recall, call, output, here: str = "", shown: set | None = None,
         n: int = MAX_LINES) -> list[Held]:
    """The current entries this tool call looked at whose number moved (the module's text
    says how one is found), the surest first, at most n. `call`: the tool's arguments (a command, a path, a URL;
    the JSON of them will do). `here`: a name of the thing the
    tools run on. `shown`: (profile, label, value) of what this chat was already told;
    what is returned is added to it."""
    call, output = _squash(said(call, keys=False)[:CALL_CHARS]), clip(said(output))
    out_lines = [x for x in (_squash(ln) for ln in output.splitlines()) if x]
    lines = [call] + out_lines
    text = "\n".join(lines)
    if not text.strip():
        return []
    call_words = set(re.findall(r"[a-z]+", call))
    line_words = [(ln, set(re.findall(r"[a-z]+", ln))) for ln in lines]

    spans = r.name_spans(call)
    r._spans_seen.pop((call, False, frozenset()), None)   # a call is read once: not kept
    scope: dict = {}   # profile -> True: the call names it; False: the place; None: only its checked entries
    called: dict = {}  # profile -> its names
    for gi, _, _ in spans + _plain_names(r, call, call_words):
        prof = r.profile_of.get(r.names[gi][0].lower())
        if prof:
            scope[prof] = True
            called.setdefault(prof, set()).update(_squash(x) for x in r.names[gi])
    place = r.profile_of.get(here.strip().lower()) if here.strip() else None
    if place and place not in scope and not any(r.kind_of.get(p, "") == r.kind_of.get(place, "") for p in scope):
        scope[place] = False
    checked = {(prof, label) for how, prof, label in _checks(r) if how in call}
    for prof, _ in checked:
        scope.setdefault(prof, None)

    found = []
    for prof, is_named in scope.items():
        kind = r.kind_of.get(prof, "")
        for label, value, line, _ in _entries(r, prof):
            if not seeable(label, value, kind) or shows(text, value):
                continue
            if shown is not None and (prof, label, value) in shown:
                continue
            ways = ["check"] if (prof, label) in checked else []
            first = NUMBER.search(value)
            if not ways and first and is_named is not None:
                slots, lab, own, by_own, by_all = _slots(r, prof, label, value)
                # The value's own word with another number: "ubuntu 24.04" for "Ubuntu 22.04".
                if any(not _agree(m.group(1), held) for pat, held in slots for m in pat.finditer(text)):
                    ways.append("value")
                # The label with another number of its shape: "python 3.12.3" for "Python version".
                # A label with no word of its own ("Port") names an entry only of a thing the call names.
                key, pat = (own, by_own) if own else (lab, by_all) if is_named else ([], None)
                held, dotted = first.group(), "." in first.group()
                moved = lambda ln: any(("." in m.group(1)) == dotted and not _agree(m.group(1), held)
                                       for m in pat.finditer(ln))
                if key and any(key[-1] in lw and all(w in lw or w in call_words for w in key) and moved(ln)
                               for ln, lw in line_words):
                    ways.append("label")
                # A label that is only "version", of a thing the call names: the name with a number.
                elif is_named and lab and all(w in FILLER for w in lab) and any(
                        ("." in m.group(1)) == dotted and not _agree(m.group(1), held) for ln in out_lines
                        for m in _name_slot(r, prof, frozenset(called[prof])).finditer(ln)):
                    ways.append("name")
                # The call asked for it by every word and the answer is bare: "orchard --version", "3.6.0".
                elif (is_named and lab and len(out_lines) <= BARE_LINES and all(w in call_words for w in lab)
                      and any(("." in m) == dotted and not _agree(m, held)
                              for ln in out_lines if len(ln.split()) <= BARE_WORDS for m in NUMBER.findall(ln))):
                    ways.append("call")
            if not ways:
                continue
            why = 3 if ways == ["check"] else len(ways)
            full = r.unconfirmed(line)
            found.append((-why, "; not confirmed in " not in full, not is_named, r.title(prof), label,
                          Held(prof, r.title(prof), label, value,
                               f"{r.title(prof)} · {label}: {full.split(':** ', 1)[-1]}", why, "+".join(ways))))
    found.sort(key=lambda x: x[:5])
    out = [x[5] for x in found[:n]]
    if shown is not None:
        shown.update((h.profile, h.label, h.value) for h in out)
    return out


def note(held: list[Held], checks: bool = False) -> str:
    """What the harness puts after the tool's result, or ""."""
    if not held:
        return ""
    how = " (check = how you looked)" if checks else ""
    return ("[memory] On what this touched, memory holds:\n" + "\n".join(f"- {h.line}" for h in held)
            + f"\nIf this output shows another value for the same thing, `remember` the new value, as short as "
              f"the one above{how}. If it is about something else, leave it.")
