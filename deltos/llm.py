"""The model the importer writes with: any OpenAI-compatible endpoint (stdlib only).

Set by deltos.toml [llm] or the environment: DELTOS_LLM_URL, DELTOS_LLM_MODEL,
DELTOS_LLM_KEY (the benchmark's MTB_BASE_URL / MTB_MODEL / MTB_API_KEY still work).

deltos asks for plain answers: its own calls (import, repair, items check) run
with the model's thinking OFF (measured 10-14: the same answers, ~100x faster).
Servers disagree on how to say that, so it is a posture, not a request field:
  thinking      "off" (default) | "on" | "server" (send nothing, the server decides)
  thinking_off  the fields that turn it off on this server, if known; unset, the
                first request probes the known styles (THINKING_OFF_STYLES) for one
                that answers with 0 reasoning tokens, and remembers it per
                url + model (~/.cache/deltos/llm-thinking.json)
  thinking_on   the fields that turn it on (DELTOS_SLOTS_THINK=1)
  extra         fields sent with every request
When no style turns thinking off, deltos says so once (stderr), sends nothing,
and the importer budgets for the thought and treats an answer lost to it as a
cut-off. Env: DELTOS_LLM_THINKING, and JSON in DELTOS_LLM_THINKING_OFF,
DELTOS_LLM_THINKING_ON, DELTOS_LLM_EXTRA.

ask() is the one call every model request goes through: it detects a degenerate
answer and recovers the same way for every model, instead of a sampling knob per
model. An answer that loops is stopped as it streams (the repeat guard), an answer
that loops or isn't in the caller's format is asked again one rung hotter on a
fixed ladder, and a cut-off answer goes back to the caller, who knows how to split
its input.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ENDPOINT = {
    "url": os.environ.get("DELTOS_LLM_URL") or os.environ.get("MTB_BASE_URL", "http://127.0.0.1:8000/v1"),
    "model": os.environ.get("DELTOS_LLM_MODEL") or os.environ.get("MTB_MODEL", ""),
    "key": os.environ.get("DELTOS_LLM_KEY") or os.environ.get("MTB_API_KEY", "none"),
    "extra": json.loads(os.environ.get("DELTOS_LLM_EXTRA") or "{}"),
    "thinking_off": json.loads(os.environ.get("DELTOS_LLM_THINKING_OFF") or "{}"),
    "thinking_on": json.loads(os.environ.get("DELTOS_LLM_THINKING_ON") or "{}"),
    "thinking": os.environ.get("DELTOS_LLM_THINKING", "off"),
}

# Ways servers take "no thinking", tried in order (10-14: the first works on vLLM
# Qwen3.8-Flash-Next and on GLM-5.3 TensorFold; the other two also on TensorFold).
THINKING_OFF_STYLES = [{"chat_template_kwargs": {"enable_thinking": False}},
                       {"chat_template_kwargs": {"thinking": False}},
                       {"reasoning_effort": "none"}]
THINKING_ON_STYLE = {"chat_template_kwargs": {"enable_thinking": True}}
PROBE_CACHE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "deltos" / "llm-thinking.json"
_probe_lock = threading.Lock()
_probed: dict[str, dict | None] = {}   # url|model -> the style that turns thinking off (None: none does)


def configure(url: str = "", model: str = "", key: str = "", extra: dict | None = None,
              thinking_off: dict | None = None, thinking_on: dict | None = None, thinking: str = "") -> None:
    for k, v in (("url", url), ("model", model), ("key", key), ("extra", extra),
                 ("thinking_off", thinking_off), ("thinking_on", thinking_on), ("thinking", thinking)):
        if v:
            ENDPOINT[k] = v


def _reasoned(msg: dict, usage: dict) -> int:
    """Reasoning tokens the answer spent (-1: the server doesn't say)."""
    n = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    if n is not None:
        return n
    if msg.get("reasoning_content") or msg.get("reasoning") or "<think>" in (msg.get("content") or ""):
        return 1
    return -1


def _off_style() -> dict | None:
    """The style that turns thinking off on this url + model: probed once
    (one tiny request per style until one answers with no reasoning), then
    remembered on disk. None: no known style does it."""
    key = f"{ENDPOINT['url'].rstrip('/')}|{ENDPOINT['model']}"
    with _probe_lock:
        if key in _probed:
            return _probed[key]
        try:
            saved = json.loads(PROBE_CACHE.read_text())
        except (OSError, ValueError):
            saved = {}
        if key in saved:
            _probed[key] = saved[key]
            return saved[key]
        found, unsure = None, False
        for style in THINKING_OFF_STYLES:
            try:
                msg, usage = chat([{"role": "user", "content": "Reply with the single word: ok"}],
                                  max_tokens=64, timeout=120, extra=style, _retries=1)
            except urllib.error.HTTPError:
                continue          # the server refuses this style (Mistral templates 400 on chat_template_kwargs)
            except Exception:
                unsure = True     # the server is down or slow: decide later, don't remember anything
                break
            n = _reasoned(msg, usage)
            if n <= 0:            # 0 reasoning tokens, or the server doesn't report them and shows none
                found = style
                break
        if unsure:
            return THINKING_OFF_STYLES[0]
        _probed[key] = found
        if found is None:
            print(f"deltos: {key} kept thinking with every known way to turn it off; requests go out "
                  "without one, budgeted for the thought (set [llm] thinking_off if this server has its own)",
                  file=sys.stderr, flush=True)
        try:
            saved[key] = found
            PROBE_CACHE.parent.mkdir(parents=True, exist_ok=True)
            tmp = PROBE_CACHE.with_suffix(".tmp")
            tmp.write_text(json.dumps(saved, indent=1))
            tmp.replace(PROBE_CACHE)
        except OSError:
            pass
        return found


def thinking(on: bool) -> dict:
    """The request fields for a call that wants the model's reasoning on or off
    ({} = say nothing). Configured fields win; "server" says nothing; otherwise
    "off" is probed (see _off_style) and "on" uses the common chat-template switch."""
    posture = ENDPOINT.get("thinking", "off")
    if on or posture == "on":
        return dict(ENDPOINT["thinking_on"] or ({} if posture == "server" else THINKING_ON_STYLE))
    if ENDPOINT["thinking_off"]:
        return dict(ENDPOINT["thinking_off"])
    if posture == "server":
        return {}
    return dict(_off_style() or {})


def thinking_stays_on() -> bool:
    """True when deltos wants thinking off and this server can't do it: budget for it."""
    return (ENDPOINT.get("thinking", "off") == "off" and not ENDPOINT["thinking_off"]
            and f"{ENDPOINT['url'].rstrip('/')}|{ENDPOINT['model']}" in _probed
            and _probed[f"{ENDPOINT['url'].rstrip('/')}|{ENDPOINT['model']}"] is None)


def cut_off(msg: dict, usage: dict, max_tokens: int) -> str:
    """Why an answer stopped before it was done, or "" when it finished:
    "length" (the server says it hit max_tokens, or the tokens say so) or
    "thinking" (no answer after reasoning: the budget went to the thought) or
    "repeat" (deltos stopped it: the answer was saying the same lines again)."""
    if usage.get("finish_reason") == "repeat":
        return "repeat"
    if usage.get("finish_reason") == "length" or usage.get("completion_tokens", 0) >= max_tokens:
        return "length"
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or usage.get("reasoning_tokens") or 0
    if reasoning and not (msg.get("content") or "").strip():
        return "thinking"
    return ""


def chat(messages, tools=None, max_tokens=8192, timeout=None, extra=None, _retries=3, stop=None):
    """(message, usage); usage also carries the choice's finish_reason. The read
    timeout grows with the budget: a non-streamed answer arrives all at once,
    and a long thought on a busy server takes a while.
    stop: called with the answer so far each time a line of it ends; a reason it
    returns (e.g. "repeat") ends the answer there: the answer is streamed, the
    connection closed (the server stops writing), and the reason is the
    finish_reason. Every OpenAI-compatible server streams."""
    timeout = timeout or max(600, 60 + max_tokens // 10)
    body = {"model": ENDPOINT["model"], "messages": messages, "temperature": 0, "max_tokens": max_tokens,
            **ENDPOINT["extra"], **(extra or {})}
    body = {k: v for k, v in body.items() if v is not None}   # a None field isn't sent: the server decides
    if tools:
        body["tools"] = tools
    if stop is not None:
        body.update(stream=True, stream_options={"include_usage": True})

    def request(b):
        return urllib.request.Request(f"{ENDPOINT['url'].rstrip('/')}/chat/completions", data=json.dumps(b).encode(),
                                      headers={"Content-Type": "application/json",
                                               "Authorization": f"Bearer {ENDPOINT['key']}"})
    for attempt in range(_retries):
        try:
            if stop is not None:
                try:
                    return _streamed(request(body), timeout, stop)
                except urllib.error.HTTPError as e:
                    if e.code != 400 or "stream_options" not in body:
                        raise
                    body.pop("stream_options")   # a server that refuses it: no usage at the end, counted instead
                    return _streamed(request(body), timeout, stop)
            with urllib.request.urlopen(request(body), timeout=timeout) as r:
                data = json.load(r)
            usage = dict(data.get("usage") or {})
            usage["finish_reason"] = data["choices"][0].get("finish_reason")
            return data["choices"][0]["message"], usage
        except Exception:
            if attempt == _retries - 1:
                raise
            time.sleep(3 * (attempt + 1))


def _streamed(req, timeout, stop) -> tuple[dict, dict]:
    """A streamed answer (server-sent events), with `stop` asked at each line end.
    Without the server's usage (stopped early, or no include_usage), the token
    counts are the streamed pieces, about one token each."""
    parts, think, usage, finish = [], 0, {}, None
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for line in r:
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                break
            ev = json.loads(data)
            if ev.get("usage"):
                usage = dict(ev["usage"])
            for ch in ev.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("reasoning_content") or d.get("reasoning"):
                    think += 1
                if d.get("content"):
                    parts.append(d["content"])
                    if "\n" in d["content"]:
                        why = stop("".join(parts))
                        if why:   # leaving the with-block closes the connection
                            return {"content": "".join(parts)}, {"finish_reason": why, "completion_tokens": len(parts) + think,
                                                                 **({"reasoning_tokens": think} if think else {})}
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
    if not usage:
        usage = {"completion_tokens": len(parts) + think, **({"reasoning_tokens": think} if think else {})}
    usage["finish_reason"] = finish
    return {"content": "".join(parts)}, usage


# The repeat guard: an answer is streamed and stopped once one line has been said REPEAT_STOP times,
# instead of running on to the token cap (GLM at temperature 0, 10-19: 7 of 20 test pieces looped to
# 8,192 tokens, minutes each). Of 1,259 logged import answers (Flash-Next, GLM, DSV), 146 looped
# (a line 10+ times); of the rest, 88 said a line 4+ times, all cycling blocks of the same lines.
REPEAT_STOP = 4
# The temperatures an answer that loops (or isn't in the caller's format) is asked at, one rung
# hotter each time: greedy, midway, and the sampling most models are trained at. Fixed and evenly
# spaced, fit to no model; a configured temperature only says where the climb starts.
LADDER = (0.0, 0.5, 1.0)
THINK = re.compile(r"(?s)<think>.*?</think>")


def repeating(text: str) -> str:
    """The stop check while an answer streams in: "repeat" once one finished line
    has been said REPEAT_STOP times."""
    done = text.rpartition("\n")[0]
    n = Counter(ln.strip() for ln in done.splitlines() if ln.strip())
    return "repeat" if n and max(n.values()) >= REPEAT_STOP else ""


def unloop(text: str) -> tuple[str, int, list[str]]:
    """An answer the repeat guard stopped, without its loop: (the finished lines
    before the stop less every line said REPEAT_STOP - 1 or more times, the most
    one line was said, the lines left out). The loop cycles through several lines;
    a loop's lines were junk 16 of 16 times on 10-19."""
    done = [ln for ln in (text or "").rpartition("\n")[0].splitlines() if ln.strip()]
    n = Counter(ln.strip() for ln in done)
    body = {ln for ln, k in n.items() if k >= REPEAT_STOP - 1}
    return "\n".join(ln for ln in done if ln.strip() not in body), max(n.values(), default=0), sorted(body)


def rungs(first: float | None = None) -> list[float]:
    """The temperatures ask() climbs: the first one, then every hotter rung of the
    ladder (None: 0; a configured 0.6 gives [0.6, 1.0])."""
    first = 0.0 if first is None else first
    return [first] + [r for r in LADDER if r > first]


@dataclass
class Answer:
    """What ask() got back. text is the last try's usable text (no thought, no
    loop). cut: "" (finished), "length" or "thinking" (cut off: the caller splits
    its input), "repeat" or "format" (still looping, or still not in the caller's
    format, on the last rung). tries logs every try: {t, finish, out_tokens,
    reasoning, loop, loop_body}; the token counts add up every try."""
    text: str = ""
    cut: str = ""
    rung: int = 0
    temperature: float | None = None
    tries: list[dict] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    recovered: bool = False   # an earlier try looped or missed the format, and this one finished


def ask(messages, *, max_tokens: int, valid=None, first: float | None = None, extra: dict | None = None,
        transport=None, guard: bool = True) -> Answer:
    """One model request, detected and recovered the same way for every model.
    Each rung (rungs(first)) asks with the repeat guard on; an answer it stopped
    keeps its lines before the loop. An answer that looped, or that says something
    valid(text) rejects, is asked again one rung hotter; an empty answer is not a
    format failure (a caller may allow "nothing"). A cut-off answer ("length",
    "thinking") returns at once: only the caller knows how to split its input;
    one whose text valid() rejects (a whole budget of something else) climbs like
    any format failure. The rung's temperature is sent last, over any in [llm]
    extra. Request errors propagate: the caller owns the outage retries.
    transport: chat() or a stand-in with its signature."""
    ans = Answer()
    for i, t in enumerate(rungs(first)):
        msg, usage = (transport or chat)(messages, max_tokens=max_tokens, extra={**(extra or {}), "temperature": t},
                                         stop=repeating if guard else None)
        usage = usage or {}
        content = THINK.sub("", msg.get("content") or "")
        cut = cut_off(msg, usage, max_tokens)
        reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or usage.get("reasoning_tokens") or 0
        loop, body = 0, []
        if usage.get("finish_reason") == "repeat":
            content, loop, body = unloop(content)
        ans.tries.append({"t": t, "finish": usage.get("finish_reason"), "out_tokens": usage.get("completion_tokens") or 0,
                          "reasoning": reasoning, "loop": loop, "loop_body": body})
        ans.prompt_tokens += usage.get("prompt_tokens") or 0
        ans.completion_tokens += usage.get("completion_tokens") or 0
        ans.reasoning_tokens += reasoning
        ans.text, ans.rung, ans.temperature = content, i, t
        if cut == "repeat" or (valid and content.strip() and not valid(content)):
            ans.cut = "repeat" if cut == "repeat" else "format"   # the answer if this was the last rung
            continue
        ans.cut, ans.recovered = cut, i > 0 and not cut
        return ans
    return ans
