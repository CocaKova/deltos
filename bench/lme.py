"""LongMemEval (Wu et al., "LongMemEval: Benchmarking Chat Assistants on Long-Term
Interactive Memory", ICLR 2025; MIT, github.com/xiaowu0162/LongMemEval) for deltos
and other memory systems: the same questions, reader model and judge for all.

Each question comes with its own chat history (~48 sessions). A memory system
reads that history, then gets the question with its date. Systems:

  full      the whole history in the prompt (the no-memory-system baseline)
  rag       plain search: the 10 best-matching user/assistant rounds (BM25)
  deltos   facts the extractor took from each session, replayed in date order,
            plus the raw sessions as pages; the context is Memory.context()
  deltos-rule  the same, with deltos's one-line reading rule (READING) above the block:
            an agent gets it in deltos's instructions, a bare reader doesn't

Cost control: deltos's extractor reads each unique session once with an empty
memory (cached in out/lme/extract.jsonl); each question's memory is then a replay
of its own sessions, no model. Data: out/lme/longmemeval_s_cleaned.json
(huggingface.co/datasets/xiaowu0162/longmemeval-cleaned).

  python3 -m bench.lme extract --types knowledge-update --n 30
  python3 -m bench.lme run --systems full,rag,deltos --types knowledge-update --n 30
  python3 -m bench.lme run ... --fake      # the whole pipeline with a stub model
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import threading
import time
import zlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from deltos import Memory
from deltos.memory import READING
from deltos.config import Config
from deltos.sources.chats import chat_prompt
from deltos.sources.notes import Migrator
from deltos import store
from deltos.store import Store
import bench.doubt as doubt
import deltos.items as items
import deltos.llm as llm
import deltos.slots as slots

# BENCH_OUT / BENCH_DATA (10-17): another benchmark in the same layout (bench/locomo.py).
OUT = Path(os.environ.get("BENCH_OUT", "out/lme"))
DATA = Path(os.environ.get("BENCH_DATA") or OUT / "longmemeval_s_cleaned.json")

# LongMemEval's user has no name; deltos's owner needs one (store names follow
# real-name rules: "user" is too generic, "user's car" is a phrase).
OWNER = "Sam"


def mid(q: dict) -> str:
    """Whose memory a question reads: its own (LongMemEval), or its conversation's (LoCoMo)."""
    return q.get("memory_id") or q["question_id"]


def owner_of(q: dict) -> str:
    """LongMemEval's user is Sam; LoCoMo's two people are both named things, and memory has no owner."""
    return "" if q.get("speakers") else OWNER

# deltos's chat prompt (deltos/sources/chats.py) with the benchmark's owner: the same text,
# byte for byte, that stood here until 10-19 (sha1 445190dd; tests/test_chats.py).
EXTRACT_PROMPT = chat_prompt(OWNER)

# LoCoMo (bench/locomo.py): two people talking, each remembered under their own name;
# the rules are EXTRACT_PROMPT's, said of both.
EXTRACT_PROMPT_DUO = """You are adding a conversation between {A} and {B} to a structured memory about them.
The conversation took place on {day}. List the facts it states about {A} and {B} and the concrete
things in their lives (people, pets, places, possessions, work, projects, events, accounts, habits,
preferences, numbers they track).

Reply with lines only, no JSON, no commentary:
= <thing> | <other names, separated by ;> | <kind>
<thing> | <attribute> | <value> | <since YYYY-MM-DD>
<thing> | <attribute> | <value> | <since YYYY-MM-DD> | correction

- Each line starts with who is speaking. A person's own facts go on that person's thing ("{A}",
  "{B}"). Their unnamed belongings, plans and routines are attributes of them ("{A} | car |
  2018 Honda Civic", "{B} | Europe trip destination | Rome"), never a thing called "{A}'s car".
  A person, pet or place with a name is its own thing ("Max", a dog), with "relationship" saying
  what it is to them (her dog, his sister).
- Attributes are short labels someone would ask about (job, location, personal best 5K time,
  favorite cuisine). One label holds one fact: two different facts get two labels, and a fact
  that changes keeps its label. Values are short: the value only.
- "since" is the date the value became true; if the conversation gives none, use {day}.
- If a value replaced an older one, give both with their dates. Use "| correction" only when
  a speaker says an earlier value was wrong.
- Only facts they would expect you to remember later.

What memory already holds about the things this conversation mentions (use an entry's label
exactly when it is the same fact, so the new value replaces the old one; a new value is an
update with its date, not a correction):
{known}"""

# user-ev: the same, plus what Sam did, as "+" lines into the daily log. Counts
# over time ("how many times did I bake in the past two weeks?", "how many
# weddings this year?") are made of events; the facts-only import kept state
# ("sourdough starter age") and lost the bakes (10-11 multi-session diagnosis).
EXTRACT_PROMPT_EV = EXTRACT_PROMPT.replace(
    """<thing> | <attribute> | <value> | <since YYYY-MM-DD> | correction
""", """<thing> | <attribute> | <value> | <since YYYY-MM-DD> | correction
+ <YYYY-MM-DD> | <something Sam did>
""").replace(
    """- Only facts Sam would expect you to remember later.""",
    """- "+" lines are what Sam did: each thing Sam did, attended, bought, made, fixed, sold, visited
  or finished, one line each, dated when it happened (work it out from "last Tuesday", "three
  weeks ago"; {day} if the conversation doesn't say), with the details someone might count or
  add up later (amounts, durations, names): "+ 2023-05-23 | baked sourdough bread (didn't rise)",
  "+ 2023-08-12 | attended cousin's wedding at a vineyard". Plans and wishes are not "+" lines.
- Only facts Sam would expect you to remember later.""")

# user-kinds (10-17, redesign steps 2+3; needs DELTOS_KINDS=1): the same, plus what
# kind of thing each item is ("how many pieces of jewelry" never matched "Engagement
# ring"; 248 of 547 evidence items missed the question's words on label and value),
# and plans kept apart from what Sam has or does ("I think I'll go with HBO" became
# the streaming service in use). Examples are made up, not from the benchmark.
EXTRACT_PROMPT_KINDS = EXTRACT_PROMPT.replace(
    '''"Sam | Europe trip destination | Rome")''', '''"Sam | Europe trip plan | Rome")''').replace(
    """- Only facts Sam would expect you to remember later.""",
    """- When a value is one particular item (something Sam owns, bought, got, made, attended, joined
  or took part in), add in brackets at the end of the value the general words someone would use
  to ask about all such items. Never on amounts, dates, places or a job:
  Sam | new purchase | Garmin Forerunner 265 [watch, fitness gadget] | 2023-04-02
  Sam | event attended | Lena's book launch at Powell's [book event, reading] | 2023-04-09
- Plans are facts too, under their own label: what Sam plans, is considering or wants ("I think
  I'll go with...", "I'm planning to...") goes under a label that says it is a plan ("streaming
  service plan", "planned trip destination"), never under the label of what Sam has or does now.
- Only facts Sam would expect you to remember later.""")
assert EXTRACT_PROMPT_KINDS.count("Garmin Forerunner 265") == 1 and "Europe trip plan" in EXTRACT_PROMPT_KINDS

READ_PROMPT = """You are a helpful assistant with long-term memory of your past conversations with
the user. This is what your memory returned for the question:

{context}

Current date: {date}. Answer the user's question from your memory. If memory does not contain
the answer, say you don't know. Be brief."""
# LoCoMo: the conversations are between two other people (the same rules otherwise).
READ_PROMPT_DUO = """You are a helpful assistant with long-term memory of past conversations between
{a} and {b}. This is what your memory returned for the question:

{context}

Current date: {date}. Answer the question from your memory. If memory does not contain
the answer, say you don't know. Be brief."""


def read_prompt(q: dict, context: str) -> str:
    if q.get("speakers"):
        a, b = q["speakers"]
        return READ_PROMPT_DUO.format(a=a, b=b, context=context, date=q["question_date"])
    return READ_PROMPT.format(context=context, date=q["question_date"])


# The official answer check, from LongMemEval's src/evaluation/evaluate_qa.py
# (MIT, Copyright (c) 2024 Di Wu), unchanged.
def anscheck_prompt(task: str, question: str, answer: str, response: str, abstention: bool = False) -> str:
    if abstention:
        return ("I will give you an unanswerable question, an explanation, and a response from a model. Please answer yes "
                "if the model correctly identifies the question as unanswerable. The model could say that the information "
                "is incomplete, or some other information is given but the asked information is not.\n\nQuestion: {}\n\n"
                "Explanation: {}\n\nModel Response: {}\n\nDoes the model correctly identify the question as unanswerable? "
                "Answer yes or no only.").format(question, answer, response)
    head = ("I will give you a question, a correct answer, and a response from a model. Please answer yes if the response "
            "contains the correct answer. Otherwise, answer no. ")
    steps = ("If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct "
             "answer, you should also answer yes. If the response only contains a subset of the information required by "
             "the answer, answer no. ")
    tail = "\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
    if task in ("single-session-user", "single-session-assistant", "multi-session"):
        t = head + steps.rstrip() + " " + tail.lstrip(" ")
    elif task == "temporal-reasoning":
        t = (head + steps + "In addition, do not penalize off-by-one errors for the number of days. If the question asks "
             "for the number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., predicting 19 days "
             "when the answer is 18), the model's response is still correct. " + tail.lstrip(" "))
    elif task == "knowledge-update":
        t = (head + "If the response contains some previous information along with an updated answer, the response "
             "should be considered as correct as long as the updated answer is the required answer." + tail)
    elif task == "single-session-preference":
        return ("I will give you a question, a rubric for desired personalized response, and a response from a model. "
                "Please answer yes if the response satisfies the desired response. Otherwise, answer no. The model does "
                "not need to reflect all the points in the rubric. The response is correct as long as it recalls and "
                "utilizes the user's personal information correctly.\n\nQuestion: {}\n\nRubric: {}\n\nModel Response: {}"
                "\n\nIs the model response correct? Answer yes or no only.").format(question, answer, response)
    else:
        raise ValueError(task)
    return t.format(question, answer, response)


# -- data ---------------------------------------------------------------------------

def day(stamp: str) -> str:
    """'2023/05/20 (Sat) 02:21' -> '2023-05-20'."""
    return stamp[:10].replace("/", "-")


def questions(types: list[str], n: int, skip: int = 0) -> list[dict]:
    """The first n questions of each type after its first `skip`, in file order
    (deterministic). Held out (never looked at while developing, up to 10-12):
    multi-session, temporal-reasoning and single-session-assistant with skip 30;
    single-session-user and -preference whole. Every knowledge-update question
    was used."""
    data = json.load(open(DATA))
    seen: Counter = Counter()
    per: Counter = Counter()
    out = []
    for q in data:
        t = q["question_type"]
        if types and t not in types:
            continue
        seen[t] += 1
        if seen[t] > skip and (not n or per[t] < n):
            per[t] += 1
            out.append(q)
    return out


def sessions(q: dict) -> list[tuple[str, str, list[dict]]]:
    """(session id, day, turns), oldest first."""
    # The file lists sessions in order; the clock times within a day are not
    # (618f13b2: "worn six times" at 17:45, "four times" at 22:16, gold six).
    rows = list(zip(q["haystack_session_ids"], q["haystack_dates"], q["haystack_sessions"]))
    order = sorted(range(len(rows)), key=lambda i: (day(rows[i][1]), i))
    return [(rows[i][0], day(rows[i][1]), rows[i][2]) for i in order]


def render(turns: list[dict], limit: int = 0) -> str:
    return "\n".join(f"{t['role']}: {t['content'][:limit] if limit else t['content']}" for t in turns)


# -- extraction (deltos's model step, once per unique session) ---------------------

# What deltos's importer reads of a chat (the whole chat is a page either way):
#   all   every turn
#   user  the user's turns whole, each assistant turn's opening only (what the user
#         is answering). The user's facts are in the user's turns (KU30: 61/61
#         answer turns) and they are 13% of the text: the import costs ~1/3.
#         The default since 10-10 (night11: KU30 30/30 vs 27-28 read whole,
#         KU78 73/78; import 1.74M vs 4.29M tokens).
READS = {"all": "extract2.jsonl", "user": "extract3-user.jsonl", "user-ev": "extract4-user-ev.jsonl",
         "user-kinds": "extract5-user-kinds.jsonl"}
READ = "user"
ASSISTANT_HEAD = 200


def for_import(turns: list[dict]) -> str:
    if READ == "all":
        return render(turns)
    return "\n".join(f"{t['role']}: {t['content']}" if t["role"] != "assistant" else
                     f"assistant: {t['content'][:ASSISTANT_HEAD]}{' ...' if len(t['content']) > ASSISTANT_HEAD else ''}"
                     for t in turns)


def extracted() -> dict[tuple[str, str], dict]:
    """(question id, session id) -> the logged model output for that session."""
    p = OUT / READS[READ]
    return {(r["qid"], r["sid"]): r for r in map(json.loads, open(p))} if p.exists() else {}


def extract_prompt(q: dict | None = None) -> str:
    if READ == "user-kinds" and not store.ITEM_KINDS:
        raise SystemExit("--read user-kinds writes kind words: run with DELTOS_KINDS=1")
    if q and q.get("speakers"):
        if READ != "user":
            raise SystemExit("LoCoMo has the default reading only (--read user)")
        a, b = q["speakers"]
        return EXTRACT_PROMPT_DUO.replace("{A}", a).replace("{B}", b)
    return {"user-ev": EXTRACT_PROMPT_EV, "user-kinds": EXTRACT_PROMPT_KINDS}.get(READ, EXTRACT_PROMPT)


def by_memory(qs: list[dict]) -> list[dict]:
    """One question per memory: a memory is built (and its sessions read) once."""
    return list({mid(q): q for q in qs}.values())


def extract(qs: list[dict], workers: int, say=print, redo_repairs: bool = False) -> None:
    """deltos's importer reads each question's chats in date order, the way
    memory grows: it sees what memory already holds (so a changed value keeps
    its label) and gets one repair call for lines the store refused. Logged per
    session; a question picks up where it stopped."""
    done, lock = extracted(), threading.Lock()
    qs = by_memory(qs)
    todo = sum(1 for q in qs for sid, _, _ in sessions(q) if (mid(q), sid) not in done)
    say(f"extract: {todo} sessions to read ({len(done)} logged)", flush=True)

    def go(q):
        qid = mid(q)
        root = OUT / "extracting" / qid
        shutil.rmtree(root, ignore_errors=True)
        # split/followup off: a cut-off or names-only extraction is not asked again,
        # so the benchmark's requests (and its cached extraction) stay as they were.
        m = Migrator(root, owner=owner_of(q), prompt=extract_prompt(q), split=False, followup=False)
        for sid, d, turns in sessions(q):
            text = f"Conversation on {d}:\n\n{for_import(turns)}"
            if (qid, sid) in done and redo_repairs:
                # The saved reading again, and a fresh repair call for what the
                # store refuses now (after a repair-pass fix).
                row = dict(done[(qid, sid)])
                st = m.session(sid, d, text, d, replay=row, redo_repair=True)
                row["repair_raw"] = st.get("repair_raw", "")
                with lock:
                    redone[(qid, sid)] = row
                continue
            if (qid, sid) in done:
                m.session(sid, d, text, d, replay=done[(qid, sid)])
                continue
            st = m.session(sid, d, text, d)
            row = {"qid": qid, "sid": sid, "day": d, "raw": st["raw"], "repair_raw": st.get("repair_raw", ""),
                   "prompt_tokens": st["prompt_tokens"], "completion_tokens": st["completion_tokens"]}
            with lock, open(OUT / READS[READ], "a") as fh:
                fh.write(json.dumps(row) + "\n")
        shutil.rmtree(root, ignore_errors=True)

    redone: dict = {}
    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(go, qs))
    if redo_repairs:
        rows = {**done, **redone}
        tmp = OUT / (READS[READ] + ".tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in rows.values()))
        tmp.replace(OUT / READS[READ])
        say(f"repairs redone: {sum(1 for r in redone.values() if r['repair_raw'])} sessions with a repair")


# -- systems ------------------------------------------------------------------------

def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def bm25(query: str, docs: list[str], k: int, k1: float = 1.5, b: float = 0.75) -> list[int]:
    toks = [_tokens(d) for d in docs]
    avg = sum(map(len, toks)) / max(1, len(toks))
    df = Counter(w for t in toks for w in set(t))
    q = set(_tokens(query))
    def score(t):
        tf = Counter(t)
        return sum(math.log(1 + (len(toks) - df[w] + 0.5) / (df[w] + 0.5)) * tf[w] * (k1 + 1)
                   / (tf[w] + k1 * (1 - b + b * len(t) / avg)) for w in q if w in tf)
    return sorted(range(len(docs)), key=lambda i: -score(toks[i]))[:k]


def ctx_full(q: dict) -> str:
    return "\n\n".join(f"### Chat on {d}\n{render(turns)}" for _, d, turns in sessions(q))


def ctx_rag(q: dict, k: int = 10) -> str:
    rounds = []
    for _, d, turns in sessions(q):
        for i in range(0, len(turns), 2):
            rounds.append((d, render(turns[i:i + 2], 1500)))
    best = sorted(bm25(q["question"], [r[1] for r in rounds], k))   # chronological among the best
    return "\n\n".join(f"[{rounds[i][0]}]\n{rounds[i][1]}" for i in best)


def code_key() -> str:
    """Which code and extraction built a store: a run never reuses stores an older
    deltos built (the cache used to be keyed by question only)."""
    h = hashlib.sha1()
    for p in sorted((Path(__file__).resolve().parents[1] / "deltos").rglob("*.py")):
        h.update(p.read_bytes())
    for ex in (OUT / READS[READ], SLOTS, ITEMS):
        if ex and ex.exists():
            h.update(f"{ex}:{ex.stat().st_size}:{ex.stat().st_mtime_ns}".encode())
    return h.hexdigest()[:10]


# One fact under two labels (deltos/slots.py): the model's same-fact decisions,
# cached. Set by --slots; the stores fold the pairs it says are one fact.
SLOTS: Path | None = None
_slots_cache: dict | None = None
_slots_lock = threading.Lock()


def slot_cache() -> dict:
    global _slots_cache
    if _slots_cache is None:
        _slots_cache = slots.load(SLOTS) if SLOTS else {}
    return _slots_cache


# Another one, or the same one changed (deltos/items.py): the model's decisions,
# cached. Set by --items; the import keeps "another one" beside the current value.
ITEMS: Path | None = None
_items_cache: dict | None = None
_items_lock = threading.Lock()
UNASKED: dict[str, int] = {}   # stores built with replacements the cache lacks (kept as "same")


def items_path(arg: str, cmd: str) -> Path | None:
    """--items: a decisions file, or "off". Default: the bench's own file,
    out/lme/items.json: `items` fills it, `run` applies it when it exists
    (10-14, Phase 2 landed for the bench; deltos itself keeps the check off)."""
    if arg == "off":
        return None
    if arg:
        return Path(arg)
    p = OUT / "items.json"
    return p if cmd == "items" or (cmd == "run" and p.exists()) else None


def item_cache() -> dict:
    global _items_cache
    if _items_cache is None:
        _items_cache = items.load(ITEMS)
    return _items_cache


def deltos_store(q: dict, cache: dict, rebuild: bool = False, root: Path | None = None,
                  decider: "items.Items | None" = None) -> Path:
    """The question's memory: its sessions' extracted facts, replayed in date order,
    and each session as a page. decider: the items check to consult (default: the
    --items cache, when set)."""
    if not root and not rebuild:
        with _build_lock:
            lk = _builds.setdefault(mid(q), threading.Lock())
        with lk:   # the questions of one conversation share its memory: build it once
            return _deltos_store(q, cache, False, None, decider)
    return _deltos_store(q, cache, rebuild, root, decider)


_build_lock = threading.Lock()
_builds: dict[str, threading.Lock] = {}


def _deltos_store(q: dict, cache: dict, rebuild: bool, root: Path | None, decider) -> Path:
    root = root or OUT / "stores" / code_key() / mid(q)
    if root.exists() and not rebuild:
        return root
    shutil.rmtree(root, ignore_errors=True)
    (root / "pages").mkdir(parents=True)
    own = decider is None and ITEMS
    if own:
        decider = items.Items(item_cache())
    m = Migrator(root, owner=owner_of(q), chat=True, items=decider, split=False, followup=False)
    for sid, d, turns in sessions(q):
        if (mid(q), sid) in cache:
            m.session(sid, d, for_import(turns), d, replay=cache[(mid(q), sid)])
        (root / "pages" / f"{d}-{sid}.md").write_text(
            f'---\ntitle: "Chat on {d}"\nupdated: {d}\n---\n\n{render(turns)}\n')
    if SLOTS:
        slots.apply(Store(root), slot_cache())
    if own and decider.pending:
        with _items_lock:
            UNASKED[mid(q)] = len(decider.pending)
    return root


def judge_slots(qs: list[dict], workers: int, say=print) -> None:
    """Ask the model about every candidate pair the cache lacks, per question
    store (built without folding, so each pair is seen as the import left it)."""
    global SLOTS
    path, SLOTS = SLOTS, None
    cache, ex = slot_cache(), extracted()
    done = [0]

    def go(q):
        root = deltos_store(q, ex, rebuild=True, root=OUT / "slotting" / mid(q))
        local = dict(cache)
        st = slots.apply(Store(root), local, judge=llm.chat)
        with _slots_lock:
            cache.update(local)
            slots.save(path, cache)
            done[0] += 1
            say(f"slots {done[0]}/{len(by_memory(qs))} {mid(q)}: {st}", flush=True)
        shutil.rmtree(root, ignore_errors=True)

    with ThreadPoolExecutor(workers) as ex_:
        list(ex_.map(go, by_memory(qs)))
    SLOTS = path


def judge_items(qs: list[dict], workers: int, say=print, rounds: int = 4) -> None:
    """Ask the model about every replacement the --items cache lacks. A store is
    rebuilt with the decisions so far; the events it meets that the cache lacks
    are asked; again until none are new (a decision changes what later values
    meet, so a second round finds a few more)."""
    global ITEMS
    cache = item_cache()   # the --items file's decisions so far: load them BEFORE ITEMS is switched off
    path, ITEMS = ITEMS, None   # (10-14: loaded after, every step started empty and overwrote the file)
    ex = extracted()
    done = [0]

    def go(q):
        root = OUT / "itemizing" / mid(q)
        st = {"events": 0, "asked": 0, "another": 0}
        for _ in range(rounds):
            with _slots_lock:
                local = dict(cache)
            d = items.Items(local)
            deltos_store(q, ex, rebuild=True, root=root, decider=d)
            st["events"] = len(d.events)
            if not d.pending:
                break
            got = items.ask(list(d.pending.values()), llm.chat)
            st["asked"] += len(d.pending)
            with _slots_lock:
                cache.update(got)
                items.save(path, cache)
            if not got:
                break   # the model answered nothing readable: don't loop on it
        st["another"] = sum(1 for e in d.events if local.get(e["key"]) == "another")
        with _slots_lock:
            done[0] += 1
            say(f"items {done[0]}/{len(by_memory(qs))} {mid(q)}: {st}", flush=True)
        shutil.rmtree(root, ignore_errors=True)

    with ThreadPoolExecutor(workers) as ex_:
        list(ex_.map(go, by_memory(qs)))
    ITEMS = path


# Mem0 and Graphiti (bench/others.py, their own venv): the context each one's
# search returned, per question. A question they haven't fully ingested is skipped.
OTHERS = ("mem0", "graphiti")


def others_ctx(system: str) -> dict[str, dict]:
    path = OUT / "others" / system / "ctx.jsonl"
    return {r["qid"]: r for r in map(json.loads, open(path))} if path.exists() else {}


def others_ingest_tokens(system: str, qids: set) -> int:
    path = OUT / "others" / system / "ingest.jsonl"
    return sum(r["tokens"] for r in map(json.loads, open(path)) if r["qid"] in qids) if path.exists() else 0


def memory_for(q: dict, cache: dict) -> Memory:
    root = deltos_store(q, cache)
    return Memory(config=Config(root=root, pages_write=root / "pages", owner=owner_of(q)), today=day(q["question_date"]))


def ctx_deltos(q: dict, cache: dict) -> str:
    return memory_for(q, cache).context(q["question"])


# deltos-tools: the block, the reading rule, and deltos's read-only tools, the
# way an agent uses deltos (an agent opens a page the block lists when its lines
# aren't enough). At most MAX_TOOL_ROUNDS rounds; every tool result counts as
# context.
READ_TOOLS = ("memory_recall", "memory_show", "page_search", "page_read")
MAX_TOOL_ROUNDS = 3
TOOLS_NOTE = ("\n\nIf the block above isn't enough, use the memory tools: page_read opens a page the block "
              "lists (a whole past chat), page_search finds exact words in all of them. At most 3 tool calls.")
# 10-17: the last round has no tools, and a reader that still wants a lookup writes the
# call out as text (DeepSeek's DSML markup ~12 answers a run, GLM's <tool_call> ~1); the
# judge read those as answers (DeepSeek's mostly as right abstentions). Such a reply counts
# as no answer: ask once, like an empty one.
TOOL_TEXT = re.compile(r"<[|｜]DSML[|｜]|</?tool_call>|<function_call", re.I)


def answer_tools(m: Memory, ctx: str, q: dict) -> tuple[str, dict, int, list]:
    tools = [{"type": "function", "function": t} for t in m.tools if t["name"] in READ_TOOLS]
    msgs = [{"role": "system", "content": read_prompt(q, ctx) + TOOLS_NOTE},
            {"role": "user", "content": q["question"]}]
    usage, extra_chars, calls = {"prompt_tokens": 0}, 0, []
    for rnd in range(MAX_TOOL_ROUNDS + 1):
        msg, u = llm.chat(msgs, tools=tools if rnd < MAX_TOOL_ROUNDS else None, max_tokens=READER_TOKENS,
                          extra=reader_extra(q))
        usage["prompt_tokens"] += u.get("prompt_tokens", 0)
        tc = msg.get("tool_calls") or []
        if not tc:
            text = (msg.get("content") or "").strip()
            if not text or TOOL_TEXT.search(text):
                # 10-11: after 3 rounds of lookups one answer came back empty;
                # ask once for the answer from what it has.
                msgs.append({"role": "user", "content": "Answer the question now, from what you found."})
                msg, u = llm.chat(msgs, max_tokens=READER_TOKENS, extra=reader_extra(q))
                usage["prompt_tokens"] += u.get("prompt_tokens", 0)
                text = (msg.get("content") or "").strip()
                if TOOL_TEXT.search(text):
                    text = ""   # still a lookup, not an answer
            return text, usage, extra_chars, calls
        msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tc})
        for c in tc:
            name = c["function"]["name"]
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
                out = m.call(name, args) if name in READ_TOOLS else f"{name} is not available here"
            except Exception as e:
                out = f"error: {e}"
            out = str(out)[:24000]
            extra_chars += len(out)
            calls.append(f"{name}({json.dumps(args)[:80] if isinstance(args, dict) else ''})")
            msgs.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": out})
    return "", usage, extra_chars, calls


# -- run ----------------------------------------------------------------------------

# 4096 until 10-11: deltos's short blocks made the reader think longer and 11
# answers came back empty (thinking hit the cap); rag and full never did.
READER_TOKENS = 16384
# Draws (10-16): at temperature 0 the reader gives the same answer to the same block,
# but any change to a block re-rolls its borderline questions (~1 in 10 of the
# changed ones flipped right/wrong), so one greedy run per version can't tell a
# small real effect from that re-roll. BENCH_DRAW=N: the reader samples
# (temperature BENCH_TEMP, default 0.7) with a seed made from N and the question,
# the same seed in every version (a paired draw). The judge and the doubt label
# stay at temperature 0.
DRAW = int(os.environ.get("BENCH_DRAW", "0"))
TEMP = float(os.environ.get("BENCH_TEMP", "0.7"))


def reader_extra(q: dict) -> dict | None:
    if not DRAW:
        return None
    return {"temperature": TEMP, "seed": DRAW * 1_000_003 + zlib.crc32(q["question_id"].encode()) % 1_000_000}


def answer(context: str, q: dict) -> tuple[str, dict]:
    msg, usage = llm.chat([{"role": "system", "content": read_prompt(q, context)},
                           {"role": "user", "content": q["question"]}], max_tokens=READER_TOKENS, extra=reader_extra(q))
    return (msg.get("content") or "").strip(), usage


def judge(q: dict, response: str) -> bool:
    p = anscheck_prompt(q.get("judge_type", q["question_type"]), q["question"], q["answer"], response,
                        "_abs" in q["question_id"])
    # Thinking off the way this server takes it (deltos.llm probes it once; on the
    # vLLM Flash-Next every result was graded with, that is the same
    # chat_template_kwargs switch the judge used to hard-code).
    msg, _ = llm.chat([{"role": "user", "content": p}], max_tokens=10, extra=llm.thinking(False))
    return "yes" in (msg.get("content") or "").lower()


def run(qs: list[dict], systems: list[str], workers: int, out: Path, say=print) -> dict:
    cache = extracted()
    report = {}
    for system in systems:
        path = out / f"{system}.jsonl"
        done = {r["qid"] for r in map(json.loads, open(path))} if path.exists() else set()
        lock = threading.Lock()
        searched = others_ctx(system) if system in OTHERS else {}

        def go(q):
            if q["question_id"] in done or (system in OTHERS and q["question_id"] not in searched):
                return
            t = time.time()
            m = memory_for(q, cache) if system.startswith("deltos") else None
            ctx = ctx_full(q) if system == "full" else ctx_rag(q) if system == "rag" else \
                searched[q["question_id"]]["context"] if system in OTHERS else m.context(q["question"])
            if system in ("deltos-rule", "deltos-tools"):   # deltos's reading rule with the block (an agent has it in its instructions)
                ctx = f"How to read it: {READING.replace(chr(10) + '  ', ' ')}\n\n{ctx}"
            t_ctx = time.time() - t
            extra, calls = 0, []
            try:
                if system == "deltos-tools":
                    resp, usage, extra, calls = answer_tools(m, ctx, q)
                else:
                    resp, usage = answer(ctx, q)
                ok = judge(q, resp)
                sure = doubt.ask(q["question"], resp) if q["question_type"] not in doubt.SKIP_TYPES else None
            except Exception as e:
                resp, usage, ok, sure = f"ERROR {e!r}"[:300], {}, False, None
            row = {"qid": q["question_id"], "type": q["question_type"], "abstention": "_abs" in q["question_id"],
                   "answer": resp, "gold": q["answer"], "correct": ok, **({"doubt": sure} if sure else {}),
                   "context_chars": len(ctx) + extra,
                   **({"tool_calls": calls} if system == "deltos-tools" else {}),
                   "context_seconds": round(searched[q["question_id"]]["seconds"] if system in OTHERS else t_ctx, 3),
                   "prompt_tokens": usage.get("prompt_tokens", 0),
                   "seconds": round(time.time() - t, 1)}
            with lock, open(path, "a") as fh:
                fh.write(json.dumps(row) + "\n")

        with ThreadPoolExecutor(workers) as ex:
            list(ex.map(go, qs))
        if not path.exists():
            say(system, "no questions ready")
            continue
        rows = [json.loads(l) for l in open(path)]
        by = Counter(r["type"] for r in rows)
        ok = Counter(r["type"] for r in rows if r["correct"])
        d = doubt.score(rows, doubt.load())
        report[system] = {"n": len(rows), "accuracy": round(sum(ok.values()) / max(1, len(rows)), 3),
                          "by_type": {t: f"{ok[t]}/{by[t]}" for t in sorted(by)},
                          "confidently_wrong": d["confidently_wrong"], "unlabeled": d["unlabeled"],
                          "avg_context_tokens": round(sum(r["context_chars"] for r in rows) / max(1, len(rows)) / 4),
                          "avg_context_seconds": round(sum(r["context_seconds"] for r in rows) / max(1, len(rows)), 3)}
        if system.startswith("deltos"):   # what storing the memory cost: the extractor, once per session
            ids = {r["qid"] for r in rows}   # every answered row, not only this call's questions (10-14:
            pool = qs if ids <= {q["question_id"] for q in qs} else questions([], 0)   # a resumed file undercounted)
            keys = {(mid(q), sid) for q in pool if q["question_id"] in ids for sid, _, _ in sessions(q)}
            report[system]["ingest_tokens"] = sum(cache[k]["prompt_tokens"] + cache[k]["completion_tokens"]
                                                  for k in keys if k in cache)
        if system in OTHERS:   # what storing the memory cost: every request their ingest made
            ids = {r["qid"] for r in rows}
            pool = qs if ids <= {q["question_id"] for q in qs} else questions([], 0)
            report[system]["ingest_tokens"] = others_ingest_tokens(system, {mid(q) for q in pool if q["question_id"] in ids})
        say(system, json.dumps(report[system]))
    if UNASKED:
        say(f"items: {sum(UNASKED.values())} replacements in {len(UNASKED)} stores built now were never asked "
            f"(kept as the same one changed); `bench.lme items` for these questions first")
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def fake_chat(messages, max_tokens=0, **kw):
    """A stub model for --fake: exercises every step without the brain."""
    text = messages[-1]["content"]
    if "Answer yes or no only" in text:
        return {"content": "yes" if "Model Response: I don't know" not in text else "no"}, {}
    if "structured memory about the user" in messages[0]["content"]:
        return {"content": f"{OWNER} | test attribute | stub value | -"}, {"prompt_tokens": len(text) // 4, "completion_tokens": 8}
    return {"content": "I don't know"}, {"prompt_tokens": len(messages[0]["content"]) // 4}


def main():
    global OUT, READ, SLOTS, ITEMS
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["extract", "run", "slots", "items"])
    ap.add_argument("--types", default="knowledge-update")
    ap.add_argument("--n", type=int, default=0, help="questions per type (0 = all)")
    ap.add_argument("--skip", type=int, default=0, help="skip the first N of each type (30 = held out)")
    ap.add_argument("--qids", default="", help="only these questions: a comma list or a file with one per line")
    ap.add_argument("--systems", default="full,rag,deltos")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--out", default="", help="results folder to resume")
    ap.add_argument("--fake", action="store_true", help="stub model: test the pipeline brain-free")
    ap.add_argument("--read", choices=list(READS), default=READ, help="what the importer reads of a chat")
    ap.add_argument("--redo-repairs", action="store_true", help="extract: replay the saved reading, repair again")
    ap.add_argument("--slots", default="", help="same-fact decisions (json): run folds them; slots fills them")
    ap.add_argument("--items", default="", help="another-one decisions (json; default out/lme/items.json, "
                                                "'off' = none): run keeps both; items fills them")
    a = ap.parse_args()
    READ = a.read
    if a.fake:
        llm.chat = fake_chat
        import deltos.sources.notes as notes
        notes.chat = fake_chat
        OUT = OUT / "fake"
        OUT.mkdir(parents=True, exist_ok=True)
    SLOTS = Path(a.slots) if a.slots else (OUT / "slots.json" if a.cmd == "slots" else None)
    ITEMS = items_path(a.items, a.cmd)
    qs = questions([t for t in a.types.split(",") if t], a.n, a.skip)
    if a.qids:
        want = set((Path(a.qids).read_text().split() if Path(a.qids).is_file() else a.qids.split(",")))
        qs = [q for q in qs if q["question_id"] in want]
    print(f"{len(qs)} questions: {dict(Counter(q['question_type'] for q in qs))}", flush=True)
    if a.cmd == "extract":
        extract(qs, a.workers, redo_repairs=a.redo_repairs)
    elif a.cmd == "slots":
        judge_slots(qs, a.workers)
    elif a.cmd == "items":
        judge_items(qs, a.workers)
    else:
        out = Path(a.out) if a.out else OUT / "results" / time.strftime("%Y%m%d-%H%M%S")
        out.mkdir(parents=True, exist_ok=True)
        run(qs, a.systems.split(","), a.workers, out)
        print("report ->", out / "report.json")


if __name__ == "__main__":
    main()
