"""Skill invocation in chat (hypit-integration, 2026-11-15).

Lab tests only: no live Render, no live Supabase, no live provider.
The provider is faked; the stores run against real temporary SQLite
files. Verifies that "/skill <slug>" loads the skill into the LLM
payload as a system message and declares the reference tool — and
that a turn with no skill declares neither.
"""
import io
import json

import pytest

import app


def make_handler(method="POST", path="/api/chat", body=None, headers=None):
    h = app.Handler.__new__(app.Handler)
    h.path = path
    h.command = method
    raw = json.dumps(body if body is not None else {}).encode()
    h.rfile = io.BytesIO(raw)
    h.headers = dict(headers or {})
    h.headers["Content-Length"] = str(len(raw))
    h.wfile = io.BytesIO()
    h._status = None
    h._resp_headers = {}

    def send_response(status, message=None):
        h._status = status

    def send_header(key, value):
        h._resp_headers[key] = value

    def end_headers():
        pass

    def send_error(code, message=None):
        h._status = code
        h.wfile.write(json.dumps({"error": message}).encode())

    h.send_response = send_response
    h.send_header = send_header
    h.end_headers = end_headers
    h.send_error = send_error
    return h


class FakeResponse:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode()
        self.headers = {"Content-Type": "application/json"}

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def llm_reply(text="hello back"):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


@pytest.fixture
def chat_env(monkeypatch, tmp_path):
    """Temp DB + temp state dir, Groq provider faked with a dummy key."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    monkeypatch.setattr(app, "DB", tmp_path / "test.db")
    monkeypatch.setattr(app, "VEKTOR_STATE", tmp_path / "state")
    monkeypatch.setenv("GROQ_KEYS", "k-groq")
    monkeypatch.setitem(app.KEY_POOLS, "groq", ["k-groq"])
    monkeypatch.setitem(app.KEY_INDEX, "groq", 0)
    return tmp_path


def _post_chat(monkeypatch, content, **extra):
    calls = []

    def fake_urlopen(req, timeout=120):
        calls.append(req)
        return FakeResponse(llm_reply("done"))

    monkeypatch.setattr(app, "urlopen", fake_urlopen)
    body = {"messages": [{"role": "user", "content": content}],
            "model": "groq"}
    body.update(extra)
    h = make_handler("POST", "/api/chat", body)
    h.do_POST()
    assert h._status == 200, h.wfile.getvalue().decode()
    payload = json.loads(calls[0].data)
    return payload


def test_skill_loads_into_chat(monkeypatch, chat_env):
    payload = _post_chat(monkeypatch, "/skill hypit make a video")
    system_texts = [m["content"] for m in payload["messages"]
                    if m["role"] == "system"]
    assert any("Skill: hypit" in text for text in system_texts)
    assert any("director and producer" in text for text in system_texts)
    tool_names = {t["function"]["name"] for t in payload["tools"]}
    assert "read_skill_reference" in tool_names


def test_skill_field_in_payload(monkeypatch, chat_env):
    payload = _post_chat(monkeypatch, "make a video", skill="hypit")
    system_texts = [m["content"] for m in payload["messages"]
                    if m["role"] == "system"]
    assert any("Skill: hypit" in text for text in system_texts)


def test_no_skill_no_reference_tool(monkeypatch, chat_env):
    payload = _post_chat(monkeypatch, "hi there")
    system_texts = [m["content"] for m in payload["messages"]
                    if m["role"] == "system"]
    assert not any("Skill: hypit" in text for text in system_texts)
    tool_names = {t["function"]["name"] for t in payload["tools"]}
    assert "read_skill_reference" not in tool_names


def test_unknown_skill_reported_to_model(monkeypatch, chat_env):
    payload = _post_chat(monkeypatch, "/skill nope do it")
    system_texts = [m["content"] for m in payload["messages"]
                    if m["role"] == "system"]
    assert any("not installed" in text for text in system_texts)
    assert any("hypit" in text for text in system_texts)
