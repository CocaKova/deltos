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

## Reading

`recall(question)`: find every thing the question names (subject first),
pin the subject's fact lines with their trails, follow `[[links]]` one hop,
then add other lines that mention any of the names: change notes before daily
logs, newest first, capped at 20. One model call answers from the result.
