# Deltos in Hermes

A memory provider plugin for the Hermes agent: the per-turn memory block,
Deltos's tools and its instructions in the system prompt, plus `/deltos` in any
chat, a Memory page in Hermes Desktop and `hermes deltos` in a terminal. What it
is and what it does on your machine: [deltos/README.md](deltos/README.md), the
page a catalog install shows. This page is the longer manual.

It is fail-soft: a memory error gives an empty block, never a failed turn.

The default install is one agent and nothing else: no notes, no git, no
nightly job, no model. Everything past step 1 is optional.

## 1. The plugin

From the plugin catalog:

    hermes plugins install deltos --enable
    hermes plugins enable deltos         # lets the Memory page read the store
    hermes memory setup deltos           # one question: the folder to keep memory in
    hermes memory status                 # the store, what it holds, "deltos (local) ← active"

Until the catalog lists it:
`hermes plugins install CocaKova/deltos#adapters/hermes/deltos --enable`. An
installed plugin carries its own copy of the `deltos` library (`lib/`), so it needs
nothing beside it. Tried on stock Hermes v0.22.7.

From a clone, to work on it (run from the repo root):

    mkdir -p ~/.hermes/plugins
    ln -s "$PWD/adapters/hermes/deltos" ~/.hermes/plugins/deltos
    hermes config set memory.provider deltos

A linked plugin imports the `deltos` library from the repo it is linked from, so an
update lands on the next gateway restart.

The plugin gives a **new** store two settings in `<root>/deltos.toml`:
`[recall] doubt_days = 21` and `[checks] on = true` (the "not confirmed" note and
the agent's checks; see the main README). A store that already has notes or a
`deltos.toml` is left as it is.

Where the memory folder comes from, first match:

| Source | What it is |
|---|---|
| `DELTOS_ROOT` | the memory folder, when set |
| `deltos.json` in the Hermes home | what `hermes memory setup` saved; each profile has its own |
| default | `~/deltos` |

Other environment for the gateway (all optional):

| Variable | What it is |
|---|---|
| `DELTOS_AGENT` | the name recorded as the source of the facts the agent writes (default: `agent` in `deltos.toml`, else `agent`) |
| `DELTOS_SRC` | a deltos checkout to import instead (default: `lib/` inside the plugin, else the repo the plugin folder sits in) |

The agent's tools (`remember`, `name_thing`, `page_write`, ...) write to memory
directly, with no model and no run: a fact the agent states is in memory the
moment it says it.

### Looking at memory

`/deltos` in any chat (the terminal, Desktop, a messaging platform) answers from
the files, with no model turn. `hermes deltos` prints the same in a terminal, and
`hermes deltos <command>` is the whole `deltos` command line on this profile's
store.

    /deltos                          how much memory holds, what it has not confirmed, what changed lately
    /deltos <thing>                  one thing: each value, since when, and what it was before
    /deltos on <YYYY-MM-DD> <thing>  what was true of it on that day
    /deltos changes [days]           what changed lately (7 days)
    /deltos due [n]                  what is worth a fresh look, most pressing first

In Hermes Desktop the plugin adds a Memory page with the same views: the things,
each fact's dates and past values, what is not confirmed, what changed, and a day
picker. It only reads; its "Ask the agent to check" button puts a sentence in the
chat box for you to send. The page talks to the plugin's own routes under
`/api/plugins/deltos/`, which are read-only too.

`hermes deltos demo <new folder>` writes a small made-up store to try all of this
on (run Hermes with `DELTOS_ROOT=<that folder>`).

## 2. Learning from chats (experimental, off)

Learning turns finished chats into facts with your model. It is off by default and
has not passed a quality test on new chats yet (see "Not in v1" in the main
README), so treat it as an experiment: every sweep is a staged run you can undo
(section 5).

With a model, an owner and Hermes' sessions in `deltos.toml`:

```toml
owner = "Sam"

[llm]                                # any OpenAI-compatible endpoint
url = "http://127.0.0.1:8000/v1"
model = "your-model"

[sessions]
hermes = "~/.hermes"

[learn]
auto = true                          # off by default
```

each chat is learned once it is finished (closed, or quiet for 30 minutes):
when a session ends or the gateway starts, the plugin starts `deltos learn
--wait` in the background. It waits until the agent has been idle for two
minutes, sends one model request at a time, and stops between chats when the
agent gets busy again. Every chat is read once; one picked up again later gives
only its new turns.

With `[learn] nightly = true` instead (and `auto` unset), chats are learned only by the nightly
job (section 4, `deltos learn --wait --nightly`), never at a session end or a gateway start: for a
model that may serve chats only at night.

Without `[learn] auto = true`, chats are learned only by hand:

    deltos learn [--days N] [--max N] [--dry-run] [--no-idle]

(`deltos` is the installed command; from the repo, `python3 -m deltos`.)

## 3. Notes (optional)

`deltos import` turns new note lines into facts with the same model. Two
sources, either or both, in one run:

```toml
[import]
folders = ["~/notes:notes"]          # any folder of markdown, as path:label; no git needed
repos = ["~/wiki:HEAD:wiki"]         # git repos, as path:revision:label; each new commit's lines
skip = ["ARCHIVE", ".bak"]           # file names containing these are skipped
```

A folder needs nobody to commit it. Each `*.md` under it (dot folders left out)
is compared with the copy imported last, kept in
`<root>/.sources/<label>/`, and only its new lines are read. The first import
reads every file whole (`deltos import --limit N` caps the pieces per run).
A deleted file gives nothing. A git repo goes under `folders` or `repos`, not
both.

## 4. A nightly job (optional)

`adapters/hermes/nightly.sh`, from cron or a systemd timer, runs the note
import, `deltos learn --wait` (the chats that ended while no gateway was
listening; a no-op that says so while `[learn] auto` is off), the chat digests
(`deltos sessions`), a commit of the memory folder if it is a git repo, and one
status line, all logged to `<root>/nightly.log`.

Steps that belong to one machine, such as checkpoint commits of the note folders
the import reads, go in a site file, `~/.config/deltos/nightly-site.sh` (or
`$DELTOS_SITE`). It runs first, in its own shell without `set -u`, so it sets
every variable it uses. A failed site step is logged and the import still runs.

## 5. Runs and holds

Every import and every learn sweep is a staged run. It works on a copy of
memory, its numbers (loops, cut-offs, lines per piece, facts stored per piece)
are judged against the runs this store accepted, and then it is either
promoted onto memory or held. A held run writes `<root>/import.hold` (notes) or
`<root>/learn.hold` (chats) with its reasons, and that kind does not run again
until you decide:

    deltos status [--line]           the last run of each kind, holds, half-promoted runs
    deltos runs [-n N]               the latest decisions
    deltos run show <id>             a run's report
    deltos run promote <id>          promote a held run anyway
    deltos run discard <id>          drop it; its notes or chats are read again next time
    deltos run undo <id> [--no-hold] take a promoted run out of memory, with or without git;
                                     an entry someone changed since is kept

A folder's snapshot moves only with a promoted run, so a held or discarded
import finds the same changes next time.

## 6. What the store logs

- `<root>/reads/<YYYY-MM>.jsonl`: one line per memory block built (a session
  hash, sizes, timing, the things and pages shown; never the message text).
- `<root>/learn.log`: the output of every learn sweep the plugin started.
- `<root>/runs.jsonl` and `<root>/.runs/<id>/`: each run's decision, report and
  the images an undo needs.
- `<root>/nightly.log`: the nightly job, when you use it.
