"""Write or not: does the agent `remember` news when a message brings it, and keep
quiet when it doesn't? 40 messages in one small world (20 with news, 20 without),
the news cases modelled on real moments from an agent's chats (a site's version
drift fixed and shipped, a tool installed, the brain swapped, an app released,
a service moved) with every name made up, and not a dictionary word: recall
counts a one-word plain-English name ("Courier") as named only mid-sentence. The agent gets deltos's instructions,
the memory block and all of deltos's tools and acts for up to MAX_ROUNDS rounds;
the outcome is read from the store afterwards (what changed), so it doesn't
matter which calls got it there.

  python3 -m bench.writeornot [--arms baseline,when,cues,both] [--cases 0,5] [--out DIR]

Arms (10-22): baseline = plain tool descriptions, no cues; when = [tools]
descriptions = "when"; cues = [recall] cues = true (deltos/cues.py); both.
Scores per arm: write recall (news cases whose news landed on the right thing),
false writes (no-news cases that changed memory), wrong writes (entries changed
to something the message didn't say). The model is deltos.toml's [llm] or
DELTOS_LLM_URL / DELTOS_LLM_MODEL.
"""
from __future__ import annotations

import argparse
import datetime
import json
import tempfile
import time
from collections import Counter
from pathlib import Path

import deltos.llm as llm
from deltos import Memory
from deltos.config import Config
from deltos.store import Store

MAX_ROUNDS = 4
TODAY = "2026-10-22"
OWNER = "Sam"
SYSTEM = (f"You are the assistant for {OWNER}'s home lab. Keep memory up to date as you work, then reply "
          "briefly.")
# (thing, kind, [(label, value)]), all true since 2026-09-01
WORLD = [
    ("Lyrin", "app", [("version", "1.1.0")]),
    ("Ferrox", "app", [("version", "1.1.2")]),
    ("Folio site", "website", [("host", "vm-5"), ("url", "https://folio.example.net"), ("status", "live")]),
    ("Brain", "model", [("model", "DeepSeek-V4.1-Flash"), ("host", "Box-3"), ("port", "8888")]),
    ("Kurio", "app", [("latest release", "2.19.1"), ("build host", "Box-3")]),
    ("Reverse kit", "tool", [("status", "not installed")]),
    ("Backup job", "service", [("host", "nas"), ("schedule", "daily 06:00")]),
    ("Nextcloud", "service", [("version", "28.0.4"), ("host", "Box-7")]),
    ("Gateway", "service", [("port", "8787")]),
    ("Box-7", "machine", [("ip", "192.168.1.20")]),
    ("Inbox scan", "job", [("schedule", "daily 06:00")]),
    ("Dashboard", "app", [("url", "http://box-7:5173")]),
    ("Media server", "service", [("status", "running")]),
    ("Ledgra", "app", [("version", "0.9.0")]),
    ("Mail relay", "service", [("vendor", "SendLoop")]),
    ("Embed server", "service", [("port", "8889")]),
    ("Notes app", "app", [("host", "Box-7")]),
    ("Memory service", "service", [("status", "testing")]),
    ("Stream plugin", "plugin", [("version", "0.5.0")]),
]
# (thing whose news it is or "", words of the new value: any one of "a|b", message)
CASES = [
    ("Lyrin", "1.2.1", "Fixed the version drift on the Folio site: the Lyrin card now says v1.2.1, same as the release."),
    ("Ferrox", "1.2.1", "The Ferrox detail page is on v1.2.1 now too."),
    ("Brain", "glm", "The Brain is GLM-5.3-Flash now; DeepSeek is gone."),
    ("Kurio", "2.20.0", "Released Kurio 2.20.0 this afternoon."),
    ("Reverse kit", "install|live|wired|working|mcp", "Installed the Reverse kit CLI and wired it up as an MCP "
                                                       "server, it works."),
    ("Backup job", "atlas", "Moved the backup job over to the new server, atlas."),
    ("Nextcloud", "29.0.1", "Updated Nextcloud to 29.0.1 this morning."),
    ("Gateway", "9119", "The Gateway listens on 9119 now, not 8787."),
    ("Box-7", "192.168.1.44", "Box-7 got a new address after the router swap: 192.168.1.44."),
    ("Inbox scan", "01:00|1:00|1am|1 am", "I moved the inbox scan to 01:00 every night."),
    ("Dashboard", "dash.example.net", "The Dashboard lives at https://dash.example.net now."),
    ("Media server", "stop|retire|off|down|shut|decommission", "We shut the media server down for good last night."),
    ("Ledgra", "1.0", "Shipped Ledgra 1.0 to the store today!"),
    ("Mail relay", "postrelay", "We switched the mail relay from SendLoop to PostRelay."),
    ("Embed server", "8891", "The embed server moved to port 8891 after the clash."),
    ("Notes app", "box-9", "The notes app runs on Box-9 now."),
    ("Kurio", "vm-12", "Kurio builds run on vm-12 now, not Box-3."),
    ("Memory service", "live", "The memory service went live for the agent today."),
    ("Stream plugin", "0.6.1", "Deployed stream plugin 0.6.1 to the Gateway."),
    ("Folio site", "self-host|self host|selfhost", "Decided: the Folio site self-hosts its fonts from now on."),
    ("", "", "What version is Nextcloud on?"),
    ("", "", "Is the Brain still DeepSeek?"),
    ("", "", "Restart the backup job on nas."),
    ("", "", "Thanks, that worked!"),
    ("", "", "Write a haiku about servers."),
    ("", "", "Nextcloud is on 28.0.4, right?"),
    ("", "", "The Lyrin card says v1.1.0, which matches the release."),
    ("", "", "Check whether Box-7 still answers on 192.168.1.20."),
    ("", "", "Remind me what port the Gateway uses."),
    ("", "", "Kurio 2.19.1 crashed once yesterday but it's fine now."),
    ("", "", "Draft release notes for Ledgra 0.9.0."),
    ("", "", "How long has the media server been running?"),
    ("", "", "The backup job finished in 4 minutes tonight."),
    ("", "", "Can you move the backup job to atlas tomorrow?"),
    ("", "", "I'm thinking about switching the mail relay to PostRelay next month."),
    ("", "", "Summarize what we know about the embed server."),
    ("", "", "Box-7 is a little slow today."),
    ("", "", "Open the Dashboard and tell me if the graphs load."),
    ("", "", "The Gateway still listens on 8787."),
    ("", "", "Did we ever deploy stream plugin 0.6.1?"),
]
ARMS = {"baseline": ("plain", False), "when": ("when", False), "cues": ("plain", True), "both": ("when", True)}


def world(root: Path) -> None:
    s = Store(root, today="2026-09-01")
    s.name(OWNER, [], "person")
    for thing, kind, entries in WORLD:
        s.name(thing, [], kind)
        for label, value in entries:
            s.remember(thing, label, value, since="2026-09-01")


def state(root: Path) -> dict[tuple[str, str], str]:
    """Every current entry: (thing, label) -> value (also-current items included)."""
    s = Store(root, today=TODAY)
    out = {}
    for sl in set(s.by_name.values()):
        t = s.find(s.label(sl).split(" (")[0])
        if t:
            for k, a in t.attrs.items():
                out[(t.name.lower(), k)] = " | ".join([a.value] + [x.value for x in a.also]).lower()
    return out


def _has(value: str, words: str) -> bool:
    return any(w and w in value for w in words.lower().split("|"))


def episode(case: tuple, arm: str) -> dict:
    thing, words, msg = case
    descriptions, cues = ARMS[arm]
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        world(root)
        before = state(root)
        m = Memory(config=Config(root=root, owner=OWNER, recall={"cues": cues}, tools={"descriptions": descriptions}),
                   agent="assistant", today=TODAY)
        block = m.context(msg)
        msgs = [{"role": "system", "content": f"{SYSTEM}\n\nToday is {TODAY}.\n\n{m.instructions()}\n\n"
                 + (f"## Memory\n{block}" if block else "")},
                {"role": "user", "content": msg}]
        calls, err = [], ""
        for _ in range(MAX_ROUNDS):
            try:
                reply, _u = llm.chat(msgs, tools=[{"type": "function", "function": t} for t in m.tools],
                                     max_tokens=8192)
            except Exception as e:   # a brain failure is reported, not scored as a choice
                err = f"{type(e).__name__}: {e}"[:200]
                break
            tc = reply.get("tool_calls") or []
            if not tc:
                break
            msgs.append({"role": "assistant", "content": reply.get("content") or "", "tool_calls": tc})
            for c in tc:
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                    out = m.call(c["function"]["name"], args)
                except Exception as e:
                    args, out = {}, f"error: {e}"
                calls.append(f'{c["function"]["name"]}({json.dumps(args)[:120]})')
                msgs.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": str(out)[:4000]})
        after = state(root)
    changed = {k: v for k, v in after.items() if before.get(k) != v}
    hit = bool(thing) and any(t == thing.lower() and _has(v, words) for (t, _), v in changed.items())
    news_words = words if thing else ""
    wrong = [f"{t} {lab}={v}" for (t, lab), v in changed.items()
             if not (thing and t == thing.lower() and _has(v, news_words))
             and not any(w in msg.lower() for w in v.replace("|", " ").split() if len(w) > 2)]
    return {"arm": arm, "thing": thing, "msg": msg, "cue": [ln for ln in block.splitlines()
                                                             if ln.startswith(("This message may change",
                                                                               "Lines marked"))],
            "hit": hit, "wrote": bool(changed), "changed": [f"{t} {lab}={v}" for (t, lab), v in changed.items()],
            "wrong": wrong, "calls": calls, "error": err}


def report(rows: list[dict], model: str) -> str:
    lines = [f"write-or-not · {model} · {datetime.datetime.now():%Y-%m-%d %H:%M} · {len(rows)} episodes", ""]
    for arm in ARMS:
        r = [x for x in rows if x["arm"] == arm]
        if not r:
            continue
        news = [x for x in r if x["thing"]]
        quiet = [x for x in r if not x["thing"]]
        errs = sum(1 for x in r if x["error"])
        lines.append(f"{arm:>8}: write recall {sum(x['hit'] for x in news)}/{len(news)}  "
                     f"false writes {sum(x['wrote'] for x in quiet)}/{len(quiet)}  "
                     f"wrong writes {sum(len(x['wrong']) for x in r)}  "
                     f"cued {sum(bool(x['cue']) for x in r)}  brain errors {errs}")
    lines.append("")
    by = {}
    for x in rows:
        by.setdefault(x["msg"], {})[x["arm"]] = x
    for msg, arms in by.items():
        marks = " ".join(f"{a}={'H' if v['hit'] else ('W' if v['wrote'] else '-')}{'!' if v['error'] else ''}"
                         for a, v in arms.items())
        lines.append(f"{'news ' if next(iter(arms.values()))['thing'] else 'quiet'} {marks}  {msg[:70]}")
    lines.append("\nH = news landed, W = memory changed (on a quiet case: a false write), - = no write, ! = brain error")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--cases", default="", help="comma-separated case numbers (0-based); default all")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    cfg = Config.load()
    if cfg.llm.get("url") and not llm.ENDPOINT.get("model"):
        llm.configure(**{k: v for k, v in cfg.llm.items() if v})
    model = llm.ENDPOINT["model"]
    cases = [CASES[int(i)] for i in a.cases.split(",")] if a.cases else CASES
    out = Path(a.out or f"out/write-or-not/{datetime.date.today()}")
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    with open(out / "rows.jsonl", "w") as fh:
        for arm in a.arms.split(","):
            for case in cases:
                t = time.time()
                row = episode(case, arm)
                row["s"] = round(time.time() - t, 1)
                rows.append(row)
                fh.write(json.dumps(row) + "\n")
                fh.flush()
                print(f"{arm:>8} {'H' if row['hit'] else ('W' if row['wrote'] else '-')} {row['s']:6.1f}s "
                      f"{case[2][:60]:60} {'; '.join(row['calls'])[:100]} {row['error']}", flush=True)
    (out / "report.txt").write_text(report(rows, model))
    print(report(rows, model))


if __name__ == "__main__":
    main()
