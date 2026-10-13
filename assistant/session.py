"""Context-window and token bookkeeping for jarvis: /context, /usage and /compact.

The server reports exact numbers for every call (prompt_tokens, completion_tokens). Those are the source of truth;
the per-part breakdown in /context is an estimate scaled to match the last exact prompt size.
"""

import json
import os
import re
import time
from datetime import date
from pathlib import Path

import httpx

DEFAULT_CONTEXT = 8192
CHARS_PER_TOKEN = 3.6      # Qwen3 tokenizer on English plus code, measured against Ollama's prompt_tokens


def forge_home():
    """~/.forge, or $FORGE_HOME (the tests point it at a temp folder)."""
    home = Path(os.environ.get("FORGE_HOME") or Path.home() / ".forge")
    home.mkdir(parents=True, exist_ok=True)
    return home


def context_window(url, model, default=DEFAULT_CONTEXT):
    """num_ctx the server really uses. Ollama cuts anything longer silently, so this matters."""
    if ":11434" not in url:
        return default
    root = url.rstrip("/").removesuffix("/v1")
    try:
        info = httpx.post(f"{root}/api/show", json={"model": model}, timeout=5).json()
    except (httpx.HTTPError, ValueError):
        return default
    m = re.search(r"^num_ctx\s+(\d+)", info.get("parameters", ""), re.M)
    if m:
        return int(m.group(1))
    return default  # Ollama's own default is small; the Modelfile sets num_ctx


def estimate_tokens(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return max(1, round(len(text) / CHARS_PER_TOKEN)) if text else 0


def message_tokens(m):
    n = estimate_tokens(m.get("content") or "") + 4      # role markers
    for c in m.get("tool_calls") or []:
        n += estimate_tokens(c["function"]["name"]) + estimate_tokens(c["function"]["arguments"]) + 6
    return n


def breakdown(messages, tools):
    """Estimated tokens per part of the prompt: system prompt, tool definitions, and the conversation by kind."""
    parts = {"system prompt": 0, "tool definitions": estimate_tokens(tools) if tools else 0,
             "your messages": 0, "replies": 0, "tool calls and results": 0}
    for i, m in enumerate(messages):
        n = message_tokens(m)
        if m["role"] == "system" and i == 0:
            parts["system prompt"] += n
        elif m["role"] == "user":
            parts["your messages"] += n
        elif m["role"] == "tool" or m.get("tool_calls"):
            parts["tool calls and results"] += n
        else:
            parts["replies"] += n
    return parts


class Usage:
    """Tokens used this session, plus running totals per day saved in usage.json."""

    def __init__(self, path=None):
        self.path = Path(path) if path else forge_home() / "usage.json"
        self.started = time.time()
        self.calls = self.prompt = self.completion = 0
        self.seconds = 0.0
        self.last_prompt = 0          # size of the most recent prompt: how full the context is right now
        self.peak_prompt = 0
        self.by_backend = {}

    def add(self, stats, seconds=0.0):
        p, c = int(stats.get("prompt_tokens") or 0), int(stats.get("tokens") or 0)
        self.calls += 1
        self.prompt += p
        self.completion += c
        self.seconds += seconds
        if p:
            self.last_prompt = p
            self.peak_prompt = max(self.peak_prompt, p)
        b = self.by_backend.setdefault(stats.get("backend", "local"), [0, 0])
        b[0] += p
        b[1] += c
        self._save(p, c)

    def _save(self, p, c):
        data = self.load()
        day = data.setdefault("days", {}).setdefault(date.today().isoformat(), {"prompt": 0, "completion": 0, "calls": 0})
        day["prompt"] += p
        day["completion"] += c
        day["calls"] += 1
        try:
            self.path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except OSError:
            pass

    def load(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def totals(self):
        days = self.load().get("days", {})
        today = days.get(date.today().isoformat(), {"prompt": 0, "completion": 0, "calls": 0})
        alltime = {k: sum(d.get(k, 0) for d in days.values()) for k in ("prompt", "completion", "calls")}
        return today, alltime, len(days)


COMPACT_PROMPT = (
    "(system note) The conversation is getting long. Write a summary of it that lets you carry on without the full "
    "history. Include: what the user wants overall, decisions made, files created or changed (with paths), commands or "
    "code that worked, important facts and numbers from tool results, errors still open, and what was about to happen "
    "next. Use short bullet points, under 300 words. Do not call tools.{extra}")

SUMMARY_HEADER = "(summary of our earlier conversation, written when it was compacted)\n"


def compact_messages(llm, messages, instructions=""):
    """Ask the model to summarise the conversation and return a short replacement history plus the call's stats."""
    extra = f" Pay special attention to: {instructions}" if instructions else ""
    # long tool outputs are what fills the window; trimming them keeps the summary request itself inside it
    convo = [dict(m, content=m["content"][:600] + " ...") if m["role"] == "tool" and len(m.get("content") or "") > 600 else m
             for m in messages[1:]]
    if not convo:
        return messages, None
    request = messages[:1] + convo + [{"role": "user", "content": COMPACT_PROMPT.format(extra=extra)}]
    reply, stats = llm.chat(request, tools=None)
    summary = (reply.get("content") or "").strip()
    if not summary:
        raise RuntimeError("the model returned an empty summary; nothing was changed")
    return [messages[0], {"role": "user", "content": SUMMARY_HEADER + summary},
            {"role": "assistant", "content": "Got it, I have the summary. What next?"}], stats
