"""Mem0 and Graphiti on the same LongMemEval questions as deltos (bench/lme.py).

Their packages live in their own venv (out/venv-others), not the bench's Python:

  out/venv-others/bin/python -m bench.others ingest --system mem0 --types multi-session --n 30
  out/venv-others/bin/python -m bench.others search --system mem0 --types multi-session --n 30
  python3 -m bench.lme run --systems mem0 --types multi-session --n 30 --out ...

ingest: each question gets its own memory (Mem0: a local Qdrant + history db per
question; Graphiti: a group in Neo4j), fed the question's chat sessions oldest
first, whole (user and assistant turns), each with its own date. Resumable per
session (out/lme/others/<system>/ingest.jsonl). search: every fully ingested
question is searched once and the context written to ctx.jsonl; bench.lme
answers and judges it with the same reader and judge as every other system.

Settings, each the library's documented default unless noted:
- model: the same brain, temperature 0 and thinking off as deltos's import; at
  most 3 requests at once (a limit on the raw requests, across questions).
- embedder: Qwen3-Embedding-0.6B (1024 dims) on a separate machine; neither library has a
  default that runs here (both default to OpenAI's API).
- Mem0 2.x: add() per session with infer on; search top_k 20 (its default).
  LoCoMo (two people talking, bench/locomo.py): one Mem0 per conversation and one user
  id per speaker in it, each session added for each speaker with that speaker's turns as
  "user" and the other's as "assistant", every turn led by its speaker's name; a
  question searches both speakers, top 10 each (20 in all). That is how Mem0's own
  LoCoMo evaluation feeds it (two memories, both searched).
  The session's date goes into the extraction prompt's Observation Date: OSS
  Mem0 leaves it at today's date (its timestamp argument is platform-only), so
  "last week" in a 2023 chat would resolve to 2026. Each memory keeps the date
  of the chat it came from in its metadata, shown beside it.
- Graphiti: add_episode per session (source message, reference_time the
  session's date); search_ with the hybrid RRF recipes, 20 facts and 20
  entities, laid out like Zep's LongMemEval context (facts with their valid
  dates, then entity summaries; Rasmussen et al., arXiv:2501.13956, see
  THIRD-PARTY.md). No LLM at search time (RRF, no cross-encoder).
- Telemetry off for both (MEM0_TELEMETRY, GRAPHITI_TELEMETRY_ENABLED).
"""
from __future__ import annotations

import os

os.environ.setdefault("MEM0_TELEMETRY", "False")
os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")

import argparse
import asyncio
import contextvars
import json
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import bench.lme as L

ROOT = L.OUT / "others"
BRAIN = os.environ.get("DELTOS_LLM_URL", "http://127.0.0.1:8000/v1")
MODEL = os.environ.get("OTHERS_MODEL", "qwen3.8-flash-next")
MAX_REQUESTS = 3
TOP = 20


def env() -> dict:
    """Neo4j and embedder addresses; the password stays out of git (out/ is ignored)."""
    vals = dict(l.strip().split("=", 1) for l in open(ROOT / "env") if "=" in l)
    return {**vals, **{k: os.environ[k] for k in vals if k in os.environ}}


class Log:
    """One row per ingested session (system, qid, sid, calls, tokens, seconds); the resume point."""

    def __init__(self, system: str):
        self.path = ROOT / system / "ingest.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.done = {(r["qid"], r["sid"]) for r in map(json.loads, open(self.path))} if self.path.exists() else set()

    def write(self, row: dict):
        with self.lock, open(self.path, "a") as fh:
            fh.write(json.dumps(row) + "\n")
            self.done.add((row["qid"], row["sid"]))


def claim(system: str, qid: str) -> bool:
    """--claim: several ingest processes (one per brain) share one question list,
    each question going to the first with a free worker. Whoever starts the
    processes clears ROOT/<system>/claims first."""
    d = ROOT / system / "claims"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.close(os.open(d / qid, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        return False


def _no_think(k: dict) -> dict:
    """Thinking off, as deltos's import runs (deltos/sources/notes.py)."""
    body = dict(k.get("extra_body") or {})
    body.setdefault("chat_template_kwargs", {"enable_thinking": False})
    return {**k, "extra_body": body}


class Meter:
    """Counts a client's raw chat requests and their tokens; holds at most MAX_REQUESTS at once."""

    def __init__(self):
        self.calls = Counter()
        self.tokens = Counter()
        self.sync_gate = threading.BoundedSemaphore(MAX_REQUESTS)
        self.async_gate: asyncio.Semaphore | None = None

    def _count(self, key, r):
        u = getattr(r, "usage", None)
        self.calls[key] += 1
        if u:
            self.tokens[key] += (u.prompt_tokens or 0) + (u.completion_tokens or 0)

    def wrap_sync(self, completions, key_of):
        create = completions.create

        def limited(*a, **k):
            with self.sync_gate:
                r = create(*a, **_no_think(k))
            self._count(key_of(), r)
            return r
        completions.create = limited

    def wrap_async(self, completions, key_of):
        create = completions.create

        async def limited(*a, **k):
            if self.async_gate is None:
                self.async_gate = asyncio.Semaphore(MAX_REQUESTS)
            async with self.async_gate:
                r = await create(*a, **_no_think(k))
            self._count(key_of(), r)
            return r
        completions.create = limited


# -- Mem0 -----------------------------------------------------------------------------

_local = threading.local()


def _mem0_dated():
    """Pass the session's date as the extraction prompt's Observation Date (see top)."""
    import mem0.memory.main as M
    if getattr(M, "_bench_dated", False):
        return
    orig = M.generate_additive_extraction_prompt

    def dated(*a, **k):
        d = getattr(_local, "day", None)
        if d:
            k["timestamp"], k["current_date"] = d, d
        return orig(*a, **k)
    M.generate_additive_extraction_prompt = dated
    M._bench_dated = True


_mems: dict = {}
_mems_lock = threading.Lock()


def mem0_memory(qid: str):
    """One Mem0 per question (a local Qdrant folder takes one client per process)."""
    with _mems_lock:
        if qid not in _mems:
            _mems[qid] = _mem0_open(qid)
        return _mems[qid]


def mem0_close(qid: str):
    with _mems_lock:
        m = _mems.pop(qid, None)
    if m:
        m.vector_store.client.close()


def _mem0_open(qid: str):
    from mem0 import Memory
    root = ROOT / "mem0" / "stores" / qid
    root.mkdir(parents=True, exist_ok=True)
    e = env()
    return Memory.from_config({
        "llm": {"provider": "openai", "config": {"model": MODEL, "openai_base_url": BRAIN, "api_key": "none",
                                                 "temperature": 0, "max_tokens": 8192}},
        "embedder": {"provider": "openai", "config": {"model": "qwen3-embedding-0.6b", "openai_base_url": e["EMBED_URL"],
                                                      "api_key": "none"}},
        "vector_store": {"provider": "qdrant", "config": {"collection_name": "memories", "path": str(root / "qdrant"),
                                                          "embedding_model_dims": 1024, "on_disk": True}},
        "history_db_path": str(root / "history.db"),
    })


def units(q: dict) -> list[tuple[str, str, str, list[dict]]]:
    """What Mem0 is fed for a question's memory, oldest first: (unit id, day, user id,
    messages). LongMemEval: each session, user "sam". LoCoMo: each session once per
    speaker (unit "<session>:<speaker>"), that speaker as "user"."""
    out = []
    for sid, d, turns in L.sessions(q):
        if not q.get("speakers"):
            out.append((sid, d, "sam", [{"role": x["role"], "content": x["content"]} for x in turns if x.get("content")]))
            continue
        for who in q["speakers"]:
            out.append((f"{sid}:{who}", d, who, [{"role": "user" if x["role"] == who else "assistant",
                                                  "content": f"{x['role']}: {x['content']}"}
                                                 for x in turns if x.get("content")]))
    return out


def mem0_ingest(qs: list[dict], workers: int, say=print):
    _mem0_dated()
    log, meter = Log("mem0"), Meter()

    def go(q):
        qid = L.mid(q)
        todo = [u for u in units(q) if (qid, u[0]) not in log.done]
        if not todo:
            return
        m = mem0_memory(qid)
        meter.wrap_sync(m.llm.client.chat.completions, lambda: (_local.qid, _local.sid))
        for sid, d, user, msgs in todo:
            _local.qid, _local.sid, _local.day = qid, sid, d
            t = time.time()
            err = ""
            try:
                m.add(msgs, user_id=user, metadata={"date": d, "session": sid.split(":")[0]})
            except Exception as ex:   # logged, not retried: one failed session is a gap, like an import error
                err = repr(ex)[:300]
            log.write({"qid": qid, "sid": sid, "day": d, "calls": meter.calls[(qid, sid)],
                       "tokens": meter.tokens[(qid, sid)], "seconds": round(time.time() - t, 1),
                       **({"error": err} if err else {})})
        mem0_close(qid)
        say(f"mem0 {qid}: {len(todo)} sessions", flush=True)

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(go, L.by_memory(qs)))


def _mem0_rows(m, q: dict, user: str, top: int) -> list[str]:
    res = m.search(q["question"], filters={"user_id": user}, top_k=top)
    rows = res.get("results", res) if isinstance(res, dict) else res
    return [f"- [{(r.get('metadata') or {}).get('date', '?')}] {r['memory']}" for r in rows]


def mem0_context(q: dict) -> str:
    m = mem0_memory(L.mid(q))
    try:
        if not q.get("speakers"):
            lines = _mem0_rows(m, q, "sam", TOP)
            return (f"Memories (Mem0 search, the {TOP} best for the question), each with the date of the chat it came from:\n"
                    + "\n".join(lines)) if lines else "(no memories)"
        parts = []
        for who in q["speakers"]:
            lines = _mem0_rows(m, q, who, TOP // 2)
            parts.append(f"Memories of {who} (Mem0 search, the {TOP // 2} best for the question), each with the date "
                         f"of the conversation it came from:\n" + ("\n".join(lines) if lines else "(none)"))
        return "\n\n".join(parts)
    finally:
        mem0_close(L.mid(q))


# -- Graphiti -------------------------------------------------------------------------

def graphiti_client(meter: Meter | None = None, key_of=None):
    from openai import AsyncOpenAI
    from graphiti_core import Graphiti
    from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
    from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
    e = env()
    cfg = LLMConfig(api_key="none", base_url=BRAIN, model=MODEL, small_model=MODEL, temperature=0)
    raw = AsyncOpenAI(api_key="none", base_url=BRAIN)
    if meter:
        meter.wrap_async(raw.chat.completions, key_of)
    return Graphiti(e["NEO4J_URI"], e["NEO4J_USER"], e["NEO4J_PASSWORD"],
                    llm_client=OpenAIGenericClient(config=cfg, client=raw),
                    embedder=OpenAIEmbedder(OpenAIEmbedderConfig(api_key="none", base_url=e["EMBED_URL"],
                                                                 embedding_model="qwen3-embedding-0.6b",
                                                                 embedding_dim=1024)),
                    cross_encoder=OpenAIRerankerClient(config=cfg))   # unused: search is RRF


# The session a request belongs to, for the meter: asyncio copies it into child tasks.
_session: contextvars.ContextVar = contextvars.ContextVar("session", default=("?", "?"))


def group(qid: str) -> str:
    return "lme_" + "".join(c if c.isalnum() or c in "-_" else "_" for c in qid)


async def _graphiti_ingest(qs: list[dict], workers: int, say=print, shared: bool = False):
    from graphiti_core.nodes import EpisodeType
    log, meter = Log("graphiti"), Meter()
    g = graphiti_client(meter, _session.get)
    await g.build_indices_and_constraints()
    gate = asyncio.Semaphore(workers)

    async def go(q):
        qid = q["question_id"]
        if not [s for s in L.sessions(q) if (qid, s[0]) not in log.done]:
            return
        async with gate:
            if shared and not claim("graphiti", qid):
                return
            todo = [s for s in L.sessions(q) if (qid, s[0]) not in log.done]
            for sid, d, turns in todo:
                _session.set((qid, sid))   # each go() is its own task; Graphiti's child tasks inherit this
                t, err = time.time(), ""
                try:
                    await g.add_episode(name=f"chat {d} {sid}", episode_body=L.render(turns),
                                        source_description="chat with the assistant",
                                        reference_time=datetime.fromisoformat(d).replace(tzinfo=timezone.utc),
                                        source=EpisodeType.message, group_id=group(qid))
                except Exception as ex:
                    err = repr(ex)[:300]
                log.write({"qid": qid, "sid": sid, "day": d, "calls": meter.calls[(qid, sid)],
                           "tokens": meter.tokens[(qid, sid)], "seconds": round(time.time() - t, 1),
                           **({"error": err} if err else {})})
            say(f"graphiti {qid}: {len(todo)} sessions", flush=True)

    try:
        await asyncio.gather(*(go(q) for q in qs))
    finally:
        await g.close()


def graphiti_ingest(qs, workers, say=print, shared=False):
    asyncio.run(_graphiti_ingest(qs, workers, say, shared))


async def _graphiti_contexts(qs: list[dict]) -> dict[str, str]:
    from graphiti_core.search.search_config import SearchConfig
    from graphiti_core.search.search_config_recipes import EDGE_HYBRID_SEARCH_RRF, NODE_HYBRID_SEARCH_RRF
    g = graphiti_client()
    cfg = SearchConfig(edge_config=EDGE_HYBRID_SEARCH_RRF.edge_config, node_config=NODE_HYBRID_SEARCH_RRF.node_config,
                       limit=TOP)
    out = {}
    try:
        for q in qs:
            r = await g.search_(q["question"], config=cfg, group_ids=[group(q["question_id"])])
            day = lambda x: x.strftime("%Y-%m-%d") if x else "present"
            facts = [f"  - {e.fact} (Date range: {day(e.valid_at)} - {day(e.invalid_at)})" for e in r.edges]
            ents = [f"  - {n.name}: {n.summary}" for n in r.nodes]
            out[q["question_id"]] = (
                "FACTS and ENTITIES represent relevant context to the current conversation.\n\n"
                "# These are the most relevant facts and their valid date ranges\n"
                "# format: FACT (Date range: from - to)\n<FACTS>\n" + "\n".join(facts) + "\n</FACTS>\n\n"
                "# These are the most relevant entities\n# ENTITY_NAME: entity summary\n<ENTITIES>\n"
                + "\n".join(ents) + "\n</ENTITIES>")
    finally:
        await g.close()
    return out


# -- commands ---------------------------------------------------------------------------

def complete(system: str, qs: list[dict]) -> list[dict]:
    done = Log(system).done
    if system == "mem0":
        return [q for q in qs if all((L.mid(q), u[0]) in done for u in units(q))]
    return [q for q in qs if all((q["question_id"], sid) in done for sid, _, _ in L.sessions(q))]


def search(system: str, qs: list[dict], say=print):
    path = ROOT / system / "ctx.jsonl"
    have = {r["qid"] for r in map(json.loads, open(path))} if path.exists() else set()
    ready = [q for q in complete(system, qs) if q["question_id"] not in have]
    t = time.time()
    if system == "mem0":
        ctx = {}
        for q in ready:
            ctx[q["question_id"]] = mem0_context(q)
    else:
        ctx = asyncio.run(_graphiti_contexts(ready))
    per = (time.time() - t) / max(1, len(ready))
    with open(path, "a") as fh:
        for q in ready:
            fh.write(json.dumps({"qid": q["question_id"], "context": ctx[q["question_id"]], "seconds": round(per, 3)}) + "\n")
    say(f"{system}: searched {len(ready)} questions ({len(qs) - len(complete(system, qs))} not fully ingested)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["ingest", "search", "status"])
    ap.add_argument("--system", choices=["mem0", "graphiti"], required=True)
    ap.add_argument("--types", default="knowledge-update")
    ap.add_argument("--n", type=int, default=0, help="questions per type (0 = all)")
    ap.add_argument("--ids", default="", help="only these question ids (comma-separated)")
    ap.add_argument("--workers", type=int, default=3, help="questions ingested at once")
    ap.add_argument("--claim", action="store_true", help="graphiti: share the questions with other ingest processes")
    a = ap.parse_args()
    qs = L.questions([t for t in a.types.split(",") if t], a.n)
    if a.ids:
        qs = [q for q in qs if q["question_id"] in a.ids.split(",")]
    if a.cmd == "ingest":
        if a.system == "mem0":
            mem0_ingest(qs, a.workers)
        else:
            graphiti_ingest(qs, a.workers, shared=a.claim)
    elif a.cmd == "search":
        search(a.system, qs)
    rows = [json.loads(l) for l in open(Log(a.system).path)] if Log(a.system).path.exists() else []
    ids = {L.mid(q) for q in qs}
    rows = [r for r in rows if r["qid"] in ids]
    total = sum(len(units(q)) if a.system == "mem0" else len(L.sessions(q)) for q in L.by_memory(qs))
    if rows:
        print(f"{a.system}: {len(rows)}/{total} sessions, {len(complete(a.system, qs))}/{len(qs)} questions complete; "
              f"{sum(r['calls'] for r in rows) / len(rows):.1f} calls, {sum(r['tokens'] for r in rows) / len(rows):.0f} tokens, "
              f"{sum(r['seconds'] for r in rows) / len(rows):.1f} s per session; errors {sum('error' in r for r in rows)}")


if __name__ == "__main__":
    main()
