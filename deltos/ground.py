"""The text is the authority: a value, a name or a new thing enters memory only
when the unit it was read from states it. What a model writes back is a
proposal; these checks decide, with no model.

Two rules, chosen by the shape of the source (never by the model):
  stated(value, text)         a note is copied text, so the value itself must be in it
                              (case, bold, thousands commas, "three" vs "3",
                              "128GB" vs "128 GB" and "v2.17.2" vs "2.17.2" aside)
  idents_stated(value, text)  a chat is paraphrased speech, so only what identifies
                              the value must be said: every word with a digit in it,
                              and every ISO date, in the user's own lines
  A number is that number in both: "5th" and "8gb" say 5 and 8, "30001" and "0.65"
  do not say 3000 and 0.6.
  grounded(value, text, kind) the rule for a unit of that kind ("note", "chat", None = off)
  named(thing, text)          a new thing's name is stated
  receipt(text, value)        the words around the first line that states a value

Measured 10-19, brain-free (bench/groundcheck.py, logged model output replayed):
  - 8 logged import-test runs (GLM, Flash-Next, DeepSeek; 20 pieces, 71 hand facts):
    14-42% fewer lines taken (mean 25%), at most 1 hand fact lost per run.
  - GLM's 10-19 nightly with its texts: 1,185 of 3,366 values the store would have
    taken are not in their unit's text (608 of them memory's current value copied
    back from the reference list); new things 158 -> 138, none with only a kind.
  - The gate's 16,348 LongMemEval extraction lines: the chat rule drops 308 (1.9%)
    and 1 of the 50 that hit a gold answer, a lens name the model copied from memory
    into a later session that only says "70-200mm zoom lens"; the note rule would
    drop 2,797 and 9 of the 50.
"""
from __future__ import annotations

import html
import re
from functools import lru_cache

from .store import SAID_CHARS, base_name

NUM = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
       "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "a dozen": "12", "once": "1", "twice": "2"}
_NUM = re.compile(r"\b(" + "|".join(sorted(NUM, key=len, reverse=True)) + r")\b")
ISO = re.compile(r"\b(\d{4})-(\d\d)-(\d\d)\b")
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
STRIP = ".,;:!?)(\"'"   # around a chat value's word


def norm(s: str) -> str:
    """'**$2,000**' and 'two thousand' read the way a value is compared: entities
    decoded, lower case, no bold or backticks, no thousands commas, number words
    as digits, single spaces."""
    s = html.unescape(s or "").lower().replace("**", "").replace("`", "")
    s = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", s)
    s = _NUM.sub(lambda m: NUM[m.group(1)], s)
    return " ".join(s.split())


def tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", norm(s)))


def squash(s: str) -> str:
    """'128 GB' and '128GB' alike: the normal form with everything but letters and digits gone."""
    return re.sub(r"[^a-z0-9]", "", norm(s))


@lru_cache(maxsize=64)
def _text(text: str) -> tuple[str, set[str], str]:
    """A unit's text in the three forms, once per unit: every line of it is checked against them."""
    n = norm(text)
    return n, _words(n), re.sub(r"[^a-z0-9]", "", n)


def _words(s: str) -> set[str]:
    """Its words, and their digit and letter runs too: "5th" is also "5", "8gb" also "8" and "gb"."""
    return set(re.findall(r"[a-z0-9]+", s)) | set(re.findall(r"[0-9]+|[a-z]+", s))


def _within(v: str, s: str) -> bool:
    """v is in s, and a number in it is that number: "3000" is not in "30001", nor "0.6" in "0.65"."""
    lead = r"(?<![0-9])" if v[:1].isdigit() else ""
    tail = r"(?![0-9]|[.,][0-9])" if v[-1:].isdigit() else ""
    return bool(re.search(lead + re.escape(v) + tail, s))


def date_said(y: str, m: str, d: str, text: str, day: str = "") -> bool:
    """Is this date in the text, in any of the ways people write one ('2026-09-27',
    '09-27', '9/27', 'Sept 27', '27th of September'), or is it the unit's own day?"""
    if f"{y}-{m}-{d}" == day:
        return True
    tl = _text(text)[0]
    if any(x in tl for x in (f"{y}-{m}-{d}", f"{m}-{d}", f"{m}/{d}", f"{int(m)}/{int(d)}")):
        return True
    if not 1 <= int(m) <= 12:
        return False
    mon, dd = MONTHS[int(m) - 1], int(d)
    return bool(re.search(rf"\b{mon}[a-z]*\.? {dd}(?:st|nd|rd|th)?\b", tl)
                or re.search(rf"\b{dd}(?:st|nd|rd|th)? (?:of )?{mon}", tl))


def stated(value: str, text: str, day: str = "") -> bool:
    """The note rule: the value itself is in the text."""
    if not value or value.strip() in ("", "-"):
        return False
    v = norm(value)
    tl, tt, ts = _text(text)
    if v and _within(v, tl):
        return True
    unv = re.sub(r"^v(?=\d)", "", v)   # "v2.17.2" is "2.17.2"
    if unv and _within(unv, tl):
        return True
    vt = set(re.findall(r"[0-9]+|[a-z]+", v))
    if vt and vt <= tt:
        return True
    vs = re.sub(r"[^a-z0-9]", "", v)
    if len(vs) >= 5 and _within(vs, ts):
        return True
    m = ISO.fullmatch(value.strip())
    return bool(m and date_said(*m.groups(), text, day))


@lru_cache(maxsize=64)
def said_by_user(text: str) -> str:
    """A chat's user lines only: an 'assistant:' line and the lines after it are
    the assistant's until a 'user:' line (or a '--- ' / 'Conversation on ' header)."""
    out, user = [], True
    for ln in text.splitlines():
        if ln.startswith("assistant:"):
            user = False
        elif ln.startswith(("user:", "--- ", "Conversation on ")):
            user = True
        if user:
            out.append(ln)
    return "\n".join(out)


def idents_stated(value: str, text: str, day: str = "") -> bool:
    """The chat rule: every ISO date in the value is said (or is the chat's day),
    and every word of it with a digit is in the text; the rest may be paraphrased.
    A value with no such word passes."""
    for y, m, d in ISO.findall(value or ""):
        if not date_said(y, m, d, text, day):
            return False
    rest = ISO.sub(" ", value or "")
    _, tt, ts = _text(text)
    for w in norm(rest).split():
        w = w.strip(STRIP)
        if not re.search(r"\d", w):
            continue
        s = re.sub(r"[^a-z0-9]", "", w)
        if len(s) >= 3:
            if not _within(s, ts):
                return False
        elif not set(re.findall(r"[0-9]+|[a-z]+", w)) <= tt:
            return False
    return True


def grounded(value: str, text: str, kind: str | None, day: str = "") -> bool:
    """The rule for a unit of this kind: "note", "chat", or None (no check)."""
    if kind == "note":
        return stated(value, text, day)
    if kind == "chat":
        return idents_stated(value, said_by_user(text), day)
    return True


def named(thing: str, text: str) -> bool:
    """Does the text name this thing ('Brain (LLM)' is named by 'Brain')?"""
    return stated(base_name(thing), text)


def _find(token: str, low: str):
    """Where a value's word is in a line as written: '2000' as '2,000', '3' as 'three'."""
    alts = [re.escape(token)]
    if token.isdigit():
        alts += [",?".join(token)] + [re.escape(w) for w, d in NUM.items() if d == token]
    return re.search(rf"(?<![a-z0-9])(?:{'|'.join(alts)})(?![a-z0-9])", low)


def receipt(text: str, value: str, width: int = SAID_CHARS) -> str:
    """The words a value was read from: the first line that states it and says one
    of its words as a word, cut to at most `width` characters around the longest
    such word at spaces, with '…' where it was cut. '' when no line does: a short
    value stated only inside other words ("off" in "offset"; 48 of 1,520 receipts of
    the 10-19 night replay) gets no receipt rather than a misleading one."""
    vt = tokens(value)
    for line in (text or "").splitlines():
        if line.startswith("--- "):
            continue
        ln = " ".join(line.removeprefix("user:").split())
        shared = sorted(vt & tokens(ln), key=lambda x: (-len(x), x))
        if not shared or not stated(value, ln):
            continue
        if len(ln) <= width:
            return ln
        low = ln.lower()
        at = next((m for m in (_find(t, low) for t in shared) if m), None)
        if at is None:
            continue
        room = width - 2   # one '…' each side
        a = max(0, min((at.start() + at.end()) // 2 - room // 2, len(ln) - room))
        b = min(len(ln), a + room)
        if a > 0:   # start at a word
            sp = ln.find(" ", a)
            a = sp + 1 if 0 <= sp < at.start() else a
        if b < len(ln):   # end at a word
            sp = ln.rfind(" ", a, b)
            b = sp if sp >= at.end() else b
        return ("…" if a > 0 else "") + ln[a:b].strip() + ("…" if b < len(ln) else "")
    return ""
