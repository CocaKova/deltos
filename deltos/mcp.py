"""deltos as an MCP server over stdio: `deltos serve`.

Any MCP client (Claude Code, Hermes, Cursor, ...) gets deltos's tools, and the
memory instructions arrive with the handshake (InitializeResult.instructions).
The per-turn memory block is not an MCP concept: harnesses that can inject
context per message call `deltos context` (or the `deltos hook` adapters);
the others can call memory_recall.

Newline-delimited JSON-RPC 2.0 on stdin/stdout, stdlib only. Logs go to stderr.
"""
from __future__ import annotations

import json
import sys

from . import __version__
from .memory import Memory, Refused

PROTOCOL = "2025-06-18"


def _tool_list(m: Memory) -> list[dict]:
    return [{"name": t["name"], "description": t["description"], "inputSchema": t["parameters"]} for t in m.tools]


def handle(m: Memory, req: dict) -> dict | None:
    """One request in, one response out (None for a notification)."""
    method, rid = req.get("method", ""), req.get("id")
    if rid is None:   # notifications/initialized, notifications/cancelled, ...
        return None
    try:
        if method == "initialize":
            asked = (req.get("params") or {}).get("protocolVersion") or PROTOCOL
            result = {"protocolVersion": asked, "capabilities": {"tools": {}},
                      "serverInfo": {"name": "deltos", "version": __version__},
                      "instructions": m.instructions()}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": _tool_list(m)}
        elif method == "tools/call":
            p = req.get("params") or {}
            try:
                text, err = m.call(p.get("name", ""), p.get("arguments") or {}), False
            except Refused as e:
                text, err = str(e), True
            result = {"content": [{"type": "text", "text": text}], "isError": err}
        else:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    except Exception as e:   # a bug: answer the request instead of killing the server
        print(f"deltos: {method} failed: {e!r}", file=sys.stderr, flush=True)
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def serve(m: Memory, stdin=sys.stdin, stdout=sys.stdout) -> None:
    for line in stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
        except ValueError:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            out = handle(m, req) if isinstance(req, dict) else \
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
        if out is not None:
            stdout.write(json.dumps(out) + "\n")
            stdout.flush()
