"""Router behaviour against tiny fake OpenAI-compatible servers (no real model needed).

    python -m pytest tests/test_router.py     (or: python tests/test_router.py)
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assistant.llm import LLMClient  # noqa: E402
from assistant.router import RoutedLLM  # noqa: E402


def fake_server(behaviour, text="ok"):
    """behaviour: 'ok' streams `text`, 'empty' streams nothing, '429' rate-limits, '500' errors."""
    hits = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length", 0)))
            hits.append(1)
            if behaviour in ("429", "500"):
                self.send_response(int(behaviour))
                if behaviour == "429":
                    self.send_header("retry-after", "7")
                self.end_headers()
                self.wfile.write(b'{"error":"nope"}')
                return
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            content = "" if behaviour == "empty" else text
            chunk = {"choices": [{"delta": {"content": content}}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode())

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, hits


def client(srv, name):
    return LLMClient(f"http://127.0.0.1:{srv.server_port}/v1", name, timeout=5)


def backend(srv, name):
    from assistant.router import Backend
    return Backend(name, client(srv, name), "remote")


USER = [{"role": "user", "content": "hi"}]


def test_local_mode_never_calls_remote():
    local, _ = fake_server("ok", "from local")
    remote, rhits = fake_server("ok", "from remote")
    llm = RoutedLLM(client(local, "l"), [backend(remote, "r")], mode="local")
    msg, stats = llm.chat(USER)
    assert msg["content"] == "from local" and not rhits


def test_empty_local_reply_escalates_and_429_fails_over():
    local, _ = fake_server("empty")
    r1, _ = fake_server("429")
    r2, _ = fake_server("ok", "from r2")
    llm = RoutedLLM(client(local, "l"), [backend(r1, "r1"), backend(r2, "r2")], mode="auto")
    msg, stats = llm.chat(USER)
    assert msg["content"] == "from r2" and stats["backend"] == "r2"
    assert not llm.remotes[0].available  # r1 is cooling down after the 429
    msg, stats = llm.chat(USER)  # r1 is skipped now
    assert stats["backend"] == "r2" or stats["backend"] == "l"


def test_big_context_goes_remote_first():
    local, lhits = fake_server("ok", "from local")
    remote, _ = fake_server("ok", "from remote")
    llm = RoutedLLM(client(local, "l"), [backend(remote, "r")], mode="auto", local_ctx=1000)
    msg, _ = llm.chat([{"role": "user", "content": "x" * 5000}])
    assert msg["content"] == "from remote" and not lhits


def test_tool_error_streak_goes_remote():
    local, lhits = fake_server("ok", "from local")
    remote, _ = fake_server("ok", "from remote")
    llm = RoutedLLM(client(local, "l"), [backend(remote, "r")], mode="auto")
    msgs = USER + [{"role": "tool", "content": "Error: boom"}, {"role": "tool", "content": "x\n[exit code 1]"}]
    msg, _ = llm.chat(msgs)
    assert msg["content"] == "from remote" and not lhits


def test_local_down_falls_back_to_remote_and_all_down_raises():
    dead = LLMClient("http://127.0.0.1:9/v1", "dead", timeout=2)
    remote, _ = fake_server("ok", "from remote")
    llm = RoutedLLM(dead, [backend(remote, "r")], mode="auto")
    assert llm.chat(USER)[0]["content"] == "from remote"
    r500, _ = fake_server("500")
    llm = RoutedLLM(dead, [backend(r500, "r")], mode="auto")
    try:
        llm.chat(USER)
    except Exception:
        return
    raise AssertionError("expected an error when every backend fails")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
