"""Hybrid local + remote inference.

``RoutedLLM`` has the same ``chat``/``health`` interface as ``LLMClient``, so the
agent and the benchmark use it unchanged. The local model (your GPU) is the
default; remote OpenAI-compatible APIs (Groq, OpenRouter, Cerebras, Gemini, ...)
take over when:

* the conversation no longer fits the local context window,
* the local model keeps hitting tool errors (it is flailing),
* the local model returns nothing, or the local server is down,
* you force it with ``force_remote`` (``/remote`` in chat.py).

Remote providers that rate-limit (429) or fail are put on a cool-down and the next
one is tried, so several free tiers behave like one bigger quota.

Modes: ``local`` (never leaves the machine), ``auto`` (above), ``remote`` (remote first,
local as the last resort).
"""

import json
import os
import time
from pathlib import Path

import httpx

from .llm import LLMClient, LLMError, TurnTimeout

DEFAULT_PROVIDERS = "providers.json"


class Backend:
    def __init__(self, name, client, kind):
        self.name, self.client, self.kind = name, client, kind
        self.cooldown_until = 0.0
        self.calls = 0
        self.failures = 0

    @property
    def available(self):
        return time.time() >= self.cooldown_until


def load_env_file(path=".env"):
    """Read KEY=VALUE lines into os.environ (existing variables win). The file is gitignored."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_remotes(path=DEFAULT_PROVIDERS, temperature=0.3):
    """Providers whose API-key environment variable is set. Others are skipped silently."""
    p = Path(path)
    if not p.exists():
        return []
    load_env_file()
    load_env_file(Path(__file__).resolve().parent.parent / ".env")
    backends = []
    for entry in json.loads(p.read_text(encoding="utf-8")):
        key = os.environ.get(entry.get("api_key_env", ""), "")
        if entry.get("disabled") or not key:
            continue
        client = LLMClient(entry["base_url"], entry["model"], temperature=temperature,
                           max_tokens=entry.get("max_tokens", 4096), api_key=key,
                           extra_body=entry.get("extra_body", {}), timeout=entry.get("timeout", 90))
        backends.append(Backend(entry.get("name", entry["model"]), client, "remote"))
    return backends


def estimate_tokens(messages, tools_overhead=900):
    chars = 0
    for m in messages:
        chars += len(m.get("content") or "")
        for tc in m.get("tool_calls") or []:
            chars += len(tc["function"]["arguments"]) + len(tc["function"]["name"])
    return int(chars / 3.2) + tools_overhead


def tool_error_streak(messages):
    """How many tool results since the last user message look like failures."""
    n = 0
    for m in reversed(messages):
        if m["role"] == "user":
            break
        if m["role"] == "tool":
            c = m.get("content") or ""
            if c.startswith("Error") or "[exit code" in c or "[stderr]" in c:
                n += 1
    return n


class RoutedLLM:
    def __init__(self, local_client, remotes=(), mode="local", local_ctx=8192, error_streak=2, log=None):
        self.local = Backend("local", local_client, "local")
        self.remotes = list(remotes)
        self.mode = mode if self.remotes else "local"
        self.local_ctx = local_ctx
        self.error_streak = error_streak
        self.force_remote = False
        self.log = log or (lambda msg: None)
        # attributes the benchmark/CLI read from a plain client
        self.model = local_client.model
        self.temperature = local_client.temperature

    # ------------------------------------------------------------ plumbing
    def health(self):
        if self.mode == "remote":
            return any(r.available for r in self.remotes) or self.local.client.health()
        return self.local.client.health()

    def _order(self, messages):
        if self.mode == "local":
            return [self.local]
        remotes = [r for r in self.remotes if r.available]
        if self.mode == "remote" or self.force_remote:
            return remotes + [self.local]
        too_big = estimate_tokens(messages) > self.local_ctx * 0.75
        flailing = tool_error_streak(messages) >= self.error_streak
        if too_big or flailing:
            self.log("escalating to remote: " + ("context too large for local" if too_big else "local keeps hitting tool errors"))
            return remotes + [self.local]
        return [self.local] + remotes

    def _penalise(self, backend, err):
        status = getattr(err, "status", None)
        if status == 429:
            wait = getattr(err, "retry_after", None) or 60
        elif status in (401, 403):
            wait = 3600  # bad key or no access: stop trying for a while
        elif status == 400:
            wait = 0  # this request was rejected, not the provider
        else:
            wait = 30
        backend.failures += 1
        if backend.kind == "remote" and wait:
            backend.cooldown_until = time.time() + wait
        self.log(f"{backend.name} failed ({type(err).__name__} {status or ''}); cooling {wait:.0f}s" if wait else
                 f"{backend.name} rejected the request ({err})")

    # ---------------------------------------------------------------- chat
    def chat(self, messages, tools=None, on_token=None, deadline=None):
        last_err = None
        order = self._order(messages)
        for i, b in enumerate(order):
            if b.kind == "remote" and not b.available:
                continue
            try:
                message, stats = b.client.chat(messages, tools=tools, on_token=on_token, deadline=deadline)
            except TurnTimeout:
                raise
            except (LLMError, httpx.HTTPError, ConnectionError, json.JSONDecodeError) as e:
                self._penalise(b, e)
                last_err = e
                continue
            empty = not message.get("content") and not message.get("tool_calls")
            if empty and b is self.local and i + 1 < len(order):
                self.log("local returned an empty reply; trying remote")
                continue
            b.calls += 1
            stats["backend"] = b.name
            return message, stats
        raise last_err or RuntimeError("no backend produced a reply")

    def status(self):
        rows = [f"{self.local.name}: {self.local.calls} calls"]
        for r in self.remotes:
            cool = max(0, r.cooldown_until - time.time())
            rows.append(f"{r.name}: {r.calls} calls, {r.failures} failures" + (f", cooling {cool:.0f}s" if cool else ""))
        return f"mode={self.mode}" + (" (forced remote)" if self.force_remote else "") + " | " + " | ".join(rows)


def make_llm(url, model, temperature, mode="local", providers=DEFAULT_PROVIDERS, log=None, think=False):
    """Build a plain LLMClient for local-only use, or a RoutedLLM when remotes are requested."""
    if think:  # let Qwen3 reason before answering: far more tokens, so allow a long reply
        local = LLMClient(url, model, temperature=temperature, max_tokens=8192,
                          extra_body={"chat_template_kwargs": {"enable_thinking": True}})
    else:
        local = LLMClient(url, model, temperature=temperature)
    if mode == "local":
        return local
    remotes = load_remotes(providers, temperature=temperature)
    if not remotes:
        (log or print)(f"No remote providers usable (create {providers} and set the API key env vars); staying local.")
        return local
    return RoutedLLM(local, remotes, mode=mode, log=log)
