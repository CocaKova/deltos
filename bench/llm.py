"""Benchmark helpers: the reader's answer rules and parsing. The chat client is deltos.llm."""
from __future__ import annotations

import json
import os
import re

TODAY = os.environ.get("MTB_TODAY", "2026-10-01")  # the synthetic world's "now"
MODEL = os.environ.get("MTB_MODEL", "qwen3.8-flash-next")

from deltos.llm import chat, configure  # noqa: E402  (one client for the bench and the importer)

configure(model=MODEL)


ANSWER_RULES = f"""Answer with JSON only: {{"answer": "<value only>", "confidence": <0..1>, "abstain": <true|false>}}
- "answer" is just the value (a place, a name, a port, a date as YYYY-MM-DD, or "retired").
- If memory does not contain the answer, set "abstain": true and "answer": "".
- Today is {TODAY}."""


def parse_answer(text: str) -> dict:
    text = text or ""
    for m in reversed(list(re.finditer(r"\{[^{}]*\}", text, re.S))):
        try:
            d = json.loads(m.group(0))
            if "answer" in d:
                return {"answer": str(d.get("answer") or ""), "confidence": float(d.get("confidence") or 0),
                        "abstain": bool(d.get("abstain"))}
        except (ValueError, TypeError):
            continue
    return {"answer": text.strip()[-200:], "confidence": 0.0, "abstain": False, "parse_error": True}
