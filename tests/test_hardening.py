"""Memory under stress: several writers at once, a writer killed mid-write, a
nightly import landing on an agent's newer value, and the MCP server over a
real pipe. Each runs real processes against a temporary memory folder."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from deltos.store import Store, parse_thing

REPO = Path(__file__).resolve().parents[1]

WRITER = """
import sys
sys.path.insert(0, {repo!r})
from pathlib import Path
from deltos.store import Store
L = "abcdefghijklmnopqrstuvwxyz"
s = Store(Path({root!r}))
for i in range({n}):
    tag = L[i // 676 % 26] + L[i // 26 % 26] + L[i % 26]
    s.remember("Tamsin", "setting {w}k" + tag, "on", source="{w}")
    s.remember("Tamsin", "status", "busy {w}")
    if i % 10 == 0:
        s.name("Thing {w} " + tag, [], "machine")
"""

WRITERS = ["alpha", "bravo", "charlie", "delta"]


def run_writer(root: Path, w: str, n: int) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", WRITER.format(repo=str(REPO), root=str(root), w=w, n=n)])


def check_store(root: Path) -> Store:
    """Every note parses, no temp file is left once memory opens, and the
    index names every thing."""
    s = Store(root)
    notes = root / "notes"
    assert not list(notes.glob(".*.tmp*")) and not list(root.glob(".*.tmp*")), "temp files left behind"
    for p in notes.glob("*.md"):
        t = parse_thing(p.read_text())
        assert p.name.endswith(("-history.md", "-extra.md")) or t.name, f"{p.name} has no title"
    index = (root / "MEMORY.md").read_text()
    for stem in s.rows:
        assert f"(notes/{stem}.md)" in index, f"{stem} missing from MEMORY.md"
    return s


class HardeningTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.root = Path(self.d.name)
        Store(self.root, today="2026-10-08").name("Tamsin", ["TS-2"], "machine")

    def test_concurrent_writers_lose_nothing(self):
        procs = [run_writer(self.root, w, 30) for w in WRITERS]
        for p in procs:
            self.assertEqual(p.wait(timeout=120), 0)
        s = check_store(self.root)
        t = s.find("Tamsin")
        for w in WRITERS:   # every writer's every entry is there: no write overwrote another
            self.assertEqual(sum(1 for k in t.attrs if k.startswith(f"setting {w}k")), 30)
            self.assertTrue(s.find(f"Thing {w} aaa"))
        self.assertTrue(t.attrs["status"].value.startswith("busy "))

    def test_writer_killed_mid_write_leaves_whole_notes(self):
        for _ in range(3):
            p = run_writer(self.root, "echo", 2000)
            time.sleep(0.6)
            p.send_signal(signal.SIGKILL)
            p.wait()
        s = check_store(self.root)
        t = s.find("Tamsin")
        self.assertIn("status", t.attrs)
        self.assertGreater(sum(1 for k in t.attrs if k.startswith("setting echok")), 10)   # it was mid-run
        # and memory keeps working after the kills
        self.assertIn("new", s.remember("Tamsin", "host", "the Annex"))

    def test_import_never_overrides_a_newer_agent_value(self):
        s = Store(self.root, today="2026-10-08")
        s.remember("Tamsin", "host", "Bay 3", since="2026-10-08", source="Ava")   # the agent, today
        # the nightly import reads an older note that still says the old host
        msg = s.remember("Tamsin", "host", "the Annex", since="2026-09-01", source="import", confirm=False)
        a = s.find("Tamsin").attrs["host"]
        self.assertEqual((a.value, a.source), ("Bay 3", "Ava"), msg)
        self.assertEqual(a.trail[0].value, "the Annex")    # kept as the earlier value
        # an import repeating the current value is not a confirmation
        s.today = "2026-10-09"
        s.remember("Tamsin", "host", "Bay 3", since="2026-10-08", source="import", confirm=False)
        self.assertEqual(s.find("Tamsin").attrs["host"].confirmed, "")

    def test_mcp_server_over_a_real_pipe(self):
        env = dict(os.environ, PYTHONPATH=str(REPO))
        p = subprocess.Popen([sys.executable, "-m", "deltos", "--root", str(self.root), "--agent", "Ava", "serve"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env=env, cwd=REPO)
        reqs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "remember", "arguments": {"thing": "TS-2", "attribute": "host", "value": "Bay 3"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "memory_show", "arguments": {"thing": "Tamsin"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "page_edit", "arguments": {"ref": "missing", "new_text": "x"}}},
        ]
        out, err = p.communicate("\n".join(json.dumps(r) for r in reqs) + "\nnot json\n", timeout=60)
        rs = [json.loads(ln) for ln in out.splitlines()]
        by = {r["id"]: r for r in rs if r.get("id") is not None}
        self.assertEqual(len(rs), 6, err)                      # the notification gets no answer
        self.assertIn("Memory", by[1]["result"]["instructions"])
        self.assertEqual(len(by[2]["result"]["tools"]), 11)
        self.assertFalse(by[3]["result"]["isError"])
        self.assertIn("Bay 3", by[4]["result"]["content"][0]["text"])
        self.assertIn("source: Ava", by[4]["result"]["content"][0]["text"])
        self.assertTrue(by[5]["result"]["isError"])            # refused, said to the agent
        self.assertEqual(rs[-1]["error"]["code"], -32700)      # bad JSON: an error, not a dead server
        self.assertEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main()
