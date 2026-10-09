"""Streaming client for an OpenAI-compatible /v1/chat/completions endpoint.

llama.cpp's ``llama-server`` exposes this API, so the assistant only needs
plain HTTP; no inference library is imported into the Python process.
"""

import json
import re
import time

import httpx

THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


class LLMError(RuntimeError):
    """The server answered with an error. ``status`` and ``retry_after`` help a router decide what to do."""

    def __init__(self, message, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class LLMClient:
    def __init__(self, base_url="http://127.0.0.1:8080/v1", model="local", temperature=0.6,
                 max_tokens=2048, extra_body=None, api_key=None, timeout=600.0):
        self.base_url = base_url.rstrip("/")
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
        self.http = httpx.Client(timeout=httpx.Timeout(timeout, connect=8.0), headers=headers)

    def health(self):
        try:
            return self.http.get(f"{self.base_url}/models").status_code in (200, 401, 403)
        except httpx.HTTPError:
            return False

    def chat(self, messages, tools=None, on_token=None):
        """Stream one assistant turn.

        Returns (message_dict, stats). ``on_token`` is called with each text
        chunk as it arrives so the CLI can print it live.
        """
        body = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            **self.extra_body,
        }
        if tools:
            body["tools"] = tools

        content, tool_calls = [], {}
        start, first_token_at, n_chunks = time.time(), None, 0
        usage = None
        with self.http.stream("POST", f"{self.base_url}/chat/completions", json=body) as resp:
            if resp.status_code != 200:
                resp.read()
                retry = resp.headers.get("retry-after")
                raise LLMError(f"LLM server error {resp.status_code}: {resp.text[:500]}", resp.status_code,
                               float(retry) if retry and retry.replace(".", "", 1).isdigit() else None)
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                chunk = json.loads(payload)
                usage = chunk.get("usage") or usage
                if not chunk.get("choices"):
                    continue
                delta = chunk["choices"][0].get("delta", {})
                if delta.get("content"):
                    if first_token_at is None:
                        first_token_at = time.time()
                    n_chunks += 1
                    content.append(delta["content"])
                    if on_token:
                        on_token(delta["content"])
                for tc in delta.get("tool_calls") or []:
                    slot = tool_calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                    slot["id"] = tc.get("id") or slot["id"]
                    fn = tc.get("function") or {}
                    slot["name"] += fn.get("name") or ""
                    slot["arguments"] += fn.get("arguments") or ""

        text = THINK_RE.sub("", "".join(content)).strip()
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
            "tokens": gen_tokens,
            "tokens_per_sec": gen_tokens / gen_time if gen_time > 0 else 0.0,
        }
        return message, stats
