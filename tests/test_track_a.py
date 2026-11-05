"""Track A tests: stateful chat, history endpoint, streaming, error
recovery, signed-cookie auth, and the egress guard on the chat path.

The handler is exercised without a socket: a bare Handler instance is
built with stubbed send_* methods and in-memory rfile/wfile.
"""
import io
import json
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

import app
from app import AUTH_COOKIE, Handler, issue_auth_token, require_auth, verify_auth_token
from vektor_cloud.storage import DashboardStore


# ---------------------------------------------------------------------------
# Fake-handler harness
# ---------------------------------------------------------------------------

def make_handler(method="POST", path="/api/chat", body=None, headers=None):
    h = Handler.__new__(Handler)
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


def response_json(handler):
    return json.loads(handler.wfile.getvalue().decode())


def sse_events(handler):
    events = []
    for line in handler.wfile.getvalue().decode().splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


class FakeResponse:
    """Stand-in for urlopen() results (JSON or SSE body)."""

    def __init__(self, payload, content_type="application/json"):
        if isinstance(payload, bytes):
            self._raw = payload
        else:
            self._raw = json.dumps(payload).encode()
        self.headers = {"Content-Type": content_type}

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self._raw.splitlines(keepends=True))


def llm_reply(text="hello back"):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    old_db = app.DB
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    monkeypatch.delenv("VEKTORCODE_TOOL_PASSWORD", raising=False)
    monkeypatch.delenv("VEKTORCODE_SESSION_SECRET", raising=False)
    app.DB = tmp_path / "t.db"
    app._STORES.clear()
    yield
    app.DB = old_db
    app._STORES.clear()


def do_chat(monkeypatch, body, headers=None):
    calls = []

    def fake_urlopen(req, timeout=120):
        calls.append(req)
        return FakeResponse(llm_reply())

    monkeypatch.setattr(app, "urlopen", fake_urlopen)
    h = make_handler("POST", "/api/chat", body, headers)
    h.do_POST()
    return h, calls


# ---------------------------------------------------------------------------
# Stateful chat + history
# ---------------------------------------------------------------------------

def test_chat_creates_session_and_persists_history(monkeypatch):
    h, calls = do_chat(monkeypatch, {"messages": [{"role": "user", "content": "hi"}], "model": "local"})
    assert h._status == 200
    out = response_json(h)
    assert out["ok"] is True
    assert out["message"] == "hello back"
    assert out["session_id"]
    assert out["tools_used"] == []

    sid = out["session_id"]
    gh = make_handler("GET", f"/api/session/{sid}/messages")
    gh.do_GET()
    assert gh._status == 200
    got = response_json(gh)
    assert got["ok"] is True
    assert got["session_id"] == sid
    assert [(m["role"], m["content"]) for m in got["messages"]] == [
        ("user", "hi"), ("assistant", "hello back")]


def test_chat_history_round_trip_includes_prior_turns(monkeypatch):
    h, _ = do_chat(monkeypatch, {"messages": [{"role": "user", "content": "first"}], "model": "local"})
    sid = response_json(h)["session_id"]
    h2, calls = do_chat(monkeypatch, {"messages": [{"role": "user", "content": "second"}], "model": "local", "session_id": sid})
    assert response_json(h2)["session_id"] == sid
    sent = json.loads(calls[0].data.decode())
    roles_contents = [(m["role"], m["content"]) for m in sent["messages"]]
    assert ("user", "first") in roles_contents
    assert ("assistant", "hello back") in roles_contents
    assert ("user", "second") in roles_contents
    # History is capped (last 20) — the system prompt is message 0.
    assert len(sent["messages"]) <= 1 + 20


def test_session_isolation(monkeypatch):
    h1, _ = do_chat(monkeypatch, {"messages": [{"role": "user", "content": "aaa"}], "model": "local"})
    h2, _ = do_chat(monkeypatch, {"messages": [{"role": "user", "content": "bbb"}], "model": "local"})
    sid1, sid2 = response_json(h1)["session_id"], response_json(h2)["session_id"]
    assert sid1 != sid2
    for sid, want in ((sid1, "aaa"), (sid2, "bbb")):
        gh = make_handler("GET", f"/api/session/{sid}/messages")
        gh.do_GET()
        msgs = response_json(gh)["messages"]
        assert [m["content"] for m in msgs if m["role"] == "user"] == [want]


def test_messages_endpoint_unknown_session_is_empty(monkeypatch):
    gh = make_handler("GET", "/api/session/no-such-id/messages")
    gh.do_GET()
    assert gh._status == 200
    assert response_json(gh)["messages"] == []


def test_resume_returns_history(monkeypatch):
    h, _ = do_chat(monkeypatch, {"messages": [{"role": "user", "content": "resume me"}], "model": "local"})
    sid = response_json(h)["session_id"]
    rh = make_handler("POST", "/api/session/resume", {"id": sid})
    rh.do_POST()
    assert rh._status == 200
    out = response_json(rh)
    assert out["ok"] is True
    assert [m["content"] for m in out["messages"]] == ["resume me", "hello back"]


def test_storage_message_roundtrip_and_cap(tmp_path):
    store = DashboardStore(tmp_path / "s.db")
    store.create_session("s1", "T", "local")
    for i in range(25):
        store.add_message("s1", "user", f"m{i}")
    msgs = store.list_messages("s1")
    assert len(msgs) == 20  # capped at HISTORY_LIMIT
    assert msgs[0]["content"] == "m5"
    assert msgs[-1]["content"] == "m24"
    store.delete_session("s1")
    assert store.list_messages("s1") == []


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

def test_streaming_sse_tokens_and_done(monkeypatch):
    sse = (b'data: {"choices": [{"delta": {"content": "hel"}}]}\n\n'
           b'data: {"choices": [{"delta": {"content": "lo"}}]}\n\n'
           b'data: [DONE]\n\n')
    monkeypatch.setattr(app, "urlopen", lambda req, timeout=120: FakeResponse(sse, "text/event-stream"))
    h = make_handler("POST", "/api/chat", {"messages": [{"role": "user", "content": "hi"}], "model": "local", "stream": True})
    h.do_POST()
    assert h._status == 200
    assert h._resp_headers.get("Content-Type") == "text/event-stream"
    events = sse_events(h)
    assert [e["content"] for e in events if e["type"] == "delta"] == ["hel", "lo"]
    done = [e for e in events if e["type"] == "done"]
    assert len(done) == 1
    assert done[0]["message"] == "hello"
    assert done[0]["session_id"]
    # Streamed reply was persisted.
    gh = make_handler("GET", f"/api/session/{done[0]['session_id']}/messages")
    gh.do_GET()
    assert response_json(gh)["messages"][-1]["content"] == "hello"


def test_streaming_falls_back_to_full_json_reply(monkeypatch):
    monkeypatch.setattr(app, "urlopen", lambda req, timeout=120: FakeResponse(llm_reply("full reply")))
    h = make_handler("POST", "/api/chat", {"messages": [{"role": "user", "content": "hi"}], "model": "local", "stream": True})
    h.do_POST()
    events = sse_events(h)
    done = [e for e in events if e["type"] == "done"]
    assert len(done) == 1
    assert done[0]["message"] == "full reply"


def test_streaming_request_sends_stream_true(monkeypatch):
    calls = []
    sse = b'data: {"choices": [{"delta": {"content": "x"}}]}\n\ndata: [DONE]\n\n'

    def fake_urlopen(req, timeout=120):
        calls.append(req)
        return FakeResponse(sse, "text/event-stream")

    monkeypatch.setattr(app, "urlopen", fake_urlopen)
    h = make_handler("POST", "/api/chat", {"messages": [{"role": "user", "content": "hi"}], "model": "local", "stream": True})
    h.do_POST()
    assert json.loads(calls[0].data.decode())["stream"] is True


def test_non_streaming_request_sends_stream_false(monkeypatch):
    _, calls = do_chat(monkeypatch, {"messages": [{"role": "user", "content": "hi"}], "model": "local"})
    assert json.loads(calls[0].data.decode())["stream"] is False


# ---------------------------------------------------------------------------
# Error recovery + statuses
# ---------------------------------------------------------------------------

def test_kill_switch_423_not_retryable(monkeypatch):
    app.store().set_setting("kill_switch", True)
    h, _ = do_chat(monkeypatch, {"messages": [{"role": "user", "content": "hi"}], "model": "local"})
    assert h._status == 423
    out = response_json(h)
    assert out["retryable"] is False


def test_provider_not_configured_400_not_retryable(monkeypatch):
    monkeypatch.delenv("GROQ_KEYS", raising=False)
    h, _ = do_chat(monkeypatch, {"messages": [{"role": "user", "content": "hi"}], "model": "groq"})
    assert h._status == 400
    assert response_json(h)["retryable"] is False


def test_rate_limited_429_retryable(monkeypatch):
    def boom(req, timeout=120):
        raise HTTPError(req.full_url, 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(app, "urlopen", boom)
    h = make_handler("POST", "/api/chat", {"messages": [{"role": "user", "content": "hi"}], "model": "local"})
    h.do_POST()
    assert h._status == 429
    assert response_json(h)["retryable"] is True


def test_provider_failure_502_retryable(monkeypatch):
    def boom(req, timeout=120):
        raise URLError("connection refused")

    monkeypatch.setattr(app, "urlopen", boom)
    h = make_handler("POST", "/api/chat", {"messages": [{"role": "user", "content": "hi"}], "model": "local"})
    h.do_POST()
    assert h._status == 502
    assert response_json(h)["retryable"] is True


# ---------------------------------------------------------------------------
# Auth: signed cookie
# ---------------------------------------------------------------------------

def test_signed_cookie_accepted_forged_rejected(monkeypatch):
    monkeypatch.setenv("VEKTORCODE_TOOL_PASSWORD", "pw")
    token = issue_auth_token()
    assert verify_auth_token(token) is True

    h = make_handler("GET", "/api/state", headers={"Cookie": f"{AUTH_COOKIE}={token}"})
    assert require_auth(h) is True

    # Old unsigned literal is rejected.
    h = make_handler("GET", "/api/state", headers={"Cookie": "vc_auth=1"})
    assert require_auth(h) is False
    # Tampered signature is rejected.
    bad = token[:-1] + ("A" if token[-1] != "A" else "B")
    h = make_handler("GET", "/api/state", headers={"Cookie": f"{AUTH_COOKIE}={bad}"})
    assert require_auth(h) is False
    # No cookie is rejected.
    h = make_handler("GET", "/api/state")
    assert require_auth(h) is False


def test_auth_open_when_no_password_set():
    h = make_handler("GET", "/api/state")
    assert require_auth(h) is True


def test_chat_401_when_password_set_no_cookie(monkeypatch):
    monkeypatch.setenv("VEKTORCODE_TOOL_PASSWORD", "pw")
    h = make_handler("POST", "/api/chat", {"messages": [{"role": "user", "content": "hi"}], "model": "local"})
    h.do_POST()
    assert h._status == 401


def test_chat_allowed_with_signed_cookie(monkeypatch):
    monkeypatch.setenv("VEKTORCODE_TOOL_PASSWORD", "pw")
    h, _ = do_chat(monkeypatch, {"messages": [{"role": "user", "content": "hi"}], "model": "local"},
                   headers={"Cookie": f"{AUTH_COOKIE}={issue_auth_token()}"})
    assert h._status == 200


def test_kill_requires_auth_when_password_set(monkeypatch):
    monkeypatch.setenv("VEKTORCODE_TOOL_PASSWORD", "pw")
    h = make_handler("POST", "/api/kill", {})
    h.do_POST()
    assert h._status == 401
    assert app.store().get_setting("kill_switch", False) is False

    h = make_handler("POST", "/api/kill", {}, headers={"Cookie": f"{AUTH_COOKIE}={issue_auth_token()}"})
    h.do_POST()
    assert h._status == 200
    assert app.store().get_setting("kill_switch", False) is True


def test_login_sets_signed_cookie(monkeypatch):
    monkeypatch.setenv("VEKTORCODE_TOOL_PASSWORD", "pw")
    h = make_handler("POST", "/api/login", {"password": "pw"})
    h.do_POST()
    assert h._status == 204
    cookie = h._resp_headers.get("Set-Cookie", "")
    assert cookie.startswith(f"{AUTH_COOKIE}=")
    assert cookie.split("=", 1)[1].split(";")[0].count(".") == 1  # body.signature
    assert "vc_auth=1;" not in cookie
    assert "HttpOnly" in cookie

    h = make_handler("POST", "/api/login", {"password": "wrong"})
    h.do_POST()
    assert h._status == 401


def test_model_and_session_endpoints_require_auth(monkeypatch):
    monkeypatch.setenv("VEKTORCODE_TOOL_PASSWORD", "pw")
    for path, body in (("/api/model", {"model": "groq"}),
                       ("/api/session", {"title": "t"}),
                       ("/api/session/resume", {"id": "x"}),
                       ("/api/session/delete", {"id": "x"})):
        h = make_handler("POST", path, body)
        h.do_POST()
        assert h._status == 401, path
    # ...and pass with a signed cookie.
    h = make_handler("POST", "/api/model", {"model": "groq"},
                     headers={"Cookie": f"{AUTH_COOKIE}={issue_auth_token()}"})
    h.do_POST()
    assert h._status == 200


# ---------------------------------------------------------------------------
# Egress guard on the chat path
# ---------------------------------------------------------------------------

def test_chat_egress_blocks_non_allowlisted_host(monkeypatch):
    calls = []

    def boom(req, timeout=120):
        calls.append(req)
        raise AssertionError("urlopen must not be called for a blocked host")

    monkeypatch.setattr(app, "urlopen", boom)
    evil = ("evil", "Evil", "EVIL_KEYS", "https://evil.example.com/v1/chat/completions", "evil-model")
    monkeypatch.setattr(app, "PROVIDERS", list(app.PROVIDERS) + [evil])
    monkeypatch.setattr(app, "KEY_POOLS", {**app.KEY_POOLS, "evil": ["test-key-not-real"]})
    monkeypatch.setattr(app, "KEY_INDEX", {**app.KEY_INDEX, "evil": 0})
    monkeypatch.setenv("EVIL_KEYS", "test-key-not-real")
    h = make_handler("POST", "/api/chat", {"messages": [{"role": "user", "content": "hi"}], "model": "evil"})
    h.do_POST()
    assert h._status == 502
    assert "Egress blocked" in response_json(h)["error"]
    assert response_json(h)["retryable"] is False
    assert calls == []


def test_all_configured_provider_endpoints_pass_guard():
    from vektor_cloud.egress import guard_url
    for _pid, _name, _env, url, _model in app.PROVIDERS:
        if url != "local":
            guard_url(url)  # raises if not allowlisted
    # DashScope host used by the Qwen/Ollama fallback path.
    guard_url("https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions")
    # Local-Ollama loopback exception still works.
    guard_url("http://127.0.0.1:11434/v1/chat/completions")
