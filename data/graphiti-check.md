# Graphiti on the 50 held-out questions: where the answers were lost

Brain-free check, 2026-10-16. I made no model calls (LLM or embedder), read Neo4j only through `READ_ACCESS` sessions, and edited nothing tracked in the repo.

Inputs:
- `out/lme/results/phase5/*/graphiti.jsonl`
- `out/lme/others/graphiti/{ctx,ingest}.jsonl`
- `out/phase5.log`
- the Neo4j groups `lme_<qid>`
- the held-out Deltos and plain-search rows

The audit scripts (not shipped; they read a local Neo4j with the 50 question graphs):
- `dump.py` pulls each group's facts, entities and episodes, the questions, the context and the answers into one JSON file.
- `dump2.py` pulls the entities each episode mentions.
- `look.py QID PATTERN…` shows where terms appear in the evidence chats, the graph and the context.
- `position.py` / `position_useronly.py` measure which parts of each chat the extraction covered.

**Result.** Graphiti got 19 of 50 right. Its 31 misses split as follows:

| | Misses |
|---|---|
| Not extracted | 25 |
| Extracted, not retrieved | 1 |
| Retrieved, still wrong | 4 |
| Gold-label problem | 1 |

All 19 right answers were extracted and retrieved. This set has no abstention (`_abs`) questions.

**How I classified the misses.** For each miss I took these steps:
1. I found the answer in the evidence session text. The stored `Episodic.content` has the same length as `L.render(turns)` for all 2,406 episodes, so the harness fed Graphiti the whole chat.
2. I searched the group's `RELATES_TO.fact` texts and `Entity.name`/`summary` for the key terms.
3. I checked the retrieved context.
4. I read every miss by hand.

For multi-session and temporal questions, "extracted" means that every needed fact is in the graph. Partial cases are marked.

---

## 1. Where each miss was lost

| Bucket | n | Question ids (* = some needed pieces were extracted, the rest missing; † = Deltos and plain search missed it too) |
|---|---|---|
| **Not extracted**: the answer is in the stored chat but in no fact or entity summary | **25** | SSU: e47becba, 118b2229\*, 1e043500, 51a45a95†, 6ade9755 · SSA: 352ab8bd, 3e321797\*, 7a8d0b71 · SSP: 195a1a1b, 32260d93†, 54026fce\*, 75832dbd\*†, afdc33df, caf03d32 · MS: 129d1232\*, 60bf93ed, gpt4_194be4b3\*, gpt4_31ff4165\* · TR: 6e984301, bcbe585f\*, gpt4_4ef30696, gpt4_74aed68e\*, gpt4_8e165409\*, gpt4_d6585ce8\*, gpt4_f420262c\*† |
| **Extracted, not retrieved** | **1** | 2788b940 (Sunday yoga existed only in the `user` node's summary, and search didn't return that node) |
| **Retrieved, still wrong** | **4** | e982271f (list order lost), a40e080f (why the companies were named was lost), 9d25d4e0 (Graphiti wrongly marked a fact as no longer true), 0edc2aef (the reader didn't apply Seattle-hotel likes to Miami) |
| **Gold-label problem** | **1** | 370a8ff4† (gold 15 weeks; the evidence chats are dated 2023-01-19 and 2023-04-10, about 11.6 weeks apart; it was also not extracted) |

**Right answers: 19 of 19 were extracted and retrieved.** For each one, the key fact is both in the graph and in the context, checked by key term. In one case, 6f9b354f, the fact arrived only incidentally: "…ZZ Plant … to complement the new lighter shade of gray walls".

By question type:

| Type | Right | Not extracted | Not retrieved | Retrieved, wrong | Gold problem |
|---|---|---|---|---|---|
| single-session-user | 5/10 | 5 | 0 | 0 | 0 |
| single-session-assistant | 5/10 | 3 | 0 | 2 | 0 |
| single-session-preference | 3/10 | 6 | 0 | 1 | 0 |
| multi-session | 4/10 | 4 | 1 | 1 | 0 |
| temporal-reasoning | 2/10 | 7 | 0 | 0 | 1 |

On the same 31 questions, Deltos was right on 26 and plain search on 17. Five of the 31 were missed by all three systems (the † marks).

### What "not extracted" looks like

The 25 extraction losses fall into three kinds:
- **The whole statement is missing (13).** No fact or summary mentions it, for example "Business Administration", "Summer Vibes", "Serenity Yoga", "$2,000", "9:30 pm", "beef stew" or "started sculpting classes".
- **The entity was kept but the detail was dropped (2).** For example, "user listens to audiobooks during their daily commute" without "45 minutes each way", and "user is excited to try tomato as a natural remedy" without "10 minutes".
- **Some needed pieces were extracted, others weren't (10).** For example, 1 of 3 charity amounts, 2 of 5 concerts, or the spark-plug date taken from a later "a while back" mention.

**Extraction covers the end of long chats much better than the start.** For every chat with at least 3 user turns (2,145 episodes), I checked whether each user turn names any entity from a fact extracted from that episode. This is a string match, so it is approximate. The table shows the share of user turns that do, by where the turn sits in the chat:

| Chat length | First fifth | 2nd | 3rd | 4th | Last fifth |
|---|---|---|---|---|---|
| < 8K chars (3,159 turns) | 46% | 37% | 38% | 43% | 41% |
| 8–16K chars (6,705 turns) | 37% | 36% | 38% | 48% | 59% |
| ≥ 16K chars (2,194 turns) | 30% | 27% | 40% | 48% | 61% |

Short chats are covered evenly. In long chats, coverage of the first user turns is about half that of the last. Counting each user turn together with the assistant reply after it gives the same shape (≥16K chars: 49% → 84%). Two examples:
- In e47becba (17.9K chars, 6 user turns), all 18 facts come from the last two user turns. Todoist, Trello, the new 9-to-5 job and "Business Administration" (user turn 3) never appear in the graph.
- In 6ade9755, the episode's 4 facts are all about the last turn (Sarah, the Sunday routine). "Serenity Yoga" and "Down Dog" are absent.

At the question level the signal is noisier. Some right answers sat early in their chat, though their key terms often recur later in it.

**The number of facts barely grows with chat length:**

| Chat length | Episodes | Median facts per episode | Facts per 1K chars | Episodes with 0 facts |
|---|---|---|---|---|
| < 4K | 345 | 4 | 2.96 | 27 |
| 4–8K | 533 | 6 | 1.63 | 22 |
| 8–12K | 553 | 7 | 0.96 | 18 |
| 12–16K | 608 | 7 | 0.68 | 17 |
| 16–24K | 362 | 8 | 0.56 | 8 |

Overall the graphs hold 21,926 facts, 25,657 entities and 2,406 episodes, about 9.1 facts per episode.

**Empty extractions.** In 70 sessions (2.9%), Graphiti made only 2 model calls. In 66 of them the episode mentions at most 2 entities, usually just the speakers `user`/`assistant`. For example, 06878be2/368276ab_3 is 16K chars long and yielded only `user`. 92 episodes (3.8%) have no facts at all. One of them is an evidence chat: gpt4_194be4b3/answer_3826dc55_5, 13.4K chars.

**Why this is possible in Graphiti's design.** A fact is an edge between two extracted entities ("involve two DISTINCT ENTITIES", `prompts/extract_edges.py:134`). The entity prompt says "Do NOT extract dates, times…" and "When in doubt, do NOT extract" (`prompts/extract_nodes.py:156-157`). So a quantity, date or name of a thing survives only if the model writes it into some edge's fact text.

**The extraction prompts are mostly older chats.** `add_episode` passes the full text of up to 10 earlier episodes (`RELEVANT_SCHEMA_LIMIT = 10`, `search/search_utils.py:64`; used at `graphiti.py:1144-1148`) into both the entity and the edge prompts (`utils/maintenance/node_operations.py:118`, `edge_operations.py:189`). With whole chats as episodes:
- an extraction call is about 26K tokens (median over the 2-call sessions: 25.7K tokens per call);
- the chat being extracted averages 10.3K chars, roughly 2.6K tokens, or about a tenth of the prompt;
- ingest cost a median of 13 calls and 95K tokens per session, 231M tokens in all.

### The 4 retrieved-but-wrong misses

All four had the needed facts in the context, but the answer still went wrong:
- **e982271f.** All 10 Portland venues were extracted as separate, identically dated facts, and both Doug Fir and Revolution Hall were retrieved. Facts carry no list order, so "the last venue" can't be answered. The reader said Doug Fir.
- **a40e080f.** "Patagonia … most ethical companies" and "Southwest … best places to work" were retrieved alongside the Triumvirate fact. No fact says the assistant named those two as companies that look after employee safety the way Triumvirate does, so the reader abstained.
- **9d25d4e0.** All three pieces of jewellery were retrieved. Graphiti's own invalidation step had marked "user owns a pair of emerald earrings" as ended on 2023-05-28, likely confused with the different "earrings with little diamonds" from that day, and the purchase fact had no date. The reader counted 2 instead of 3.
- **0edc2aef.** The Seattle hotel facts (hot tub on the balcony, skyline views, rooftop pools) were retrieved. The user's own statement ("I like hotels with unique features…") survived only as hotel attributes, and the reader wouldn't carry it over to Miami.

---

## 2. Five examples

**e47becba (single-session-user), not extracted (the start of a long chat was lost)**
- **Question:** "What degree did I graduate with?"
- **Gold:** Business Administration.
- **Source:** user turn 3 of 6 in a 17.9K-char chat: "I graduated with a degree in Business Administration…".
- **Graph:** 18 facts from that chat, all from the last two user turns (Mint vs Personal Capital; meal-prep brands such as Pyrex and OXO). "Business Administration", "Todoist", "Trello" and "new job" appear in 0 facts and 0 entities. A Neo4j check finds the phrase in 1 Episodic node and 0 facts.
- **Search returned:** nothing about a degree.
- **Graphiti's answer:** "I don't know what degree you graduated with."

**118b2229 (single-session-user), not extracted (the detail was dropped)**
- **Question:** "How long is my daily commute to work?"
- **Gold:** 45 minutes each way.
- **Source:** "I've been listening to audiobooks during my daily commute, which takes 45 minutes each way."
- **Graph:** `user -LISTENS_TO-> audiobooks`: "user listens to audiobooks during their daily commute". The duration is in no fact or summary.
- **Search returned:** that fact, ranked high.
- **Graphiti's answer:** "I don't know how long your daily commute is."

**2788b940 (multi-session), extracted but not retrieved**
- **Question:** "How many fitness classes do I attend in a typical week?"
- **Gold:** 5.
- **Graph:** facts and entities for BodyPump (Mondays), Zumba (Tuesdays and Thursdays) and Hip Hop Abs (Saturdays), plus "The user attends yoga classes" with no schedule. The only place "yoga on Sundays at 6:00 PM" appears is the summary of the hub node `user`, a running summary held under about 1,000 chars (`MAX_SUMMARY_CHARS = 1000`, `utils/text_utils.py:26`).
- **Search returned:** the three schedules and the bare yoga fact. The `user` node came back for only 9 of the 50 questions, and not for this one.
- **Graphiti's answer:** "At least 4 scheduled classes… You also attend yoga, but I don't know how often."

**9d25d4e0 (multi-session), retrieved but still wrong (wrong invalidation)**
- **Question:** "How many pieces of jewelry did I acquire in the last two months?"
- **Gold:** 3.
- **Search returned:**
  - "The user acquired the engagement ring one month prior… (2023-04-28 - present)"
  - "The user acquired the silver necklace … on the 15th of last month (2023-04-15 - present)"
  - "The user owns a pair of emerald earrings. (Date range: 2023-05-21 - 2023-05-28)"
  - "The user purchased the emerald earrings at a flea market. (Date range: present - present)"

  The earrings facts were given an end date by Graphiti's contradiction step. Nothing in the source says the user stopped owning them.
- **Graphiti's answer:** "2 … engagement ring and the silver necklace".

**gpt4_74aed68e (temporal), not extracted (the dated event was lost and the facts are undated)**
- **Question:** days between replacing the spark plugs and the Turbocharged Tuesdays event.
- **Gold:** 29 days.
- **Source:**
  - 2023-02-14, user turn 1 of a 17.6K-char chat: "I replaced my spark plugs with new ones from NGK today…".
  - 2023-03-15: "…during the Turbocharged Tuesdays event today".
- **Graph:**
  - The 02-14 chat's 4 facts are all brake-fluid brand recommendations from a later turn; the spark-plug statement is missing.
  - The spark-plug fact that does exist was extracted from the 03-15 chat, where the user says it happened "a while back".
  - Both remaining facts have no `valid_at`.
- **Search returned:** both facts, shown as "(Date range: present - present)".
- **Graphiti's answer:** "I don't know—the memory doesn't include the exact dates…".

---

## 3. Graphiti's own errors and warnings

From `out/phase5.log`. Lines 128–686 are the phase-5 runs: the first lane, the second machine's lane and both split lanes all appended stderr to this log. Lines 21–126 are the calibration run on 0a995998, which is not one of the 50.

| What was logged | Phase 5 (50 questions, 2,406 sessions) | Calibration (19 sessions) |
|---|---|---|
| "Target entity not found in nodes for edge relation" | 288 | 12 |
| "Source entity not found in nodes for edge relation" | 38 | 0 |
| "Error in generating LLM response" (JSON parse: 4× "Expecting ',' delimiter", 4× "Expecting value") | 8 | 0 |
| "Retrying … after N attempts" | 0 | 0 |
| "Error parsing valid_at date, skipping" | 6 | 0 |
| "LLM returned invalid duplicate_facts idx" | 3 | 0 |
| "LLM returned summary for unknown entity" | 2 | 0 |
| Ingest rows with `error` (`ingest.jsonl`) | 0 of 2,406 | 0 |

- **Entity-not-found.** Each warning is one extracted edge thrown away because its endpoint name wasn't in the entity list (`edge_operations.py:216-230`, `continue`). That is 326 dropped edges against 21,926 kept, about **1.5% of edges** or 0.14 per session.
- **JSON errors.** All 8 broke on a single line between characters 16,496 and 16,577. Edge extraction asks for up to 16,384 output tokens (`edge_operations.py:141`), and the generic client's default is also 16,384 (`openai_generic_client.py:65`).
  - Hitting that limit at about 16.5K characters means about 1 character per token, which points to degenerate output (a repetition or whitespace loop), not a long, legitimate list of facts. I can't confirm this without the raw output.
  - Graphiti retries a JSONDecodeError up to 4 times (`llm_client/client.py:62-71,121`) and logs a warning from the second failed attempt on. None was logged and no session errored, so all 8 recovered on the second try.
- **valid_at.** The 6 date-parse failures leave 6 facts without `valid_at`.

**Conclusion:** Graphiti's own errors cost about 1.5% of edges and no sessions. They do not explain the 25 extraction losses.

There was **no sign of truncation**:
- No chat-level cap: the stored episodes have the full chat text.
- No output-token cap in play: the median is 7 facts per episode, far below what 16,384 tokens allows, and only the 8 recovered calls hit the limit.

The real problem is the saturation and position pattern in §1: even the longest chats get a median of about 8 facts.

---

## 4. Were our harness choices fair to Graphiti?

Library code cited relative to `out/venv-others/lib/python3.12/site-packages/graphiti_core/` (graphiti-core 0.30.2).

| Setting | What we did (`bench/others.py`) | Graphiti default / docs | Plausibly cost accuracy? | Evidence |
|---|---|---|---|---|
| **Search recipe** | `search_()` with `EDGE_HYBRID_SEARCH_RRF` edges + `NODE_HYBRID_SEARCH_RRF` nodes, limit 20, no cross-encoder (l.309) | `Graphiti.search()` uses `EDGE_HYBRID_SEARCH_RRF` with 10 results (`graphiti.py:1628-1631`; `DEFAULT_SEARCH_LIMIT = 10`, `search/search_config.py:29`). But `search_()` defaults to `COMBINED_HYBRID_SEARCH_CROSS_ENCODER` (`graphiti.py:1665`; recipe at `search/search_config_recipes.py:81`), which adds breadth-first graph search and LLM reranking and also searches episodes. The `search()` docstring recommends `search_()` "for more robust results" (`graphiti.py:1601`). | **A deviation from the advanced default, but small at most.** Only 1 of 31 misses (2788b940) had the answer in the graph but not in the context. 25 had nothing in the graph to find, and 4 were already retrieved. Our limit is twice the default. | Not run: the cross-encoder reranker (`OpenAIRerankerClient`) would make model calls. |
| **Raw episodes left out of the context** | Facts and entity summaries only, laid out like Zep's LongMemEval template (THIRD-PARTY.md) | The COMBINED recipes also run BM25 over raw episodes (`episode_config`, `search_config_recipes.py:43/67/98`) | **The biggest "what if", but not Graphiti's graph.** The raw chats hold all 25 lost answers. Putting episodes in the context would answer from raw-chat retrieval, close to the plain-search row (whole chats average 10.3K chars, so 20 episodes would be about 200K chars). | Our choice follows the context layout Zep published for LongMemEval. |
| **Dates in the context** | Each fact shown with `valid_at`–`invalid_at` (l.316), as in Zep's template | Every edge also stores `reference_time`, the date of its chat (22,043 of 22,043 edges), which we don't show | **Plausibly hurts temporal questions; changes none in this set.** 25% of stored facts (5,544 of 21,926) have no `valid_at`. 183 of the 1,000 fact lines shown to the reader had no start date (48 of 200 on temporal questions). Every temporal miss here also lacks another needed fact, so showing the chat date wouldn't flip any of them alone. | gpt4_74aed68e, gpt4_8e165409, gpt4_f420262c |
| **max_tokens** | Not set, so the library defaults apply | 16,384 in `LLMConfig` (`llm_client/config.py:19`), in `OpenAIGenericClient` (`openai_generic_client.py:65,94`) and hard-coded for edges (`edge_operations.py:141`) | **No.** 8 of about 44K calls hit the limit and all recovered. The median of 7 facts per episode is far below the limit. | §3 |
| **Thinking off** | `chat_template_kwargs.enable_thinking=False` (l.94) | Graphiti's own OpenAI client resolves the default model gpt-5.5 to reasoning effort `'none'` ("reasoning off … comparable extraction quality at far lower cost", `llm_client/openai_base_client.py:119-131`), other models to `'minimal'`. The generic client's default model is non-reasoning gpt-4.1-mini (`openai_generic_client.py:35`). | **Matches Graphiti's own default.** Whether thinking would rescue the long-chat losses can't be known without running it. | — |
| **Temperature 0** | `LLMConfig(temperature=0)` (l.245), noted in the harness header | `DEFAULT_TEMPERATURE = 1` (`llm_client/config.py:20`) | **Unlikely to matter much; I can't rule it out.** Temperature 0 is the usual choice for extraction. The 8 runaway outputs look like greedy-decoding loops, but they recovered. | — |
| **MAX_REQUESTS = 3** | A semaphore around the raw `chat.completions.create` (l.56, `wrap_async` l.126). It only queues requests and never changes one. | Graphiti's internal concurrency is `SEMAPHORE_LIMIT` = 20 (`helpers.py:38`). `add_episode` wants each group's episodes added one at a time, which we do (`graphiti.py:1120`). | **No.** It sets throughput only. No timeouts and 0 ingest errors. | `ingest.jsonl` |
| **Embedder** | Qwen3-Embedding-0.6B, 1024 dims, raw text (no Qwen3 query instruction) | Default `text-embedding-3-small` (`embedder/openai.py:24`), `EMBEDDING_DIM = 1024` (`embedder/client.py:23`). The cosine cutoff `DEFAULT_MIN_SCORE = 0.6` (`search/search_utils.py:65`) is fixed whatever the model. | **Possibly, but only for retrieval (1 of 31 misses).** BM25 is the other half of the hybrid search. Embeddings also feed entity deduplication. I can't measure this brain-free because it needs embedding the questions. | — |
| **Small model = main model** | `small_model=MODEL` | `OpenAIGenericClient` always sends `self.model` (`openai_generic_client.py:156`) | **No effect.** | — |
| **Whole chat as one episode** (`source=message`, `L.render`, l.284) | One `add_episode` per session, the same unit Mem0 got | Graphiti accepts any size. Each extraction prompt carries up to 10 earlier episodes in full (`graphiti.py:1144-1148`). | **The most plausible harness-side lever, untested.** About 26K-token extraction prompts for about 2.6K tokens of new chat, near-flat output (§1), and coverage falling toward the start of long chats. Per-turn or per-exchange episodes would give the extractor much shorter targets, at several times the 18 calls and 96K tokens per session we already spent. I don't know what episode size Zep used for its own LongMemEval run. | §1 tables |

Some causes are Graphiti's design rather than our setup:
- facts must connect two entities;
- the hub `user` node's summary is held under about 1,000 chars, and summaries in this set ranged from 846 to 1,905 chars;
- the contradiction step can wrongly end facts (9d25d4e0).

These cost answers here, but they are how Graphiti works.

---

## 5. Suggested fair wording for the README

> Graphiti (graphiti-core 0.30.2) answered 19 of the 50 held-out questions it ran on, against 44 for Deltos and 32 for plain BM25 search on the same questions. Every system built memory with the same local model (Qwen3.8-Flash-Next, thinking off) and was graded by the same reader and judge. A brain-free audit of its 31 misses found that 25 were lost at extraction (the answer is in the stored chat but never became a graph fact, most often a detail from early in a long chat); only 1 was a search miss, 4 were wrong answers from the right facts, and 1 has a gold label that doesn't match the chat dates. Graphiti's own ingest errors were negligible (no failed sessions, about 1.5% of extracted edges dropped), so the result reflects Graphiti's extraction with this model and whole-chat episodes, and a stronger extraction model, smaller episodes or its cross-encoder search might score differently; we did not test those.
