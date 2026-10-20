# Data behind the README's numbers

Everything here was produced by the benchmark in `bench/` and is shipped so you
can check every number in the top-level README **without running a model**, and
rebuild Deltos's memory for the 332 held-out questions **without a model**.

LongMemEval's chats, questions and gold answers are not copied here: download
them from Hugging Face (see "Rebuild the memory" below). Rows carry the question
id (`qid`), the gold answer and the system's answer, so scoring needs nothing
else.

## Files

| Path | Rows | What it is |
|---|---|---|
| `results/heldout/deltos.jsonl` | 332 | Deltos's answers to the 332 held-out questions (one run) |
| `results/heldout/plain-search.jsonl` | 332 | Plain search (BM25, top 10 chat rounds) on the same questions |
| `results/vs-others/mem0.jsonl` | 100 | Mem0 2.2 on 100 held-out questions (the first 20 of each type) |
| `results/vs-others/graphiti.jsonl` | 50 | Graphiti 0.30 on 50 of them (the first 10 of each type) |
| `results/vs-others/deltos.jsonl` | 100 | The held-out Deltos rows for Mem0's 100 question ids (Graphiti's 50 are a subset) |
| `results/vs-others/plain-search.jsonl` | 100 | The held-out plain-search rows for the same 100 ids |
| `results/after-fixes/<set>/fix-off-N.jsonl` | 30–103 | Deltos with the items check off (see below) |
| `results/after-fixes/<set>/fix-on-a.jsonl`, `fix-on-b.jsonl` | 30–103 | Deltos with the items check on, two runs |
| `results/count-rule/<set>/rule-a.jsonl`, `rule-b.jsonl` | 30–103 | Fix on, plus the count reading rule (`DELTOS_READING_COUNT=1`), two runs. Compare with `after-fixes/<set>/fix-on-a/b` |
| `results/two-models/<model>/lme/deltos.jsonl`, `plain-search.jsonl` | 332 | The README's LongMemEval boards (10-18): Deltos and plain search, answered and graded by `<model>` (`deepseek-v4.1-flash`, `glm-5.3-flash`) |
| `results/two-models/<model>/lme/mem0.jsonl` | 100 | Mem0 2.2 on its 100 questions, same model |
| `results/two-models/<model>/locomo/{deltos,mem0,plain-search}.jsonl` | 500 | The LoCoMo fresh 500: id, category, grade, sure / unsure label, sizes and timings, with no question, answer or gold text (LoCoMo is CC BY-NC 4.0; `bench/locomo.py fresh` rebuilds the questions) |
| `cache/extract-heldout.jsonl` | 15,867 | Deltos's memory-building output for every chat of the 332 held-out questions: the raw model reply per chat |
| `cache/items.json` | 11,150 | The items check's decisions ("another" / "same"), used by the after-fixes and count-rule runs |
| `costs/mem0-ingest.jsonl` | 4,792 | Mem0's memory-building cost per chat, for the 100 scored questions |
| `costs/graphiti-ingest.jsonl` | 2,406 | Graphiti's, for its 50 |

`<set>` is one of:

| Set | Questions | Studied while building Deltos? |
|---|---|---|
| `multi-session-heldout` | 103 (multi-session after the first 30) | no, held out until 10-13 |
| `temporal-reasoning-heldout` | 103 (temporal reasoning after the first 30) | no, held out until 10-13 |
| `multi-session-first30` | 30 | yes |
| `temporal-reasoning-first30` | 30 | yes |
| `knowledge-update` | 78 (all of them) | yes |

The five sets are the 344 questions of the README's "After fixes" section.

### The fix-off runs

| Set | `fix-off-1` | `fix-off-2` | `fix-off-3` |
|---|---|---|---|
| `multi-session-heldout`, `temporal-reasoning-heldout` | the 10-13 held-out run (same rows as `results/heldout/deltos.jsonl`) | 10-14 run | 10-14 run |
| `multi-session-first30`, `temporal-reasoning-first30` | 10-14 run | 10-14 run | |
| `knowledge-update` | 10-14 run | | |

One of the 10-14 runs per set (`fix-off-2` for the held-out sets, `fix-off-1` for
the two first-30 sets) was meant to have the check **on**. A bug made each step
that collects the check's decisions start from an empty file and overwrite it,
so the file those runs used held only the knowledge-update decisions, and for
these questions the check never ran. Their memory was checked file by file
against the fix-off memory: identical for 205 of the 206 held-out questions (the
other differs by one entry) and for the first-30 sets, so they count as fix-off
runs. Their spread (83 / 77 / 84 on multi-session) is the answering model
varying from run to run. The fix-on runs (`fix-on-a`, `fix-on-b`) came after
the bug was fixed.

## Graphiti audit

`graphiti-check.md`: where each of Graphiti's 31 misses was lost (extraction, search or answering), checked against its graph with no model calls, plus a review of our Graphiti settings.

## Row fields (results)

| Field | Meaning |
|---|---|
| `qid` | LongMemEval `question_id` (ids ending in `_abs` are unanswerable: the right answer says so) |
| `type` | LongMemEval `question_type` |
| `abstention` | `true` for `_abs` questions |
| `answer` | the answering model's reply, verbatim |
| `gold` | LongMemEval's gold answer |
| `correct` | the grader's verdict (LongMemEval's grading prompts, same model as the answerer) |
| `doubt` | how the answer is put: `sure`, `unsure` or `no answer`. Missing on single-session-preference rows (left out of the doubt scores: their answers are suggestions, not facts) |
| `context_chars` | characters of memory the answering model was shown, including what its own lookups returned |
| `tool_calls` | (Deltos only) the lookups the answering model made, at most 3 (`page_read`, `page_search`, `memory_recall`, `memory_show`) |
| `context_seconds` | time to build the memory block for the question (for Mem0 and Graphiti: their search) |
| `prompt_tokens` | prompt tokens the answering model used, all rounds |
| `seconds` | wall time for the question: memory, answer, grading |

"Memory shown per question" in tokens is `context_chars / 4`.

## Rescore without a model

From the repo root:

```bash
python3 -m bench.doubt score data/results/heldout      # right, confidently wrong, says so when wrong, hedged when right
python3 -m bench.doubt score data/results/vs-others data/results/after-fixes data/results/count-rule
```

`bench.doubt score` reads each row's `doubt` label (every shipped row that is
scored has one) and leaves single-session-preference out, so its "right" is out
of the 302 questions with a factual answer. Accuracy over all questions comes
from `correct`:

```bash
python3 - <<'EOF'
import json, collections
rows = [json.loads(l) for l in open("data/results/heldout/deltos.jsonl")]
print(sum(r["correct"] for r in rows), "of", len(rows))
by = collections.Counter(r["type"] for r in rows)
ok = collections.Counter(r["type"] for r in rows if r["correct"])
for t in by: print(f"  {t}: {ok[t]}/{by[t]}")
EOF
```

For the Graphiti table, score `vs-others/deltos.jsonl` and `plain-search.jsonl`
on the question ids in `vs-others/graphiti.jsonl`.

The "building memory, per chat" column: for Deltos, the mean of
`prompt_tokens + completion_tokens` in `cache/extract-heldout.jsonl` (calls: one
per chat, plus a repair call where `repair_raw` isn't empty); for Mem0 and
Graphiti, the means of `calls`, `tokens` and `seconds` in `costs/*-ingest.jsonl`
(one row per chat: `qid`, `sid`, `day`, `calls`, `tokens`, `seconds`). Seconds
are wall time with at most 3 requests at once to one local model, so they
depend on that setup.

## Rebuild the memory (no model)

`cache/extract-heldout.jsonl` holds, for each held-out question and each of its
chats, what the memory-building model replied (`raw`) and its reply to the one
repair call it gets when the store refuses lines (`repair_raw`, empty for 89% of
chats). Replaying these through Deltos's import rebuilds each question's memory
with no model. On 10-16 all 332 rebuilt memories were identical, file for file,
to the stores built for the held-out run.

Row fields: `qid`, `sid` (LongMemEval session id), `day` (the chat's date),
`raw`, `repair_raw`, `prompt_tokens`, `completion_tokens` (the memory-building
cost: ~1,200 tokens per chat on average). Eight (`qid`, `sid`) pairs appear
twice (an interrupted run resumed); the loader keeps the last one, as the
benchmark did.

Until the benchmark reads `data/` directly, put the files where it looks:

```bash
mkdir -p out/lme
curl -L -o out/lme/longmemeval_s_cleaned.json \
  https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json
cp data/cache/extract-heldout.jsonl out/lme/extract3-user.jsonl
python3 - <<'EOF'
import bench.lme as L
cache = L.extracted()
q = L.questions(["multi-session"], 0, 30)[0]     # held out: skip the first 30
m = L.memory_for(q, cache)                       # builds out/lme/stores/<code>/<qid>, no model
print(q["question"]); print(m.context(q["question"]))
EOF
```

`cache/items.json` is the items check's decisions, keyed by a hash of each
replacement (thing, label, earlier value and date, later value and date; see
`deltos/items.py`). Copied to
`out/lme/items.json`, `bench.lme run` applies it, which is how the after-fixes
runs were built.
