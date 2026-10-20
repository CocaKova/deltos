# Deltos in Claude Code

Two parts, both plain commands (install Deltos first: `pip install -e .` at the
repo root gives the `deltos` command).

**Tools (MCP):**

    claude mcp add deltos -- deltos --agent Claude serve

`--agent` is recorded as the source of facts written through Claude Code; leave
it out and facts have no source name.

**The memory block on every prompt (hook).** In `~/.claude/settings.json` (or a
project's `.claude/settings.json`):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "deltos hook claude-code", "timeout": 10 } ] }
    ]
  }
}
```

The hook reads the prompt from stdin and returns the memory block as
`additionalContext`, headed `## Memory (deltos)`. It uses no model and takes
well under a second. It never blocks a prompt: on any error it returns nothing
and writes the error to stderr.

Memory is `$DELTOS_ROOT` or `~/deltos`; settings in `deltos.toml` there (see the
root README's Configuration section). To test the hook by hand:

    echo '{"prompt": "where does the nightly backup run?"}' | deltos hook claude-code
