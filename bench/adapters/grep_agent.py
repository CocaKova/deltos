"""AMR-style memory: a MEMORY.md index in the prompt, plus grep and read tools
over the notes. No embeddings. The agent navigates like it would a codebase."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..llm import ANSWER_RULES, chat, parse_answer

MAX_STEPS = 10

TOOLS = [
    {"type": "function", "function": {
        "name": "grep", "description": "Case-insensitive regex search over all memory notes. Returns matching lines as file:line:text.",
        "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "read", "description": "Read one memory note by path, e.g. notes/tamsin.md",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
]


class GrepAgent:
    name = "grep"

    def setup(self, world_dir: Path):
        self.root = world_dir
        self.system = ("You answer questions about a team's work using your memory, a folder of markdown "
                       "notes. Use the grep and read tools to look things up. Here is the index:\n\n"
                       + (world_dir / "MEMORY.md").read_text() + "\n\nWhen you are done, reply with no tool "
                       "call and the final answer.\n" + ANSWER_RULES)

    def _grep(self, pattern: str) -> str:
        r = subprocess.run(["rg", "-i", "-n", "--no-heading", "-e", pattern, "notes"], cwd=self.root,
                           capture_output=True, text=True, timeout=20)
        lines = r.stdout.splitlines()
        if not lines:
            return "(no matches)" if r.returncode == 1 else f"(error: {r.stderr.strip()[:200]})"
        more = f"\n... {len(lines) - 40} more lines" if len(lines) > 40 else ""
        return "\n".join(lines[:40]) + more

    def _read(self, path: str) -> str:
        p = (self.root / path).resolve()
        if not str(p).startswith(str(self.root.resolve())) or not p.is_file():
            return "(no such note)"
        return p.read_text()[:6000]

    def answer(self, question: str) -> dict:
        msgs = [{"role": "system", "content": self.system}, {"role": "user", "content": question}]
        usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
        calls = 0
        for _ in range(MAX_STEPS):
            msg, usage = chat(msgs, tools=TOOLS)
            for k in usage_total:
                usage_total[k] += usage.get(k, 0)
            tcs = msg.get("tool_calls") or []
            if not tcs:
                out = parse_answer(msg.get("content"))
                break
            msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tcs})
            for tc in tcs:
                calls += 1
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except ValueError:
                    args = {}
                fn = tc["function"]["name"]
                res = self._grep(args.get("pattern", "")) if fn == "grep" else \
                    self._read(args.get("path", "")) if fn == "read" else "(unknown tool)"
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": res})
        else:
            out = {"answer": "", "confidence": 0.0, "abstain": True, "step_limit": True}
        out["usage"] = usage_total
        out["tool_calls"] = calls
        return out
