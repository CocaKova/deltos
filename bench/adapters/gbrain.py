"""gbrain as the memory: an ISOLATED instance (its own GBRAIN_HOME), notes
synced from a git repo, one hybrid query per question, dated hits injected
into the prompt. That mirrors how the Hermes gbrain plugin prefetches recall
each turn.

Never points at the live brain. The work dir has to live outside $HOME on a
machine whose live brain is in managed mode: gbrain refuses to sync a repo
under a "managed canonical worktree".
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

from ..llm import ANSWER_RULES, chat, parse_answer

EMBED_MODEL = os.environ.get("MTB_GBRAIN_EMBED", "llama-server:qwen3-embedding-0.6b")
EMBED_DIMS = os.environ.get("MTB_GBRAIN_DIMS", "1024")
EMBED_URL = os.environ.get("MTB_GBRAIN_EMBED_URL", "http://127.0.0.1:8889/v1")
LIMIT = int(os.environ.get("MTB_GBRAIN_LIMIT", "8"))


class GBrain:
    name = "gbrain"

    def setup(self, world_dir: Path):
        base = Path(os.environ.get("MTB_GBRAIN_DIR", "/tmp/mtb-gbrain"))
        self.work = base / world_dir.name
        if self.work.exists():
            shutil.rmtree(self.work)
        (self.work / "home").mkdir(parents=True)
        notes = self.work / "notes"
        shutil.copytree(world_dir / "notes", notes, ignore=shutil.ignore_patterns(".git"))
        self.env = {**os.environ, "GBRAIN_HOME": str(self.work / "home")}
        git = ["git", "-c", "user.name=bench", "-c", "user.email=bench@local"]
        for cmd in (["git", "init", "-q"], ["git", "add", "-A"], git + ["commit", "-qm", "world"]):
            subprocess.run(cmd, cwd=notes, check=True)
        self._gb("init", "--pglite", "--non-interactive", "--db-only",
                 "--embedding-model", EMBED_MODEL, "--embedding-dimensions", EMBED_DIMS)
        cfg = self.work / "home" / ".gbrain" / "config.json"
        c = json.loads(cfg.read_text())
        c["provider_base_urls"] = {EMBED_MODEL.split(":")[0]: EMBED_URL}
        cfg.write_text(json.dumps(c, indent=2))
        self._gb("sync", "--repo", str(notes), timeout=1800)
        self._gb("extract", "--stale")
        stats = self._gb("stats").stdout
        if "Pages:     0" in stats:
            raise RuntimeError(f"gbrain sync imported nothing:\n{stats}")
        self.lock = threading.Lock()  # PGLite is single-process; serialize queries

    def _gb(self, *args, timeout=600):
        r = subprocess.run(["gbrain", *args], cwd=self.work, env=self.env, capture_output=True,
                           text=True, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError(f"gbrain {args[0]} failed rc={r.returncode}: {r.stderr[-800:]}")
        return r

    def recall(self, question: str) -> list[dict]:
        with self.lock:
            out = self._gb("query", question, "--limit", str(LIMIT), "--json").stdout
        start = out.find("[")
        return json.loads(out[start:]) if start >= 0 else []

    def answer(self, question: str) -> dict:
        hits = self.recall(question)
        ctx = "\n\n".join(f"[{h.get('slug')} · {h.get('effective_date', '?')}]\n{h.get('chunk_text', '')}"
                          for h in hits)
        system = ("You answer questions about a team's work using your memory. These are the memory "
                  "results recalled for this question, each tagged [note · date]:\n\n" + ctx + "\n\n" + ANSWER_RULES)
        msg, usage = chat([{"role": "system", "content": system}, {"role": "user", "content": question}])
        out = parse_answer(msg.get("content"))
        out["usage"] = usage
        out["recalled"] = [h.get("slug") for h in hits]
        return out
