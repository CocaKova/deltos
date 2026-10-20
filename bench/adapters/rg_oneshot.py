"""One-shot ripgrep memory: no agent loop, no embeddings.

A thin adapter over deltos's read path (deltos/recall.py): one ripgrep pass
by thing, then one model call answers from the lines it found.
"""
from __future__ import annotations

from pathlib import Path

from deltos.recall import Recall

from ..llm import ANSWER_RULES, chat, parse_answer


class RgOneShot:
    name = "rg1"

    def setup(self, world_dir: Path):
        self.recall = Recall(world_dir)

    def answer(self, question: str) -> dict:
        ctx, n = self.recall.context(question)
        system = ("You answer questions about a team's work using your memory. This is what memory has "
                  "on the question:\n\n" + ctx + "\n\n" + ANSWER_RULES)
        msg, usage = chat([{"role": "system", "content": system}, {"role": "user", "content": question}])
        out = parse_answer(msg.get("content"))
        out["usage"] = usage
        out["lines"] = n
        return out
