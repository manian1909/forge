"""Tool calls are recovered from the model's text even when its JSON is slightly broken, and never shown on screen."""

import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assistant import toolparse  # noqa: E402
from assistant.llm import LLMClient  # noqa: E402
from assistant.tools import TOOL_SCHEMAS  # noqa: E402

GOOD = '<tool_call>\n{"name": "run_python", "arguments": {"code": "print(\\"hi\\")\\nx = 1"}}\n</tool_call>'
# one quote that should have been \" (this is what the 8B really produced inside a long script)
BROKEN = '<tool_call>\n{"name": "run_python", "arguments": {"code": "f(b, \\"Used\\", \\"=IF(A)", \\"0%\\")\\nprint(1)"}}\n</tool_call>'


def test_valid_json_is_read_normally():
    before, calls = toolparse.split_calls("Sure.\n" + GOOD, TOOL_SCHEMAS)
    assert before == "Sure." and calls == [{"name": "run_python", "arguments": {"code": 'print("hi")\nx = 1'}}]


def test_an_unescaped_quote_does_not_lose_the_call():
    with_json = None
    try:
        json.loads(BROKEN.split("<tool_call>")[1].split("</tool_call>")[0])
    except ValueError as e:
        with_json = e
    assert with_json, "the fixture must be invalid JSON"
    _, calls = toolparse.split_calls(BROKEN, TOOL_SCHEMAS)
    assert calls[0]["arguments"]["code"] == 'f(b, "Used", "=IF(A)", "0%")\nprint(1)'


def test_two_string_arguments_and_a_missing_required_one():
    text = '<tool_call>{"name": "write_file", "arguments": {"path": "a.txt", "content": "line1\\nsay "hi"\\n"}}</tool_call>'
    _, calls = toolparse.split_calls(text, TOOL_SCHEMAS)
    assert calls[0]["arguments"] == {"path": "a.txt", "content": 'line1\nsay "hi"\n'}
    cut_off = '<tool_call>{"name": "write_file", "arguments": {"path": "a.txt", "content": "unfinished'
    assert toolparse.split_calls(cut_off, TOOL_SCHEMAS)[1][0]["arguments"]["content"] == "unfinished"   # kept: the file gets what was written
    assert toolparse.split_calls('<tool_call>{"name": "write_file", "arguments": {"path": "a.txt", ', TOOL_SCHEMAS)[1] == []
    assert toolparse.split_calls("no call here", TOOL_SCHEMAS) == ("no call here", [])


def test_muffler_stops_at_the_tag_and_holds_back_a_partial_one():
    shown = []
    m = toolparse.Muffler(shown.append)
    for piece in ["Hello ", "wor", "ld <tool", "_call>\n{\"name\"", ": 1}"]:
        m.feed(piece)
    m.flush()
    assert "".join(shown) == "Hello world "
    shown.clear()
    m = toolparse.Muffler(shown.append)
    for piece in ["a < b and ", "c <to", "day"]:
        m.feed(piece)
    m.flush()
    assert "".join(shown) == "a < b and c <today"


def sse(*texts):
    lines = [f"data: {json.dumps({'choices': [{'delta': {'content': t}}]})}" for t in texts]
    lines.append(f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 9, 'completion_tokens': 5}})}")
    return "\n\n".join(lines + ["data: [DONE]", ""])


def test_client_sends_tools_in_the_prompt_and_returns_structured_calls():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, text=sse("Let me run it. ", BROKEN[:40], BROKEN[40:]))

    client = LLMClient("http://x/v1", "forge", own_tools=True)
    client.http = httpx.Client(transport=httpx.MockTransport(handler))
    shown = []
    msg, stats = client.chat([{"role": "system", "content": "You are Forge."}, {"role": "user", "content": "go"}],
                             tools=TOOL_SCHEMAS, on_token=shown.append)
    assert "tools" not in seen["body"] and "<tools>" in seen["body"]["messages"][0]["content"]
    assert seen["body"]["messages"][0]["content"].startswith("You are Forge.")
    assert "".join(shown) == "Let me run it. " and msg["content"] == "Let me run it."
    call = msg["tool_calls"][0]["function"]
    assert call["name"] == "run_python" and json.loads(call["arguments"])["code"].endswith("print(1)")
    assert stats["prompt_tokens"] == 9 and stats["finish"] == "stop"


def test_remote_style_client_still_uses_native_tools():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, text=sse("ok"))

    client = LLMClient("http://x/v1", "m")
    client.http = httpx.Client(transport=httpx.MockTransport(handler))
    client.chat([{"role": "system", "content": "s"}, {"role": "user", "content": "go"}], tools=TOOL_SCHEMAS)
    assert seen["body"]["tools"] and "<tools>" not in seen["body"]["messages"][0]["content"]


def test_a_reply_cut_off_at_the_token_cap_runs_nothing():
    def handler(request):
        lines = [f"data: {json.dumps({'choices': [{'delta': {'content': GOOD}}]})}",
                 f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': 'length'}]})}", "data: [DONE]", ""]
        return httpx.Response(200, text="\n\n".join(lines))

    client = LLMClient("http://x/v1", "forge", own_tools=True)
    client.http = httpx.Client(transport=httpx.MockTransport(handler))
    msg, stats = client.chat([{"role": "system", "content": "s"}, {"role": "user", "content": "go"}], tools=TOOL_SCHEMAS)
    assert "tool_calls" not in msg and stats["finish"] == "length"
