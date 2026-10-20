"""Tests never reach a model: deltos's thinking probe would send real requests
to the configured endpoint, so tests say nothing about thinking unless a test
sets it (tests/test_llm.py)."""
import deltos.llm as llm

llm.ENDPOINT["thinking"] = "server"
llm.ENDPOINT["thinking_off"] = {}
