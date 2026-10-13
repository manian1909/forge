"""Read tool calls out of the model's own text, forgivingly.

Qwen writes a call as ``<tool_call>{"name": ..., "arguments": {...}}</tool_call>``. Long code inside that JSON is easy to
get slightly wrong (one unescaped quote), and Ollama's parser then drops the whole call and returns an empty reply. Here
a call that is not valid JSON is still recovered: string arguments are cut out between their keys and unescaped by hand.
"""

import json
import re

OPEN, CLOSE = "<tool_call>", "</tool_call>"


def tools_block(tools):
    """The same text Qwen's chat template adds to the system prompt when tools are passed to it."""
    sigs = "\n".join(json.dumps({"type": "function", "function": t["function"]}, ensure_ascii=False) for t in tools)
    return ("\n\n# Tools\n\nYou may call one or more functions to assist with the user query.\n\n"
            "You are provided with function signatures within <tools></tools> XML tags:\n<tools>\n" + sigs + "\n</tools>\n\n"
            "For each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:\n"
            "<tool_call>\n{\"name\": <function-name>, \"arguments\": <args-json-object>}\n</tool_call>")


def _unescape(raw):
    out, i = [], 0
    table = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/", "'": "'"}
    while i < len(raw):
        c = raw[i]
        if c == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            if nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", raw[i + 2:i + 6]):
                out.append(chr(int(raw[i + 2:i + 6], 16)))
                i += 6
                continue
            out.append(table.get(nxt, "\\" + nxt))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _lenient_arguments(body, schema):
    """Pull string-valued arguments out of text that is not valid JSON."""
    props = (schema or {}).get("properties", {})
    args_at = re.search(r'"arguments"\s*:\s*\{', body)
    tail = body[args_at.end():] if args_at else body
    tail = re.sub(r'\s*\}\s*\}\s*$', "", tail.rstrip())          # the closing braces of arguments and of the call
    marks = []
    for key, spec in props.items():
        m = re.search(r'"%s"\s*:\s*"' % re.escape(key), tail)
        if m:
            marks.append((m.start(), m.end(), key))
    marks.sort()
    args = {}
    for n, (start, vstart, key) in enumerate(marks):
        end = marks[n + 1][0] if n + 1 < len(marks) else len(tail)
        raw = tail[vstart:end].rstrip()
        raw = re.sub(r'",?\s*$', "", raw) if n + 1 < len(marks) else re.sub(r'"\s*$', "", raw)
        args[key] = _unescape(raw)
    return args


def parse_call(body, tools):
    """One call as {"name", "arguments"(dict)}, or None when nothing usable is there."""
    body = body.strip()
    try:
        obj = json.loads(body)
        if isinstance(obj, dict) and obj.get("name"):
            args = obj.get("arguments", {})
            if isinstance(args, str):
                args = json.loads(args or "{}")
            return {"name": obj["name"], "arguments": args}
    except (ValueError, TypeError):
        pass
    name = re.search(r'"name"\s*:\s*"([\w.-]+)"', body)
    if not name:
        return None
    schema = next((t["function"]["parameters"] for t in tools if t["function"]["name"] == name.group(1)), None)
    args = _lenient_arguments(body, schema)
    required = (schema or {}).get("required", [])
    return {"name": name.group(1), "arguments": args} if all(k in args for k in required) else None


def split_calls(text, tools):
    """(text before the first call, [calls]) from a full reply."""
    if OPEN not in text:
        return text, []
    before, *chunks = text.split(OPEN)
    calls = []
    for chunk in chunks:
        call = parse_call(chunk.split(CLOSE)[0], tools)
        if call:
            calls.append(call)
    return before.strip(), calls


class Muffler:
    """Forwards streamed text to the screen but stops at the first ``<tool_call>`` (and holds back a partial tag)."""

    def __init__(self, emit):
        self.emit, self.buf, self.muted = emit, "", False

    def feed(self, piece):
        if self.muted or not self.emit:
            return
        self.buf += piece
        i = self.buf.find(OPEN)
        if i >= 0:
            if i:
                self.emit(self.buf[:i])
            self.buf, self.muted = "", True
            return
        keep = next((k for k in range(min(len(self.buf), len(OPEN) - 1), 0, -1) if OPEN.startswith(self.buf[-k:])), 0)
        if len(self.buf) > keep:
            self.emit(self.buf[:len(self.buf) - keep])
            self.buf = self.buf[len(self.buf) - keep:]

    def flush(self):
        if not self.muted and self.buf and self.emit:
            self.emit(self.buf)
        self.buf = ""
