# Deltos

**Memory for AI agents that knows what's true now, what it used to be, and when it isn't sure.**

An agent's worst memory failure isn't "I don't know". It's answering confidently
with the value that changed last week. Deltos keeps, for every thing in your
world, the value that's true now, the date it became true and every value it
replaced, as plain markdown you can read, edit and diff. No index server, no
embeddings and no model on the read path: building a turn's memory takes well
under a second.

**Status: 0.1, early.** It has been one agent's daily memory for weeks, and the read
path is the steady part. The nightly import (notes and chats into facts) is the young
part: how well it does depends on the model you point it at (measured on three).
Expect changes before 1.0.

Tested on [LongMemEval](https://github.com/xiaowu0162/LongMemEval) (Wu et al.,
ICLR 2025; MIT) and compared against [Mem0](https://github.com/mem0ai/mem0)
(Apache-2.0) and [Graphiti](https://github.com/getzep/graphiti) (Apache-2.0), then on a
second benchmark, [LoCoMo](https://github.com/snap-research/locomo) (Maharana et al.,
ACL 2024; CC BY-NC 4.0, downloaded at run time, not bundled), with three different
answering models. LongMemEval's grading prompts are used unchanged. Recall ships
[SCOWL](http://wordlist.sourceforge.net/)'s English word list (Kevin Atkinson et al.).
Credits and licenses: [THIRD-PARTY.md](THIRD-PARTY.md).

- [Results](#results) · [Confidently wrong](#confidently-wrong) · [Why Deltos misses](#why-deltos-misses) · [Read this before quoting the numbers](#read-this-before-quoting-the-numbers) · [Not in v1](#not-in-v1--next)
- [How it works](#how-it-works) · [Use it](#use-it) · [Reproduce](#reproduce) · [The first benchmark: a synthetic world](#the-first-benchmark-a-synthetic-world)

## Results

### Three models, and a fresh test

On every board below Deltos gives the most right answers, and its confidently
wrong answers are level with the best system's.

**LoCoMo fresh** is the clean test: 500 LoCoMo questions drawn with a fixed seed
from the ones we had not used, their answers unread until the change they test was
settled (`bench/locomo.py`). The LongMemEval boards are the 332 held-out questions
of the next section, studied since, so read them as after fixes.

| Board | Answers and grades | Deltos | Mem0 2.2 | Plain search |
|---|---|---|---|---|
| LoCoMo fresh (500) | DeepSeek-V4.1-Flash | **391** / 75 | 352 / 87 | 292 / 103 |
| LoCoMo fresh (500) | GLM-5.3-Flash | **387** / 67 | 364 / 73 | 312 / 72 |
| LongMemEval (332) | DeepSeek-V4.1-Flash | **303** / 18 | | 259 / 23 |
| LongMemEval (332) | GLM-5.3-Flash | **285** / 17 | | 254 / 15 |
| LongMemEval, Mem0's 100 | DeepSeek-V4.1-Flash | **88** / 6 | 83 / 4 | 73 / 7 |
| LongMemEval, Mem0's 100 | GLM-5.3-Flash | **83** / 6 | 81 / 5 | 70 / 6 |

Right / confidently wrong. Confidently wrong is counted over all 500 on LoCoMo and
over the questions with a factual answer on LongMemEval (302 of 332, 80 of 100).

Paired on the same questions (sign test), Deltos is ahead of plain search beyond
luck on all six boards (p ≤ 0.015), and ahead of Mem0 beyond luck on both LoCoMo
boards (p < 0.001, p = 0.03) but within luck on LongMemEval's 100 (p = 0.33, 0.79).
Confidently wrong: fewer than both on both LoCoMo boards, slightly more in three
LongMemEval cells (17 vs 15 against plain search on GLM; 6 vs 4 and 6 vs 5 against
Mem0), and only one difference is beyond luck: fewer than plain search on LoCoMo
with DeepSeek (p = 0.01).

Each system's memory was built once per benchmark and kept: LongMemEval's by
Qwen3.8-Flash-Next and LoCoMo's by DeepSeek-V4.1-Flash,
for Deltos and Mem0 alike. The boards change the model that answers and grades.
LoCoMo's chats are two people talking: Deltos reads both sides, and Mem0 gets one
memory per speaker, as its own LoCoMo evaluation does. A question the conversation
never answers (LoCoMo's adversarial category) is graded as unanswerable with
LongMemEval's abstention check. The rule that checks whose line a fact is (on by
default since 10-18) was made on a separate practice 500 of LoCoMo. Mem0 has no
332-question rows (its memory takes far longer to build) and Graphiti wasn't re-run.
One run each. The rows are in [data/results/two-models](data/README.md), LoCoMo's
without question or answer text.

### The first held-out run (Qwen3.8-Flash-Next)

LongMemEval-S (cleaned): each question comes with about 50 past chats (~115K
tokens), and the system has to answer from its memory of them. Every system gets
the same chats, the same answering model and prompt, and the same grader; only
the memory changes. The Deltos rows are the memory block for the question plus
up to three rounds of lookups the answering model can make itself (`page_read`,
`page_search`, `memory_recall`, `memory_show`); what those return counts as
memory shown.

**Held-out questions only.** We built Deltos looking at the first 30 questions of
three question types. The rows below are the 332 questions we had not looked at
when these numbers were taken (the rest of those three types, and all of the
single-session-user and single-session-preference questions). One run each.

| Memory | Right (332) | Confidently wrong | Memory shown per question | Building memory, per chat |
|---|---|---|---|---|
| **Deltos** | **295 (88.9%)** | 21 (7.0%) | ~2,150 tokens | 1 model call (+1 repair call for 11% of chats), ~1,200 tokens |
| Plain search (BM25, top 10 chat rounds) | 254 (76.5%) | 22 (7.3%) | ~4,000 tokens | none |

Confidently wrong is counted over the 302 questions with a factual answer
(preference questions left out, see below). Tokens shown are characters ÷ 4.

| Question type | Deltos | Plain search |
|---|---|---|
| multi-session (103) | 83 | 66 |
| temporal reasoning (103) | 91 | 79 |
| single-session, assistant said (26) | 26 | 25 |
| single-session, user said (70) | 68 | 69 |
| single-session, preference (30) | 27 | 15 |
| of which: unanswerable, says so (24) | 21 | 22 |

### Against other memory systems

Same held-out pool, fewer questions because their memory takes far longer to
build: 100 for Mem0 (the first 20 held-out questions of each type) and 50 for
Graphiti (the first 10, a subset of Mem0's), with Deltos and plain search scored
on the same questions. One run each.

| Memory | Right (100) | Confidently wrong (80) | Memory shown per question | Building memory, per chat |
|---|---|---|---|---|
| **Deltos** | **90** | 5 | ~8,700 chars | ~1.1 calls, ~1,200 tokens |
| Mem0 2.2 | 82 | 4 | ~3,800 chars | 1 call, ~11,900 tokens, ~49 s |
| Plain search | 66 | 7 | ~15,900 chars | none |

| Memory | Right (50) | Confidently wrong (40) | Memory shown per question | Building memory, per chat |
|---|---|---|---|---|
| **Deltos** | **44** | 3 | ~8,700 chars | ~1.1 calls, ~1,200 tokens |
| Graphiti 0.30 | 19 | 5 | ~7,000 chars | ~18 calls, ~96,000 tokens, ~217 s |
| Plain search | 32 | 3 | ~15,600 chars | none |

Per type, of 20: Deltos and Mem0 tie on multi-session (16 each) and are close on
temporal reasoning (18 / 17) and single-session-user (19 / 20); Deltos leads on
what the assistant said (20 / 14: Mem0 keeps few of the assistant's facts) and on
preferences (17 / 15). On confidently wrong the two are level (5 vs 4 of 80); Mem0
says so when it's wrong more often (9 of 13 misses vs Deltos's 2 of 7).

**Graphiti, read before quoting:** we checked each of its 31 misses against its
graph, with no model calls ([the audit](data/graphiti-check.md)): 25 were lost when facts
were pulled out of the chats (the answer is in the chat Graphiti stored but never
became a fact, most often a detail from early in a long chat), 1 was a search
miss, 4 were wrong answers from the right facts, and 1 has an answer key that
doesn't match the chat dates. Graphiti's own errors were negligible (no failed
chat, about 1.5% of extracted facts dropped). So this row reflects Graphiti's
extraction with this local model and a whole chat per episode; a stronger model,
smaller episodes or its cross-encoder search might score differently, and we
didn't test those.

Deltos turns only the user's side of each chat into facts and keeps every chat
whole as a searchable page; Mem0 and Graphiti read both sides. Each system uses
its library's documented defaults ([bench/others.py](bench/others.py) lists every
setting). Seconds are wall time with at most three requests at a time to one
local model.

### After fixes

After the held-out run we sorted its 37 misses by cause (below) and fixed the
biggest one: a second item of the same kind (another fundraiser, another course)
replaced the first instead of sitting beside it. Because we studied these
questions, the numbers below are **after fixes**, not held-out. A clean test on
unseen questions is still to come.

| | Fix off | Fix on (2 runs) |
|---|---|---|
| multi-session, held out (103) | 83 / 77 / 84 | 89 / 86 |
| temporal reasoning, held out (103) | 91 / 89 / 87 | 90 / 88 |
| knowledge update (78, all used in development) | 73 | 74 / 76 |
| multi-session, first 30 (studied) | 20 / 20 | 20 / 20 |
| temporal reasoning, first 30 (studied) | 27 / 28 | 29 / 29 |

The fix fixed 4 held-out multi-session questions in both runs that every earlier
run got wrong, and lost none of the 73 that every earlier run got right.
Temporal reasoning didn't move. The fix asks the model about each replaced value
while building memory, 12 to a call; these runs had its thinking off, about 3
extra calls per question's ~50 chats. In `deltos import` it is on by default
(`[import] items = false` turns it off), and since 10-20 the model reasons before
it answers: with thinking off GLM-5.3-Flash read "pi-media now also runs Pi-hole"
as a replacement. On 46 cases (40 written, 6 from real notes, graded blind) it
kept 22 of 24 second ones with thinking on against 19 off, and called none of
the 22 true updates "another" either way; DeepSeek-V4.1-Flash-1017, thinking on,
kept 20 of 24 and also called none of them "another". Only labels that hold one value by
definition (kind, status, version, latest release, relationship, a setting)
skip the check; a value stored before the check was on is asked about with its
receipt.

## Confidently wrong

A wrong answer that says "I'm not sure" or "I have no record" is a nuisance; a
wrong answer stated as fact is how memory misleads you. We label each answer by
how it's put (sure / unsure / no answer) with the same model that grades
correctness, cache the labels, and count:

- **confidently wrong**: wrong and stated as fact (lower is better)
- **says so when wrong**: the share of wrong answers that flag doubt or decline
- **hedged when right**: right answers that hedged anyway, so a system can't win
  by doubting everything

Held out (302 questions with a factual answer):

| | Right | Confidently wrong | Says so when wrong | Hedged when right |
|---|---|---|---|---|
| Deltos | 268 | 21 (7.0%) | 38% (13 of 34) | 16 |
| Plain search | 239 | 22 (7.3%) | 65% (41 of 63) | 5 |

**Honest reading:** Deltos is right far more often, but it is confidently wrong
about as often as plain search. When plain search can't find something it
usually says so (34 of its 63 misses are "I have no record"). Deltos finds more,
and when it's still wrong it usually doesn't know it. 13 of its 21 confident
misses are "how many / how much" questions, and most of those come up short:
memory can't know about an item it never stored. 5 of the 21 are questions where
the grader or the gold answer looks wrong or ambiguous (see below).

The fix above (after fixes, all 344 questions in that table) cut confidently
wrong answers from 34.8 per run (mean of the fix-off runs) to 25.5 (mean of the
fix-on runs).

### Tried, mixed: a reading rule for counts

Since most confident misses are counts that come up short, we tried telling the
answering model that a count from memory is a floor ("3 on record, maybe more").
Same 344 questions (after fixes), two runs with the rule against two without:

| Per run | Without | With the rule |
|---|---|---|
| Right | 300.5 | 299.5 |
| Confidently wrong | 25.5 | 19.5 |
| Hedged when right | 16.5 | 47 |

A quarter fewer confident misses, but none of that gain came from the held-out
multi-session questions, where most of the counts are (7.5 → 8.5). Count answers
got slightly less accurate (about 3 more wrong per run; one added up every
fundraiser on record instead of the ones asked about), and the hedging spread
to answers that were already right, including questions that aren't counts.
The rule ships off; `DELTOS_READING_COUNT=1` turns it on.

## Why Deltos misses

The 37 held-out misses, each checked against the memory the reader actually used:

| Cause | Misses |
|---|---|
| The answering model misread facts it was shown | 11 |
| One item overwrote a different one (fixed above) | 10 |
| The grader or the gold answer looks wrong | 7 |
| A detail never made it into memory | 5 |
| The fact was stored but not found | 4 |

## Read this before quoting the numbers

- **Single runs.** Every headline number is one run. The answering model varies
  run to run: three runs over byte-identical memory scored 83, 77 and 84 of 103
  multi-session questions. A gap of a few questions between single runs means
  nothing. Means and ranges over several runs are still to come.
- **The model that answers also grades.** On each board one local model answers
  every question, grades every answer with LongMemEval's grading prompts, and
  labels how sure each answer sounds: Qwen3.8-Flash-Next (NVFP4) in the sections
  below, DeepSeek-V4.1-Flash or GLM-5.3-Flash on the boards above. The paper's
  leaderboard grades with GPT-4o, so these scores are comparable to each other,
  not to published ones. The sure / unsure labels haven't been checked by hand.
- **Measured before the 10-19 changes.** The boards are from 10-18. What changed
  since (imports as staged runs, values checked against their text, the nightly
  job) is on the writing side; each change was checked without a model on the
  gate's frozen suites, where no question got worse.
- **Knowledge-update questions were all used in development**, so they're
  reported only under "after fixes".
- **Made-up chats.** LongMemEval's chats are tidy; real notes are messier.

## Not in v1 / next

- **Averaged runs.** Mean and range of several runs for the headline rows.
- **Memory building compared across models.** Each benchmark's memory was built
  by one model (see Results). The note import is scored on two: on a 71-fact
  import test its prompt finds 61 on DeepSeek-V4.1-Flash and 60 on GLM-5.3-Flash,
  and it runs nightly on GLM, which repeats itself at temperature 0
  (`[import] temperature = 0.6` fixes that there).
- **Learning from chats ships off.** `deltos learn` turns finished chats into
  facts. On the author's real chats 14% of the facts it kept were wrong, against
  a 10% bar, measured on the same chats its last fix came from. It needs a test
  on new chats before `[learn] auto` defaults on.
- **One prompt example to neutralize.** The import prompt's worked example names
  the author's own app. A refusal message's example name, which the model copied
  into 11 of the 15,867 memory-building outputs, is now a made-up one; that swap
  wasn't scored on two models first, against this project's own rule.
- **The agent's own save tool keeping a second item.** The import's items check
  keeps "another one" beside the first; the agent's `remember` tool still
  replaces it. An `another` option is being added (`DELTOS_REMEMBER_ANOTHER=1`,
  off until it's scored on two models).
- **Time travel for the agent.** `memory_show` takes `as_of` behind
  `DELTOS_SHOW_AS_OF=1` until it's scored on two models (`bench/asof.py`). On
  GLM it changed nothing (13 of 14 with and without it; the memory block already
  carries the dated history a question about the past needs). The command
  line's `show --as-of` always works.
- **Who confirmed.** A confirmation counts as an agent's (only a person or an
  agent confirms), and values written before sources were kept count as a note
  import's. A line written by hand without a date has no date to compare, so any
  dated note can change it.

## How it works

- **Things.** One profile per thing (a machine, a service, a person, a repo).
  Each entry is the current value with its date and a short `was:` trail. Older
  values move to a history note, and a value recorded by mistake is kept as
  *never true*. A second item of the same kind can stay current beside the
  first. Things link to each other (`runs on`, `part of`).
- **Pages.** Documents for what isn't a fact: decisions, how-tos, write-ups,
  and a digest per day of every chat. Some folders are writable, some read-only.
- **Sources.** Where knowledge comes from: an agent's tool calls, note repos
  (read by a model, in the background) and chat histories. All of them go
  through the same write rules.

A profile entry:

```
- **Host:** nas (since 2026-09-18; source: assistant)
  - was recorded as backup-box by mistake (corrected 2026-09-20 by Sam; never true)
  - was: pi (2026-08-01 to 2026-09-18)
  - 2 older value(s) in [[backup-job-history]]
```

**The write rules** (the store is the only gatekeeper):

- One fact, one entry: `build host`, `Setting build host` and `build_host` are the same.
- A repeat confirms; a backdated value goes into the trail, never over the current one.
- Names are names: no versions, labels, commit refs, or another thing's name
  (a person never merges with a non-person, and a wrong merge can be undone).
- A status is a state in a few words. Events, run logs and measurements are set
  aside next to the profile: kept and searchable, but not current facts.
- **Trust order.** A value a person set stands against an agent and a note
  import; one an agent set (or confirmed) stands against a note import, unless
  the news is dated after it. A note that restates an old value can't quietly
  undo a fix: the import logs the line as held instead. Older news still goes
  into the trail. A less trusted source can report a change, but never mark a
  more trusted value "never true".
- **Receipts.** The import keeps the sentence a current value was read from
  under it (`said: "..."`), so you can see why memory believes it.

**The read path** finds the things a message names (or describes), takes their
entries and one hop of links (older values only when the question asks about the
past), adds the best-matching pages and fits the block to a budget.

**Time travel.** `deltos show <thing> --as-of 2026-04-01` gives each entry's
value on that day, from the dates memory already keeps (no extra storage):

```
# Tamsin on 2026-04-01
- **Location:** Bay 3 rack 12 (2026-03-06 to 2026-05-09)
- **Serial number:** SN-48213-K (since 2026-01-23, still current)
- **Status:** nothing known on that day (first value from 2026-09-21)
```

A value recorded by mistake is never offered. This is when values were true,
as memory knows it now; what memory believed on a day is in its git history.

The file format is specified in [deltos/SPEC.md](deltos/SPEC.md).

## Use it

### Install

From source (Python 3.11 or newer, standard library only):

```bash
git clone https://github.com/CocaKova/deltos ~/deltos-src && cd ~/deltos-src
python3 -m deltos --help
```

There is nothing to install: `python3 -m deltos` runs from the clone. For a
`deltos` command anywhere, `pip install -e .` in a venv or with pipx (Debian and
Ubuntu refuse pip installs into the system Python), or
`alias deltos='PYTHONPATH=~/deltos-src python3 -m deltos'`.

Memory lives in `~/deltos`, or `$DELTOS_ROOT`, or `--root <folder>`, so keep the
code in another folder. It's plain markdown; keeping it in a git repo is a good idea.

### Python

```python
from deltos import Memory

m = Memory(agent="assistant")      # ~/deltos (or $DELTOS_ROOT); agent = the source recorded on facts
m.call("name_thing", {"thing": "backup job", "other_names": ["nightly backup"], "kind": "service"})
m.call("remember", {"thing": "backup job", "attribute": "host", "value": "nas", "since": "2026-09-18"})
print(m.context("where does the nightly backup run?"))   # the per-turn memory block
m.tools, m.instructions()          # the tool schemas and instructions to give an agent
```

`remember` only takes names memory knows: register a new thing, or another name
for a known one, with `name_thing` first. A write the store refuses raises
`deltos.Refused` with the reason.

### Command line

```
deltos context <message>          the memory block for a message (what a harness injects per turn)
deltos recall <query>             the same search, with more pages
deltos show <thing> [--history]   everything on one thing
deltos show <thing> --as-of YYYY-MM-DD   each entry's value on that day
deltos remember <thing> <attribute> <value> [--since YYYY-MM-DD] [--correction]   (source: hand)
deltos name <thing> [other names...] [--kind K] [--remove NAME ...] [--merge]
deltos forget <thing> [--attribute A] [--reason R]    undoable: deltos restore <thing>
deltos forgotten [-n N]           the latest forgets
deltos purge <thing>              delete a forgotten thing's trash for good
deltos search <text>              exact text across pages
deltos read <ref>                 a whole page
deltos doctor [--fix] [--review [N]]   health numbers; --fix moves junk out of profiles
deltos sessions [--days N]        rebuild the daily chat digests
deltos import [--limit N]         note repos -> facts, with the configured model
deltos serve                      MCP server on stdio
deltos hook claude-code           Claude Code prompt hook: the memory block for the prompt on stdin
```

Global options: `--root <folder>` and `--agent <name>` (the source recorded on
facts written through this command). Without `--agent`, `deltos remember` is
you, by hand (`source: hand`), the top of the trust order; `deltos serve`
writes as `agent`.

### MCP server

`deltos serve` speaks MCP over stdio (newline-delimited JSON-RPC, stdlib only).
Any MCP client gets the tools `memory_recall`, `memory_show`, `remember`,
`name_thing`, `forget`, `memory_restore`, `page_search`, `page_read`,
`page_write`, `page_edit` and `page_forget`, and the memory instructions arrive
with the handshake. For a client configured by JSON:

```json
{ "mcpServers": { "deltos": { "command": "deltos", "args": ["--agent", "assistant", "serve"] } } }
```

MCP has no per-message context, so a client that can't inject text per turn
relies on the agent calling `memory_recall`. The harness adapters below add the
per-turn block.

### Claude Code

Tools: `claude mcp add deltos -- deltos serve`. The memory block on every
prompt comes from a `UserPromptSubmit` hook; see
[adapters/claude-code](adapters/claude-code/README.md).

### Hermes

A memory provider plugin: the per-turn block, the tools and the instructions.
See [adapters/hermes](adapters/hermes/README.md).

### Configuration

Settings are read from `$DELTOS_CONFIG`, else `<root>/deltos.toml`. Every key is
optional:

```toml
owner = "Sam"                        # whose memory this is (the instructions name them)
agent = "assistant"                  # the agent writing through an adapter (each fact's source)

[pages]
write = "~/notes"                    # documents the agent may edit
read = { wiki = "~/team-wiki" }      # read-only folders, by prefix

[hermes]                             # the Hermes adapter
extra_block = "~/.hermes/extra.py"   # optional: a file whose build_block() is added to the instructions

[sessions]                           # daily chat digests under <root>/sessions
hermes = "~/.hermes"

[learn]                              # deltos learn: finished chats -> facts (needs [llm], owner, [sessions])
auto = false                         # true: each chat is learned after it ends, while the agent is idle;
                                     # false (default): only `deltos learn` by hand

[import]                             # deltos import: notes -> facts (needs [llm]); both sources optional
folders = ["~/notes:notes"]          # any folder of markdown as path:label, no git needed: each note's
                                     # new lines since the copy imported last (<root>/.sources/<label>/)
repos = ["~/wiki:HEAD:wiki"]         # git repo : revision : label; each commit's added
                                     # lines in .md files, dated by the commit
skip = ["ARCHIVE", ".bak"]           # file names containing these are skipped
workers = 2
temperature = 0.6                    # unset sends 0; some models repeat themselves at 0
notes_prompt = "v3"                  # fenced reference + worked example; "v1": the first prompt
items = true                         # the default: before a new value replaces one under the same
                                     # label, the model says whether it is another one of the same
                                     # kind; decisions cached in <root>/items.json. false: it replaces

[llm]                                # any OpenAI-compatible endpoint
url = "http://127.0.0.1:8000/v1"
model = "your-model"
key_env = "DELTOS_LLM_KEY"           # the key is read from this variable, never stored here
thinking = "off"                     # deltos's own calls: "off" (default) | "on" | "server"
# How this server turns thinking off; unset = probed once and remembered:
# thinking_off = { chat_template_kwargs = { enable_thinking = false } }
# thinking_on = { chat_template_kwargs = { enable_thinking = true } }
# extra = { top_k = 20 }             # sent with every request
```

Only `deltos import`, `deltos learn` (and the items check) call a model. Reading
memory, writing facts, the MCP server and the hooks never do. Each import or learn
sweep is a staged run: it writes to a copy of memory, is judged against the
store's own accepted runs, and is promoted or held (`deltos status`,
`deltos run show|promote|discard|undo <id>`).

Environment:

| Variable | What it does |
|---|---|
| `DELTOS_ROOT` | the memory folder (default `~/deltos`) |
| `DELTOS_CONFIG` | the settings file (default `<root>/deltos.toml`) |
| `DELTOS_LLM_URL`, `DELTOS_LLM_MODEL`, `DELTOS_LLM_KEY` | the model, when `[llm]` doesn't set it |
| `DELTOS_LLM_THINKING`, `DELTOS_LLM_THINKING_OFF`, `DELTOS_LLM_THINKING_ON`, `DELTOS_LLM_EXTRA` | the `[llm]` thinking and extra fields (JSON for the last three) |
| `DELTOS_ITEMS_EXTRA` | extra request fields (JSON) for the items check |
| `DELTOS_READING_COUNT=1` | the count reading rule (off; see Results) |
| `DELTOS_READING_WHOSE=0` | leaves out the whose-line-is-it reading rule (on since 10-18, scored on two models) |
| `DELTOS_REMEMBER_ANOTHER=1` | the `remember` tool's `another` option (off until scored) |

The servers' thinking switch, once probed, is remembered in
`~/.cache/deltos/llm-thinking.json`.

### Layout

```
deltos/    memory.py (the one surface) · store.py (write rules) · recall.py (read path)
           pages.py · doctor.py · items.py · config.py · cli.py · mcp.py · llm.py
           sources/notes.py (note repos -> facts) · sources/hermes.py (chat digests)
adapters/  claude-code/ · hermes/
bench/     the benchmarks: LongMemEval, Mem0 / Graphiti, a synthetic world, the gate
data/      the results and caches behind every number above (see data/README.md)
tests/     python3 -m pytest tests   (needs pytest; no test calls a model)
```

A memory folder holds `MEMORY.md` (an index, generated), `notes/<thing>.md`
(profiles), `notes/<thing>-history.md` (older values), `notes/log-<date>.md`
(daily notes that aren't facts) and `sessions/` (chat digests).

## Reproduce

### Check the numbers without a model

Everything behind the tables is in [data/](data/README.md): every answer with
its grade and its sure / unsure label, Mem0's and Graphiti's answers and
memory-building costs, and the memory-building output for the 332 held-out
questions.

```bash
python3 -m bench.doubt score data/results/heldout data/results/vs-others   # right, confidently wrong, ...
python3 -m bench.doubt score data/results/after-fixes data/results/count-rule
python3 -m bench.doubt score data/results/two-models/*/lme data/results/two-models/*/locomo
```

[data/README.md](data/README.md) shows accuracy from the `correct` field and how
to rebuild Deltos's memory for every held-out question from the shipped
memory-building output, with no model (all 332 rebuild file for file identical
to the stores built for the run).

### Run it

Any OpenAI-compatible server works for the answering model, grader and memory
building: `DELTOS_LLM_URL`, `DELTOS_LLM_MODEL`, `DELTOS_LLM_KEY`.

```bash
mkdir -p out/lme && curl -L -o out/lme/longmemeval_s_cleaned.json \
  https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json
H="--types multi-session,temporal-reasoning,single-session-assistant --skip 30"   # held out
S="--types single-session-user,single-session-preference"                         # whole types
python3 -m bench.lme extract $H                      # build memory: one call per chat, cached
python3 -m bench.lme extract $S
python3 -m bench.lme run $H --systems deltos-tools,rag --items off --out out/lme/results/mine
python3 -m bench.lme run $S --systems deltos-tools,rag --items off --out out/lme/results/mine
python3 -m bench.doubt score out/lme/results/mine    # confidently wrong, no model calls
```

After fixes: `python3 -m bench.lme items $H` asks the items check's questions
(cached in `out/lme/items.json`), and `run` without `--items off` applies them.
`DELTOS_READING_COUNT=1` adds the count rule. Systems: `deltos-tools` (the
block plus up to three rounds of lookups, the Deltos rows above), `deltos-rule` (the block
and its reading rule, no lookups), `rag` (plain search), `full` (the whole
history in the prompt). Every run labels its answers as it grades them; older
results: `python3 -m bench.doubt label <results>`.

Mem0 and Graphiti run from their own virtualenv against your own Neo4j and an
OpenAI-compatible embedder: [bench/others.py](bench/others.py) has the commands
and every setting.

`python3 -m bench.gate` is our development gate: it replays frozen
memory-building output through today's code and fails on any question that got
worse, with no model calls (~45 s). Its frozen inputs aren't shipped in v1.

## The first benchmark: a synthetic world

Before LongMemEval, the project started as a benchmark for one question: can
memory tell the current fact from the one it replaced? It's still in `bench/`.

1. **A synthetic world** (`bench/world.py`): a fictional team, Kestrel Works,
   whose facts change over nine months. Every fact has two dates (when it became
   true, when memory found out) and a volatility tag. Some recorded values were
   never true and get corrected later.
2. **Messy notes**, written the way agents write them: some updates edit the
   profile in place, some add a dated note and leave the old line standing,
   some add a "superseded" marker; later changes are often one-liners that use a
   nickname.
3. **Questions with answers worked out from the timeline**, not from the notes:
   current value, what it was before, when it changed, what it was on a date,
   and questions memory can't answer (the right move is to abstain).
4. **Adapters** (`bench/adapters/`): `fullcontext` (every note in the prompt),
   `grep` (an index plus grep and read tools, the plain-files style
   [Agent Memory Repo](https://github.com/AgentMemoryRepo/agentmemoryrepo)
   describes), `rg1` (one ripgrep pass by thing, one model call) and `gbrain`
   (an isolated [gbrain](https://github.com/garrytan/gbrain) instance, hybrid
   search).

Scoring is deterministic (world values are distinctive strings): right answer
with nothing else from that fact's history mixed in, stale answers, abstaining
when memory doesn't know, and confident wrong answers.

```bash
python3 bench/world.py out/world-7 7                                   # generate (also done automatically)
python3 -m bench.run --adapters fullcontext,grep,gbrain --limit 12    # smoke test
python3 -m bench.run --workers 3                                       # full run
```

The answering model comes from `DELTOS_LLM_URL` / `MTB_BASE_URL`, `MTB_MODEL`,
`MTB_API_KEY`. The gbrain adapter needs `gbrain` on PATH and an embedder
(`MTB_GBRAIN_EMBED*`).

What it found (2026-10-06, one local model, seed 7): how memory is **written**
mattered more than how it's searched. Same timeline, same 166 paraphrased
questions, the `rg1` reader:

| Writing style | Right, of 148 non-trick questions | Answers erased from memory | Stale answers |
|---|---|---|---|
| messy (edit in place / append / banner) | 124 (83.8%) | 21 | 2.0% |
| **trail** (current line + `was:` trail + cold history note) | **146 (98.6%)** | **0** | **0%** |

The trail style is the format Deltos stores. It also gave the smallest notes of
the three styles tried (31 KB vs 34 KB messy, 39 KB append-only).

## License

MIT, see [LICENSE](LICENSE). Third-party material and credits:
[THIRD-PARTY.md](THIRD-PARTY.md).
