# Memory format v0 (working name: deltos)

Memory that knows when it's wrong. Plain markdown in a folder (ideally a git
repo), readable and editable by people, written by agents through four
operations, read with one ripgrep pass. No database, no embeddings required.

## Principles

1. **One current line per fact.** A reader never has to work out which of
   several statements is the live one.
2. **Old values stay, labeled.** Every replaced value moves under the current
   line as `was:` with the dates it held. History is kept *and* can't be
   mistaken for the present.
3. **Two kinds of wrong.** A value that *changed* becomes `was:`. A value that
   was *never true* (a recording mistake) becomes `was recorded as … by
   mistake (…; never true)` and is never offered as history.
4. **Bounded hot path.** Two past values stay inline (warm). Older ones move to
   a per-thing history note (cold), linked with `[[thing-history]]`. A profile
   stays small however long the thing lives.
5. **Agents don't hand-edit facts.** They say what they learned; the store
   decides where it goes. Repeating the current value is a confirmation, news
   that arrives late lands in the past, a correction marks the old value.
6. **Receipts.** An import keeps the sentence a current value was read from
   under it as `said: "…" (date)`, so a reader can see why memory believes
   it. A new value brings its own receipt; a value said again keeps the one
   it has. The block shown to an agent shows them (`DELTOS_RECEIPTS=0`
   leaves them out).
7. **Trust order.** A person's value (`source: hand`, or a line written by hand
   without a date) stands against an agent's and a note import's; an agent's
   (any other source, or a confirmation) against a note import's. A less trusted
   writer replaces it only with news dated after the day it was set or last
   confirmed; older news still lands in the trail, and it can report a change
   but never mark the value never true. The refusal says who set it and when.
   A note line the text gives no date for counts from the earliest day it could
   have been written: for a commit's lines, the day of the commit before it
   (a nightly commit carries yesterday's edits).
8. **Things have many names.** Every name a team uses is registered once, so a
   question about "TS-2" finds Tamsin.

## Layout

```
MEMORY.md                       index, one line per thing (generated)
notes/<thing>.md                profile: names, about, current facts + warm trail
notes/<thing>-history.md        cold tier: values that fell off the trail
notes/log-YYYY-MM-DD.md         daily log: things that aren't facts
```

## Profile

```markdown
---
title: "Tamsin"
kind: "machine"
date: "2026-01-09"          # created
updated: "2026-09-27"       # last write
volatility: {location: high, serial number: static, status: high}
---

# Tamsin

Tamsin (also called TS-2, the big GPU box).
Free text about the thing; kept as is.

## Details

- **Location:** the Annex closet (since 2026-05-09; confirmed 2026-06-01; source: standup)
  - said: "Tamsin moved to the Annex closet this morning." (2026-05-09)
  - was: Bay 3 rack 12 (2026-03-06 to 2026-05-09)
  - was: the north lab bench (2026-01-09 to 2026-03-06)
  - 1 older value(s) in [[tamsin-history]]
- **Serial number:** SN-48213-K (since 2026-01-23)
  - was recorded as SN-11542-T by mistake (corrected 2026-02-14; never true)
- **Status:** retired / decommissioned since 2026-09-21
```

- `since` is when the value became true in the world (valid time). The note's
  `updated` and git history say when memory learned it (record time).
- `confirmed` appears only when someone re-observed the current value later.
- A line a person writes by hand without dates (`- **Owner:** Arun`) is a
  fact with an unknown start; when it's replaced it shows as
  `was: Arun (until 2026-03-01)`. Sub-lines and sections a person adds that
  aren't in this format are kept as written, never dropped.
- `volatility` (static / low / high) says how fast a fact goes stale; readers
  and future tools use it to decide when to check reality instead of trusting
  memory.

## Cold history

```markdown
# Tamsin history (archived values)

Past values for Tamsin that are no longer current. Not current facts.

- location: Dock B shelf 4 (2026-01-05 to 2026-01-09)
- serial number: SN-0000-X (2026-01-02 to 2026-01-02) (recorded by mistake, never true)
```

## Operations

| Operation | Effect |
|---|---|
| `name(thing, aliases, kind)` | create a thing or add names; a name belongs to one thing |
| `remember(thing, attribute, value, since, source, correction, volatility)` | new value → current, old → trail; same value → confirmation; older `since` → placed in the trail; `correction` → old marked never true |
| `note(text, day)` | append to the daily log |
| `show(thing)` | the profile and its cold history |
| `as_of(thing, day)` | time travel: each entry's value on that day, from the current line, trail and cold history; never a value recorded by mistake |
| `forget(thing, attribute, reason, source, user_asked)` | delete a whole thing (files, names, index row) or one entry with its trail; the files move to `.trash/<day>-<thing>/` with a `forgotten.json` receipt and a line in `forget-log.jsonl` |
| `restore(thing)` | undo the newest forget of that thing or entry; refused if its name now belongs to another thing |
| `purge(thing)` | a person only (CLI, no agent tool): delete that thing's trash for good; git history may still hold it |

`value` = `retired` renders as `retired / decommissioned since …`.

What the store refuses (the receipt says why, nothing is written):
dates that aren't `YYYY-MM-DD` (they're compared as text, so `2026-9-5` would
sort after October), dates more than a year after today (a dated plan within a
year is a fact with its date), values over 10 words or with line breaks, attributes with symbols
(`:`, `*`, brackets, commas), and names that are really values, placeholders,
single letters or generic words ("it", "the service").

Forgetting is for what should never have been in memory (a test, junk, a
thing made by mistake, something the owner asked to drop). A changed value is
`remember`, a wrong one is a `correction`: those keep the history; forgetting
removes it. Forgetting is undoable and always says why (`reason` is required).
An agent (any `source`) gets ten forgets a day, and a whole thing with five or
more entries, or one another thing points to (`runs on` / `part of` it), only
with `user_asked`. A person at the CLI has neither limit.

Backdated news is merged, not stacked: the same value from an earlier date
moves that value's start back; a dated repeat of a value already in the trail
is a no-op.

## Files and writers

Each thing gets its own file stem. Two names that slug alike get `-2`; a name
that would land on a daily log or a history note gets `thing-…` / `…-thing`.
The stem is the file name and doesn't change if the title is edited.

Notes are written to a temp file and renamed, so a killed or paused process
never leaves half a note. Writers in several processes share a folder through
an advisory lock (`.deltos.lock`); a store that sees `MEMORY.md` changed by
someone else re-reads the names before writing.

## Learning from chats

A finished chat is learned once, and only the owner's words ground a fact.
A user turn is read by who wrote it, from the turn itself:

- **harness**: a turn that opens with a bracketed tag in capitals
  (`[CONTEXT COMPACTION — …]`, `[STILL IN PROGRESS — …]`) or a known notice.
  It is the harness talking, so it is not read. A tag that says it carries the
  user's message (`[OUT-OF-BAND USER MESSAGE …]`) is opened, and what it
  carries is read by these same rules.
- **relay**: a turn that names its writer (`[From Orin, for Ava …]`,
  `[From Ava, relayed by Orin]`). It is another agent's words, shown as context
  only.
- **owner**: everything else, read as typed.

A value only a relay or the assistant states is refused, whatever its words.
A chain of sessions (a compacted chat going on in a child session) is one
conversation, learned once its last session ends.

## Reading

`recall(question)`: find every thing the question names (subject first),
pin the subject's fact lines with their trails, follow `[[links]]` one hop,
then add other lines that mention any of the names: change notes before daily
logs, newest first, capped at 20. One model call answers from the result.

## Links

An entry whose current value names another thing links the two. Nothing is
written for a link: `- **Host:** Calder` on Gristle *is* the link from Gristle
to Calder, labelled host, with the entry's dates, trail, source and trust. A
link that changes is a value that changes; a value moved to the trail is no
longer a link. The graph is read from the notes (deltos/links.py), so every
writer makes links.

- **Exact** when the value is the name (`Host: Calder`, `Host: Calder (rack 2)`),
  a **mention** when the name is part of it (`Relationship: Mira's partner`).
  Names are found the way a question's are (longest name first, a name inside a
  longer name doesn't count, skipping the entry's own thing's names).
- A mention counts only as a word: not inside a path, an address or a file name
  (`~/.hermes/hermes-agent/plugins`), and a name that is a plain English word
  only where it is written as a name (`historical brain` is not Brain;
  `Nick's girlfriend` names Nick).
- Never links: `Kind` (a category), `Status` (a state), `Setting ...` (a config
  key's value).
- A person known only by what they are to someone is a placeholder name
  (`Ivo's dad`, kind person). Its profile starts with `Relationship: Ivo's dad`,
  which links it to Ivo; naming the person later with the placeholder as another
  name (`name("Rowan", ["Ivo's dad"])`) makes the name the profile's (the file
  follows) and keeps the placeholder as another name.

With `[recall] links = true` the block shows both ends of a link, resolves a
role word through the owner's links, lists what depends on a thing two links
away, gives the chain between two things the question names (worked out, not
stored: memory records what was said, the reader follows the chain), and puts
`  - linked: <thing> status <value>` under an entry whose linked thing was
retired, or whose status changed after the entry was set when the question asks
about that entry.

A product and one unit of it are two things. A unit is known by what only it has
(an address, a serial, a host name that is one of its own names); a price or a
release date is the same for every unit. When one is written on a unit, the write
stands and its receipt says where it belongs (the product's own thing, linked
from the unit by `model`); `deltos doctor` lists the units that hold one.

## Checks

Notes say what was true when they were written; a check is someone looking
again. A person or an agent that just saw a value says how:
`remember(thing, attribute, value, check="hermes --version")`. The same value
confirms the entry (`confirmed <today>`), another one replaces it from today
(or from the `since` the writer gives: a value known to have held since an
earlier day is still confirmed today, because it was seen today), and the entry
keeps how to look, under its line:

```markdown
- **Version:** 0.22.4 (since 2026-10-22; confirmed 2026-10-26)
  - check: hermes --version on box-7
```

- How to look stays when the value changes (how you look doesn't change when
  what you see does). It is one line of at most 25 words and never holds a
  password, token or key; deltos never runs it.
- An import never writes a check: a note can't look.
- Each look is a row in `checks.jsonl` (day, thing, attribute, value before,
  value seen, outcome `same` / `changed` / `new` / `held`, how, who). `held` =
  what was seen did not become current (trust order, or an older date).
  The share of `changed` among known values is memory's own measured error on
  live state, per label (`deltos checks --stats`).
- What is due (`deltos checks`, the `memory_checks` tool): current entries
  whose label drifts in live state (status, version, host, port, model, ...),
  or any entry with a way to check, not seen (since or confirmed) in 21 days,
  ranked by days since seen × (1 + times the agent's blocks named the thing in
  30 days). Retired things and values are left alone.
- With `[checks] on = true`: agents get `remember`'s `check` and
  `memory_checks`, and a `check:` line shows in the block only under a line
  marked "not confirmed in N weeks" (`[recall] doubt_days`). Off (the
  default), the store still keeps check lines and the CLI still works.
- A note never overturns a look. An import line that disagrees with a value a
  person or agent saw (the entry has a `check:` line) is kept beside it, not over
  it, whatever the line's date:

  ```markdown
  - **LAN address:** 10.0.0.87 (since 2026-05-03; source: Ava)
    - check: ping 10.0.0.87 answered
    - said otherwise: 10.0.0.116 (a note, 2026-05-05; not seen)
  ```

  The entry is due for a look first (`deltos checks`). A look, a confirmation or
  a new value from a person or agent takes the line off.
