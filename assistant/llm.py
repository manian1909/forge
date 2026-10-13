"""Streaming client for an OpenAI-compatible /v1/chat/completions endpoint.

llama.cpp's ``llama-server`` exposes this API, so the assistant only needs
plain HTTP; no inference library is imported into the Python process.
"""

import json
import re
import time

import httpx

from . import toolparse

THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


class TurnTimeout(TimeoutError):
    """The wall-clock budget for this turn ran out (checked on every streamed chunk, so it cannot hang)."""


class LLMError(RuntimeError):
    """The server answered with an error. ``status`` and ``retry_after`` help a router decide what to do."""

    def __init__(self, message, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class LLMClient:
    def __init__(self, base_url="http://127.0.0.1:8080/v1", model="local", temperature=0.6,
                 max_tokens=3072, extra_body=None, api_key=None, timeout=600.0, own_tools=False):
        self.base_url = base_url.rstrip("/")
        # own_tools: tool definitions go in the system prompt and the calls are read from the reply text here, because the
        # server's parser throws away a whole call over one badly escaped quote (see toolparse.py). Local models only.
        self.own_tools = own_tools
        self.on_chunk = None       # called for every streamed line; the governor uses it to stop a reply mid-way
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        # llama-server reads chat_template_kwargs; disabling Qwen3's thinking
        # mode cuts latency a lot for everyday requests
        # (llama-server reads chat_template_kwargs, Ollama reads reasoning_effort)
        self.extra_body = extra_body if extra_body is not None else {
            "chat_template_kwargs": {"enable_thinking": False},
            "reasoning_effort": "none",
        }
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.http = httpx.Client(timeout=httpx.Timeout(timeout, connect=8.0), headers=headers, follow_redirects=True)

    def health(self):
        try:
            return self.http.get(f"{self.base_url}/models").status_code in (200, 401, 403)
        except httpx.HTTPError:
            return False

    def chat(self, messages, tools=None, on_token=None, deadline=None):
        """Stream one assistant turn.

        Returns (message_dict, stats). ``on_token`` is called with each text
        chunk as it arrives so the CLI can print it live.
        """
        own = self.own_tools and bool(tools)
        if own:
            first = messages[0]
            sent = [dict(first, content=first["content"] + toolparse.tools_block(tools)) if first["role"] == "system"
                    else {"role": "system", "content": toolparse.tools_block(tools).strip()}] + \
                   (messages[1:] if first["role"] == "system" else messages)
        else:
            sent = messages
        muffle = toolparse.Muffler(on_token) if own else None
        body = {
            "model": self.model,
            "messages": sent,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            **self.extra_body,
        }
        if tools and not own:
            body["tools"] = tools

        content, tool_calls, finish = [], {}, None
        start, first_token_at, n_chunks = time.time(), None, 0
        usage = None
        with self.http.stream("POST", f"{self.base_url}/chat/completions", json=body) as resp:
            if resp.status_code != 200:
                resp.read()
                retry = resp.headers.get("retry-after")
                raise LLMError(f"LLM server error {resp.status_code}: {resp.text[:500]}", resp.status_code,
                               float(retry) if retry and retry.replace(".", "", 1).isdigit() else None)
            for line in resp.iter_lines():
                if deadline and time.time() > deadline:
                    raise TurnTimeout()
                if self.on_chunk:
                    self.on_chunk()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                chunk = json.loads(payload)
                usage = chunk.get("usage") or usage
                if not chunk.get("choices"):
                    continue
                finish = chunk["choices"][0].get("finish_reason") or finish
                delta = chunk["choices"][0].get("delta", {})
                if delta.get("content"):
                    if first_token_at is None:
                        first_token_at = time.time()
                    n_chunks += 1
                    content.append(delta["content"])
                    if muffle:
                        muffle.feed(delta["content"])
                    elif on_token:
                        on_token(delta["content"])
                for tc in delta.get("tool_calls") or []:
                    slot = tool_calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                    slot["id"] = tc.get("id") or slot["id"]
                    fn = tc.get("function") or {}
                    slot["name"] += fn.get("name") or ""
                    slot["arguments"] += fn.get("arguments") or ""

        if muffle:
            muffle.flush()
        text = THINK_RE.sub("", "".join(content)).strip()
        if own:
            text, parsed = toolparse.split_calls(text, tools)
            if finish == "length":
                parsed = []              # cut off at the token cap: a half-written call must not run as if it were whole
            for i, c in enumerate(parsed):
                tool_calls[i] = {"id": f"call_{i}", "name": c["name"], "arguments": json.dumps(c["arguments"], ensure_ascii=False)}
        message = {"role": "assistant", "content": text}
        if tool_calls:
            message["tool_calls"] = [
                {
                    "id": slot["id"] or f"call_{i}",
                    "type": "function",
                    "function": {"name": slot["name"], "arguments": slot["arguments"] or "{}"},
                }
                for i, slot in sorted(tool_calls.items())
            ]
        elapsed = time.time() - start
        gen_tokens = (usage or {}).get("completion_tokens", n_chunks)
        # some servers send a tool call in one final chunk, so fall back to wall time
        gen_time = elapsed - (first_token_at - start) if first_token_at and n_chunks > 1 else elapsed
        stats = {
            "time_to_first_token": (first_token_at or time.time()) - start,
            "finish": finish,                      # "length" means the reply hit max_tokens and was cut off
            "prompt_tokens": (usage or {}).get("prompt_tokens", 0),   # what the whole conversation costs to send
            "tokens": gen_tokens,
            "tokens_per_sec": gen_tokens / gen_time if gen_time > 0 else 0.0,
        }
        return message, stats
