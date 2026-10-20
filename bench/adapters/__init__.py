"""Memory adapters. Each one gets the world dir once (setup) and then answers
questions with the same answering model, so only the memory differs.

An adapter is a class with:
    name: str
    setup(world_dir: Path) -> None
    answer(question: str) -> dict  # {answer, confidence, abstain, usage, ...}
"""
from .fullcontext import FullContext
from .grep_agent import GrepAgent
from .gbrain import GBrain
from .rg_oneshot import RgOneShot

ADAPTERS = {a.name: a for a in (FullContext, GrepAgent, GBrain, RgOneShot)}
