"""Baseline: every note in the prompt. The ceiling at small scale, and it
doesn't scale, which is the point of having memory at all."""
from __future__ import annotations

from pathlib import Path

from ..llm import ANSWER_RULES, chat, parse_answer


class FullContext:
    name = "fullcontext"

    def setup(self, world_dir: Path):
        parts = []
        for p in sorted((world_dir / "notes").glob("*.md")):
            parts.append(f"=== {p.name} ===\n{p.read_text()}")
        # Notes first and identical every call so the server's prefix cache hits.
        self.system = ("You answer questions about a team's work using only the memory notes below.\n\n"
                       + "\n".join(parts) + "\n\n" + ANSWER_RULES)

    def answer(self, question: str) -> dict:
        msg, usage = chat([{"role": "system", "content": self.system},
                           {"role": "user", "content": question}])
        out = parse_answer(msg.get("content"))
        out["usage"] = usage
        return out
