# Deltos in Hermes

A memory provider plugin for the Hermes agent: the per-turn memory block,
Deltos's tools, and its instructions in the system prompt. It only maps
`deltos.Memory` onto Hermes' memory-provider hooks. It is fail-soft: a memory
error gives an empty block, never a failed turn.

The default install is one agent and nothing else: no notes, no git, no
nightly job. Everything past step 1 is optional.

## 1. The plugin

    mkdir -p ~/.hermes/plugins
    ln -s "$PWD/adapters/hermes/deltos" ~/.hermes/plugins/deltos
    hermes config set memory.provider deltos
    hermes memory status                 # "deltos (no setup needed) ← active"

Run these from the repo root (tried on a fresh install of stock Hermes v0.22.2). The plugin imports the `deltos` library from
the repo it is linked from, so an update lands on the next gateway restart.

Environment for the gateway (all optional):

| Variable | What it is |
|---|---|
| `DELTOS_ROOT` | the memory folder (default `~/deltos`); settings in `deltos.toml` there |
| `DELTOS_AGENT` | the name recorded as the source of the facts the agent writes (default: `agent` in `deltos.toml`, else `agent`) |
| `DELTOS_SRC` | the deltos checkout to import (default: the repo the plugin folder sits in) |

The agent's tools (`remember`, `name_thing`, `page_write`, ...) write to memory
directly, with no model and no run: a fact the agent states is in memory the
moment it says it.

## 2. Learning from chats

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
