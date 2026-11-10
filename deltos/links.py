"""Links: an entry whose value names another thing links the two.

Nothing is written for a link. "Host: Calder" on Gristle *is* the link from
Gristle to Calder, labelled host, so it has everything an entry has: the date it
became true, its old values, who said it, the trust order, undo. A link that
changes is a value that changes. The graph is read out of the notes, like the
names table, and rebuilt when they change, so every writer (an agent's tools, a
note import, chat learning, a person's editor) makes links without knowing it.

A value links to every thing it names (recall's own name finder: longest name
first, a name inside a longer name doesn't count, a plain word only where it is
written as a name). The link is exact when the value is the name ("Host: Calder"),
a mention when the name is part of it ("Relationship: Mira's partner"). Kind
entries never link: a kind is a category ("Kind: node" is not the node command).

What reads the graph:
  recall    both ends of a link (what points at the thing asked about), a role
            word through the owner's links ("my grandma"), what depends on a
            thing two links away, the path between two things, and a linked
            thing whose state changed after the entry that points at it was set
  memory    the receipt an agent gets for a write (what a value now links to, a
            name in it memory doesn't know), and pages found through the things
            a question reaches
  lint      links that are unusual for their label (learned from the store)
  health    links an import added and removed, beside its other numbers
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict, deque
from dataclasses import dataclass

from .recall import WORDS

# Entries that are never links. A kind names a category, not a thing ("Kind: node" is not
# the node command). A status is the state a thing is in, not a relation ("Status:
# historical brain"). A setting is a config key's value: recall already holds that it is
# not what two things have to do with each other ("Setting --highvram: ... OOM with 27B brain").
NO_LINK = {"kind", "status"}


def links_label(label: str) -> bool:
    k = label.strip().lower()
    return k not in NO_LINK and not k.startswith("setting")
HUB = 50                    # a thing more links than this meet at is never a step in a path (the owner always isn't)
PATH_HOPS = 3


@dataclass(frozen=True)
class Link:
    src: str        # profile path of the thing whose entry it is ("notes/gristle.md")
    label: str      # the entry's label as written ("Host")
    dst: str        # profile path of the thing the value names
    exact: bool     # the value is the name (not a phrase that mentions it)
    line: str       # the entry's current line
    item: bool = False   # from an "also current:" item under the entry, not its value

    @property
    def key(self) -> str:
        return self.label.lower()


def label_of(line: str) -> str:
    return line.split(":**")[0].removeprefix("- **")


def value_of(line: str) -> str:
    """A current line's value: no "(since ...)" and no retired wording."""
    v = re.sub(r" \((since|confirmed|source:) .*$", "", line.split(":** ", 1)[-1])
    return re.sub(r"^retired / decommissioned.*$", "", v).strip()


def since_of(line: str) -> str:
    """The newest date a person or agent stood behind the line: its since or its confirmed."""
    m = re.search(r"\(since (\d{4}-\d\d-\d\d)[^)]*\)", line)
    if not m:
        m2 = re.search(r"retired / decommissioned since (\d{4}-\d\d-\d\d)", line)
        return m2.group(1) if m2 else ""
    return max([m.group(1)] + re.findall(r"confirmed (\d{4}-\d\d-\d\d)", m.group(0)))


PATHY = set("/\\~.@:_=#%&+")   # characters that join a name into a path, an address, a key or a file name


def _in_words(text: str, start: int, end: int) -> bool:
    """A name is mentioned as a word: what touches it is a space, a bracket, a quote or
    the end of a clause, not a path or address character ("notes/buffer-week1/",
    "include:_spf.mx.cloudflare.net"). A full stop counts only when a space or the end follows."""
    before = text[start - 1] if start > 0 else " "
    after = text[end] if end < len(text) else " "
    if before in PATHY or after in PATHY - {".", ":"}:
        return False
    if after in ".:" and end + 1 < len(text) and not text[end + 1].isspace():
        return False
    token = re.search(r"\S*$", text[:start]).group(0) + text[start:end] + re.match(r"\S*", text[end:]).group(0)
    return not re.search(r"[/\\~]|https?:|\w\.\w+\.\w", token)


CAPS = re.compile(r"(?<![\w'’])([A-Z][\w-]*(?:\s+[A-Z][\w-]*){0,2})(['’]s\b)?")
BOLD = re.compile(r"\*\*([^*\n]{2,60}?)\*\*")
# Month and day names are dates, not things ("the Sep 7 install" names nothing).
# They count as plain words, so like "May" or "August" they are a name only when
# someone's ("June's").
CALENDAR = set("january february march april may june july august september october november december "
               "jan feb mar apr jun jul aug sep sept oct nov dec monday tuesday wednesday thursday friday "
               "saturday sunday mon tue tues wed thu thur thurs fri sat sun".split())


def _plain(word: str) -> bool:
    """A plain English word, or one made of them ("Re-seed", "Follow-up")."""
    w = word.lower()
    return w in WORDS or w in CALENDAR or ("-" in w.strip("-") and all(
        x in WORDS or x in CALENDAR or x in ("re", "co", "pre", "non") for x in w.split("-")))


def unknown_names(recall, text: str, limit: int = 8) -> list[str]:
    """Names in a text that memory has no thing for: what a write receipt points out,
    so the writer can name them and the facts about them can link. A name is a
    capitalized word (up to three in a row) that isn't a plain English word, or
    any capitalized word someone's ("Jesse's"), or a bolded name ("**Kristina**").
    Words opening a sentence or a line count only if they aren't plain words, and plain
    words in front of a name are not part of it ("Then GoDaddy" names GoDaddy)."""
    from .store import name_problem
    known = set()
    for gi, s, e in recall.name_spans(text):
        known.add((s, e))
    covered = lambda s, e: any(a <= s and e <= b for a, b in known)
    out: list[str] = []
    for m in BOLD.finditer(text):
        n = re.split(r"\s+[=—–:-]\s+|[.:;,]", m.group(1).strip())[0].strip()
        if n and n[0].isupper() and not covered(m.start(1), m.start(1) + len(n)) and not name_problem(n) \
                and not recall.profile_of.get(n.lower()) and not all(_plain(w) for w in n.split()):
            out.append(n)
    for m in CAPS.finditer(text):
        n, owns, start = m.group(1), bool(m.group(2)), m.start(1)
        # A plain word in front of a name opens the sentence or titles it ("Then GoDaddy",
        # "The Bitwarden", "Manage DNS"): the name is what follows it. 11-05: a page's
        # receipt listed those three as names memory lacked.
        while " " in n and _plain(n.split()[0]):
            rest = n.split(None, 1)[1]
            start += len(n) - len(rest)
            n = rest
        if covered(start, m.end(1)) or recall.profile_of.get(n.lower()):
            continue
        words = n.split()
        plain = all(_plain(w) for w in words)
        if (plain and not owns) or name_problem(n) or n.isupper():
            continue
        out.append(n)
    seen, res = set(), []
    for n in out:
        if n.lower() not in seen:
            seen.add(n.lower())
            res.append(n)
    return res[:limit]


class Graph:
    """The links of one loaded memory (a deltos.recall.Recall): both ways, by profile."""

    def __init__(self, recall):
        self.r = recall
        groups: dict[str, set] = defaultdict(set)   # profile -> its name groups
        for gi, g in enumerate(recall.names):
            groups[recall.profile_of.get(g[0].lower(), "")].add(gi)
        self._own: dict[str, frozenset] = {p: frozenset(s) for p, s in groups.items() if p}
        self.out: dict[str, list[Link]] = defaultdict(list)
        self.inc: dict[str, list[Link]] = defaultdict(list)
        self.of_line: dict[tuple[str, str], list[Link]] = {}
        for i, (prof, _, line) in enumerate(recall.facts):
            label = label_of(line)
            if not links_label(label):
                continue
            values = [(value_of(line), False)] + [(v, True) for v in recall.items_of.get(i, [])]
            found: list[Link] = []
            for v, item in values:
                for dst, exact in self.resolve(v, prof):
                    found.append(Link(prof, label, dst, exact, line, item))
            if found:
                self.of_line[(prof, line)] = found
                for lk in found:
                    self.out[prof].append(lk)
                    self.inc[lk.dst].append(lk)
        self.degree = Counter()
        for p, ls in self.out.items():
            self.degree[p] += len(ls)
        for p, ls in self.inc.items():
            self.degree[p] += len(ls)

    # -- resolving a value ---------------------------------------------------------

    def resolve(self, text: str, src: str = "") -> list[tuple[str, bool]]:
        """The things a value names: [(profile, exact)], in the order it names them.
        src: the profile the value is on; its own names are not looked for (it can't
        link to itself, and a name of its own like "Ivo's dad" would hide Ivo)."""
        text = (text or "").strip()
        if not text:
            return []
        core = re.sub(r"\s*\([^()]*\)$", "", text).strip()     # "Box 7 (this box)" is Box 7
        core = re.sub(r"^the\s+", "", core, flags=re.I)
        own = self._own.get(src, frozenset())
        out, seen = [], set()
        for gi, start, end in self.r.name_spans(text, value=True, skip=own):
            prof = self.r.profile_of.get(self.r.names[gi][0].lower())
            if not prof or prof in seen or prof == src:
                continue
            span = text[start:end]
            exact = span.lower() == core.lower() or self.r.profile_of.get(core.lower()) == prof
            if not exact and not _in_words(text, start, end):
                continue   # inside a path, an address or a file name: "~/.hermes/hermes-agent/plugins"
            if not exact and span.lower() in WORDS and not any(span == n for n in self.r.names[gi]) \
                    and not (span[:1].isupper() and (start > 0 or re.match(r"['’]s\b", text[end:end + 2]))):
                # A plain word not written as a name: "historical brain" is not Brain. Written
                # as one, it is: capitalized inside the value, or someone's ("Nick's girlfriend").
                continue
            seen.add(prof)
            out.append((prof, exact))
        return out

    # -- reading -------------------------------------------------------------------

    def partners(self, prof: str) -> dict[str, list[Link]]:
        """Every thing linked to prof either way, with the links between them."""
        out: dict[str, list[Link]] = defaultdict(list)
        for lk in self.out.get(prof, []):
            out[lk.dst].append(lk)
        for lk in self.inc.get(prof, []):
            out[lk.src].append(lk)
        return out

    def path(self, a: str, b: str, avoid: set[str] = frozenset()) -> list[Link]:
        """The shortest chain of links from a to b, either way along each link, at
        most PATH_HOPS long, never through a hub (the owner, or a thing more than
        HUB links meet at: every two things it touches would look related)."""
        if a == b:
            return []
        prev: dict[str, tuple[str, Link] | None] = {a: None}
        todo = deque([(a, 0)])
        while todo:
            p, d = todo.popleft()
            if d >= PATH_HOPS:
                continue
            for q, links in sorted(self.partners(p).items(), key=lambda kv: kv[0]):
                if q in prev:
                    continue
                if q != b and (q in avoid or self.degree[q] > HUB):
                    continue
                prev[q] = (p, links[0])
                if q == b:
                    chain, cur = [], b
                    while prev[cur] is not None:
                        before, lk = prev[cur]
                        chain.append(lk)
                        cur = before
                    return list(reversed(chain))
                todo.append((q, d + 1))
        return []

    def dependents(self, prof: str, hops: int = 2) -> list[tuple[str, list[Link]]]:
        """Things that point at prof through exact links (they run on it, are part
        of it, ...), and things that point at those: [(profile, chain to prof)]."""
        out, seen = [], {prof}
        frontier = [(prof, [])]
        for _ in range(hops):
            nxt = []
            for p, chain in frontier:
                for lk in self.inc.get(p, []):
                    if lk.exact and lk.src not in seen and self.degree[lk.src] <= HUB:
                        seen.add(lk.src)
                        out.append((lk.src, [lk] + chain))
                        nxt.append((lk.src, [lk] + chain))
            frontier = nxt
        return out

    def unusual(self, min_uses: int = 20, share: float = 0.02) -> list[tuple[Link, str]]:
        """Links that are rare for their label in this store on the one line the store
        itself draws between kinds, person or not (store.PEOPLE: a person never merges
        with a thing): a label used for 20+ links whose ends are almost never a person
        on one side, on a link where that side is one ("Owner" from 76 things and one
        person). Learned from the store; free-form kinds (tool, cli tool, package) are
        too many to compare."""
        from .store import PEOPLE
        who = lambda p: "person" if self.r.kind_of.get(p, "") in PEOPLE else "thing"
        sides: dict[str, list[Counter]] = defaultdict(lambda: [Counter(), Counter()])
        for ls in self.out.values():
            for lk in ls:
                if lk.exact:
                    sides[lk.key][0][who(lk.src)] += 1
                    sides[lk.key][1][who(lk.dst)] += 1
        out = []
        for ls in self.out.values():
            for lk in ls:
                c = sides.get(lk.key)
                if not lk.exact or not c or sum(c[0].values()) < min_uses:
                    continue
                n = sum(c[0].values())
                for side, end, name in ((0, lk.src, "from"), (1, lk.dst, "to")):
                    w = who(end)
                    if c[side][w] / n <= share:
                        other = "thing" if w == "person" else "person"
                        out.append((lk, f"{lk.label} goes {name} a {other} in {c[side][other]} of {n} links here; "
                                        f"this one goes {name} a {w}"))
                        break
        return out
