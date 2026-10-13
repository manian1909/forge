"""http_request: key placeholders, redaction, and which methods need permission (no network: httpx is faked)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from assistant.tools import Toolbox, needs_confirmation  # noqa: E402


class FakeResponse:
    def __init__(self, data, status=200):
        self.status_code, self.reason_phrase = status, "OK" if status == 200 else "Unauthorized"
        self.text = json.dumps(data)
        self.content = self.text.encode()
        self.headers = {"content-type": "application/json"}

    def json(self):
        return json.loads(self.text)


def test_keys_are_filled_in_and_never_shown(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_HOME", str(tmp_path))
    (tmp_path / "keys.json").write_text(json.dumps({"WEATHER_KEY": "s3cret-value"}))
    sent = {}

    def fake(method, url, params=None, headers=None, json=None, **kw):
        sent.update(method=method, url=url, params=params, headers=headers)
        return FakeResponse({"echo": params["appid"], "temp": 21.5})

    monkeypatch.setattr(httpx, "request", fake)
    out = Toolbox(tmp_path).call("http_request", {"url": "https://api.x/w", "params": {"q": "Delhi", "appid": "{{WEATHER_KEY}}"}})
    assert sent["params"]["appid"] == "s3cret-value" and sent["method"] == "GET"
    assert "s3cret-value" not in out and "***" in out and "21.5" in out and "HTTP 200" in out


def test_missing_key_explains_how_to_add_it(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_HOME", str(tmp_path))
    monkeypatch.setattr(httpx, "request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called")))
    out = Toolbox(tmp_path).call("http_request", {"url": "https://api.x", "headers": {"Authorization": "Bearer {{NEWS_KEY}}"}})
    assert "/key NEWS_KEY" in out


def test_only_writes_need_permission():
    assert not needs_confirmation("http_request", {"url": "u"})
    assert not needs_confirmation("http_request", {"url": "u", "method": "get"})
    assert needs_confirmation("http_request", {"url": "u", "method": "POST"})
    assert needs_confirmation("write_file", {}) and not needs_confirmation("read_file", {})
