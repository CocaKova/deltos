"""deltos.toml: where memory lives and what feeds it.

Looked up as $DELTOS_CONFIG, else <root>/deltos.toml, with root =
$DELTOS_ROOT or ~/deltos. Every key is optional:

    owner = "Sam"                        # whose memory this is (the instructions name them)
    agent = "Ava"                        # the agent writing through an adapter (each fact's source)

    [pages]
    write = "~/notes"                    # documents the agent may edit
    read = { memory = "~/.claude/projects/<project>/memory" }   # read-only, by prefix

    [hermes]                             # the Hermes adapter (adapters/hermes)
    extra_block = "~/.hermes/extra_block.py"   # optional: a file whose build_block() adds to the instructions
    skip_ids = "^(probe-|check_)"        # optional: a regex of session ids not to learn from (a bench's
                                         # probe sessions); unset = none
    cron_block = "full"                  # a scheduled (cron) run's per-turn block: "full" (default), "facts"
                                         # (no pages: a job's prompt is the same every day, so are its page
                                         # matches) or "off"

    [recall]
    doubt_days = 21                      # "(since ...; not confirmed in 5 weeks)" on a current entry that drifts
                                         # in live state (status, host, port, model, version, ...) when its
                                         # newest date is this old; 0 or unset = off ($DELTOS_DOUBT_DAYS wins)
    cues = true                          # one line at the end of the block when the message may change a known
                                         # thing ("remember it before answering") or the block shows "not
                                         # confirmed" lines (deltos/cues.py); unset = off ($DELTOS_CUES wins)

    [tools]
    descriptions = "when"                # tool descriptions that also say WHEN to use each tool (remember: the
                                         # moment a value changes); "plain" (default) = what each tool does
                                         # ($DELTOS_TOOL_DESCRIPTIONS wins)

    [sessions]                           # chat digests under <root>/sessions
    hermes = "~/.hermes"

    [learn]                              # finished chats -> facts (deltos/learn.py; needs [llm], owner and
                                         # [sessions] hermes)
    auto = true                          # learn each chat once it ends, while the agent is idle (needs [llm] url,
                                         # owner and [sessions] hermes); unset or false: only `deltos learn` by hand
    days = 2                             # chats started in the last N days (and resumed ones already learned)
    max_units = 20                       # chats per sweep, oldest first; the rest wait for the next one

    [import]                             # deltos import: notes -> facts (needs [llm]); both sources optional
    folders = ["~/notes:notes"]          # any folder of markdown as path:label, no git needed: each note's new
                                         # lines against the copy imported last (<root>/.sources/<label>/);
                                         # a git repo goes here or under repos, not both
    repos = ["~/notes:HEAD:notes"]       # git repos as path:revision:label; each new commit's lines
    skip = ["ARCHIVE-index", ".bak"]
    workers = 2
    notes_prompt = "v3"                  # fenced reference + worked example (default); "v1": the first prompt; "v4": v3 + what to keep first
    missed_pass = false                  # true: one more turn per answer for the facts it left out (MISSED)
    temperature = 0.6                    # the import calls' sampling temperature; unset sends 0. Some models loop
                                         # and write trivia at 0 (GLM-5.3-Flash at 0.6: 1-2 loops in 20 pieces instead of 7, half the lines)
    items = true                         # the default: before a new value replaces one under the same label,
                                         # the model says whether it is another one of the same kind
                                         # (deltos/items.py); decisions in <root>/items.json. false: it replaces

    [llm]                                # any OpenAI-compatible endpoint
    url = "http://127.0.0.1:8000/v1"
    model = "qwen3.8-flash-next"
    key_env = "DELTOS_LLM_KEY"          # the key is read from this variable, never stored here
    thinking = "off"                     # deltos's own calls: "off" (default) | "on" | "server"
    # How this server turns thinking off; unset = probed once and remembered (deltos/llm.py):
    # thinking_off = { chat_template_kwargs = { enable_thinking = false } }
    # thinking_on = { chat_template_kwargs = { enable_thinking = true } }
    # extra = { top_k = 20 }             # sent with every request
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def _path(p: str) -> Path:
    return Path(os.path.expanduser(p))


@dataclass
class Config:
    root: Path
    owner: str = ""
    pages_write: Path | None = None
    pages_read: dict[str, Path] = field(default_factory=dict)
    sessions: dict[str, Path] = field(default_factory=dict)
    import_repos: list[str] = field(default_factory=list)
    import_folders: list[str] = field(default_factory=list)   # [import] folders: "path:label"
    import_skip: list[str] = field(default_factory=list)
    import_workers: int = 2
    import_notes_prompt: str = "v3"
    import_items: bool = True
    import_temperature: float | None = None
    import_missed: bool = False
    agent: str = ""
    hermes: dict = field(default_factory=dict)   # the Hermes adapter's own keys ([hermes])
    llm: dict = field(default_factory=dict)
    learn: dict = field(default_factory=dict)    # chat learning ([learn]: auto, days, max_units)
    recall: dict = field(default_factory=dict)   # reading ([recall]: doubt_days, cues)
    tools: dict = field(default_factory=dict)    # what the agent is given ([tools]: descriptions)

    @classmethod
    def load(cls, root: str | Path | None = None) -> "Config":
        root = _path(str(root or os.environ.get("DELTOS_ROOT") or "~/deltos"))
        f = _path(os.environ["DELTOS_CONFIG"]) if os.environ.get("DELTOS_CONFIG") else root / "deltos.toml"
        d = tomllib.loads(f.read_text()) if f.is_file() else {}
        pages, imp, llm = d.get("pages", {}), d.get("import", {}), d.get("llm", {})
        key = os.environ.get(llm.get("key_env", "DELTOS_LLM_KEY"), "")
        return cls(
            root=root,
            owner=d.get("owner", ""),
            pages_write=_path(pages["write"]) if pages.get("write") else None,
            pages_read={k: _path(v) for k, v in pages.get("read", {}).items()},
            sessions={k: _path(v) for k, v in d.get("sessions", {}).items()},
            import_repos=[os.path.expanduser(r) for r in imp.get("repos", [])],
            import_folders=[os.path.expanduser(f) for f in imp.get("folders", [])],
            import_skip=list(imp.get("skip", [])),
            import_workers=int(imp.get("workers", 2)),
            import_notes_prompt=str(imp.get("notes_prompt", "v3")),
            import_items=bool(imp.get("items", True)),
            import_temperature=float(imp["temperature"]) if "temperature" in imp else None,
            import_missed=bool(imp.get("missed_pass", False)),
            agent=str(d.get("agent", "")),
            hermes=dict(d.get("hermes", {})),
            learn=dict(d.get("learn", {})),
            recall=dict(d.get("recall", {})),
            tools=dict(d.get("tools", {})),
            llm={"url": llm.get("url", ""), "model": llm.get("model", ""), "key": key,
                 **{k: dict(llm[k]) for k in ("extra", "thinking_off", "thinking_on") if llm.get(k)},
                 **({"thinking": str(llm["thinking"])} if llm.get("thinking") else {})},
        )
