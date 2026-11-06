"""Qwen default + Grok removal + per-provider free model selection +
Ollama/Qwen settings persistence.

Uses the same bare-Handler harness as test_track_a (no socket).
"""
import io
import json

import pytest

import app
from app import (FREE_MODELS, PROVIDERS, QWEN_SOURCE, default_model,
                 provider_models)
from vektor_cloud.egress import VEKTOR_EGRESS_ALLOWLIST
from vektor_cloud.storage import DashboardStore


# ---------------------------------------------------------------------------
# Fake-handler harness (mirrors test_track_a)
# ---------------------------------------------------------------------------

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


def response_json(handler):
    return json.loads(handler.wfile.getvalue().decode())


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


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    old_db = app.DB
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    monkeypatch.delenv("VEKTORCODE_TOOL_PASSWORD", raising=False)
    monkeypatch.delenv("VEKTORCODE_SESSION_SECRET", raising=False)
    monkeypatch.delenv("GROQ_KEYS", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("QWEN_KEYS", raising=False)
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


def get_state():
    h = make_handler("GET", "/api/state")
    h.do_GET()
    assert h._status == 200
    return response_json(h)


# ---------------------------------------------------------------------------
# Grok removal
# ---------------------------------------------------------------------------

def test_grok_provider_removed_everywhere():
    ids = [p[0] for p in PROVIDERS]
    assert "grok" not in ids
    assert "groq" in ids  # Groq (free route) stays
    assert not any(m["id"] == "grok" for m in provider_models())
    assert "api.x.ai" not in VEKTOR_EGRESS_ALLOWLIST
    assert "api.groq.com" in VEKTOR_EGRESS_ALLOWLIST
    state = get_state()
    assert not any(m["id"] == "grok" for m in state["models"])


# ---------------------------------------------------------------------------
# Qwen default
# ---------------------------------------------------------------------------

def test_qwen_is_default_model():
    assert default_model() == "qwen"
    state = get_state()
    assert state["active_model"] == "qwen"
    assert state["qwen_source"] == QWEN_SOURCE
    qwen = next(m for m in state["models"] if m["id"] == "qwen")
    assert qwen["model"] == "qwen/qwen3.8-27b"
    assert QWEN_SOURCE in qwen["description"]
    assert "does not run on Render" in qwen["description"]


def test_qwen_unconfigured_reports_route_none_honestly():
    state = get_state()
    qwen = next(m for m in state["models"] if m["id"] == "qwen")
    assert qwen["route"] == "none"
    assert qwen["configured"] is False


# ---------------------------------------------------------------------------
# Free model lists
# ---------------------------------------------------------------------------

def test_free_model_lists_only_free_models():
    assert "qwen/qwen3.8-27b" in FREE_MODELS["groq"]
    assert "openai/gpt-oss-120b" in FREE_MODELS["groq"]
    assert "gemini-3.8-flash" in FREE_MODELS["gemini"]
    assert all(m.endswith(":free") for m in FREE_MODELS["openrouter"])
    # Paid-only providers are gone entirely — free-tier only (Wallace 2026-11-05).
    assert "openai" not in FREE_MODELS
    assert "cohere" not in FREE_MODELS
    assert "openai" not in {p[0] for p in PROVIDERS}
    assert "cohere" not in {p[0] for p in PROVIDERS}
    # No paid flagship models anywhere in the free lists.
    paid = {"gpt-4o", "gpt-4.1", "claude-sonnet-4-5", "grok-3-mini",
            "grok-4", "gemini-2.5-pro"}
    for pid, models in FREE_MODELS.items():
        assert not (paid & set(models)), pid


def test_state_exposes_models_and_api_urls():
    state = get_state()
    by_id = {m["id"]: m for m in state["models"]}
    assert by_id["groq"]["api_url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert by_id["groq"]["models"] == FREE_MODELS["groq"]
    assert by_id["groq"]["key_env"] == "GROQ_KEYS"
    assert by_id["gemini"]["api_url"].startswith("https://generativelanguage.googleapis.com/")
    # Paid-only providers are gone from /api/state entirely.
    assert "openai" not in by_id
    assert "cohere" not in by_id
    assert "free_note" not in by_id["groq"]


def test_stale_saved_model_falls_back_to_qwen():
    # A saved active_model pointing at a removed provider (e.g. OpenAI,
    # saved before the free-tier-only cleanup) must resolve to Qwen, not
    # report a provider that no longer exists.
    h = make_handler("POST", "/api/model", {"model": "qwen"})
    h.save_setting("active_model", "openai")
    h.save_setting("active_model_name", "gpt-4o-mini")
    state = get_state()
    assert state["active_model"] == "qwen"
    assert state["active_model_name"] is None
    # resolve_model_id: stale saved id, no candidate -> default.
    assert app.resolve_model_id(h) == "qwen"
    assert app.resolve_model_id(h, "openai") == "qwen"
    assert app.resolve_model_id(h, "groq") == "groq"


# ---------------------------------------------------------------------------
# /api/model: two-level selection
# ---------------------------------------------------------------------------

def test_model_select_valid_free_model_persists():
    h = make_handler("POST", "/api/model",
                     {"model": "groq", "model_name": "openai/gpt-oss-120b"})
    h.do_POST()
    assert h._status == 200
    assert response_json(h)["ok"] is True
    state = get_state()
    assert state["active_model"] == "groq"
    assert state["active_model_name"] == "openai/gpt-oss-120b"


def test_model_select_paid_or_unknown_model_rejected():
    for bad in ("gpt-4o", "grok-3-mini", "not-a-model"):
        h = make_handler("POST", "/api/model",
                         {"model": "groq", "model_name": bad})
        h.do_POST()
        assert h._status == 400, bad
    # Provider itself still validates.
    h = make_handler("POST", "/api/model", {"model": "grok"})
    h.do_POST()
    assert h._status == 400


def test_model_select_clears_model_name_when_omitted():
    h = make_handler("POST", "/api/model",
                     {"model": "groq", "model_name": "openai/gpt-oss-120b"})
    h.do_POST()
    assert h._status == 200
    h = make_handler("POST", "/api/model", {"model": "qwen"})
    h.do_POST()
    assert h._status == 200
    assert get_state()["active_model_name"] in (None, "")


# ---------------------------------------------------------------------------
# /api/settings: Ollama + Qwen URLs
# ---------------------------------------------------------------------------

def test_settings_save_valid_urls_and_state_reflects_them():
    h = make_handler("POST", "/api/settings",
                     {"ollama_base_url": "http://127.0.0.1:11434",
                      "qwen_api_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"})
    h.do_POST()
    assert h._status == 200
    out = response_json(h)
    assert out["ok"] is True
    assert out["ollama_base_url"] == "http://127.0.0.1:11434"
    state = get_state()
    assert state["ollama_base_url"] == "http://127.0.0.1:11434"
    local = next(m for m in state["models"] if m["id"] == "local")
    assert local["api_url"] == "http://127.0.0.1:11434/v1/chat/completions"


def test_settings_reject_bad_urls():
    for bad in ("ftp://example.com", "not-a-url", "http://user:pass@host:11434",
                "http://localhost:11434", "javascript:alert(1)"):
        # localhost is a valid http URL and IS accepted; skip it here.
        if bad == "http://localhost:11434":
            continue
        h = make_handler("POST", "/api/settings", {"ollama_base_url": bad})
        h.do_POST()
        assert h._status == 400, bad


def test_settings_accept_localhost_ollama_url():
    h = make_handler("POST", "/api/settings",
                     {"ollama_base_url": "http://localhost:11434"})
    h.do_POST()
    assert h._status == 200


# ---------------------------------------------------------------------------
# Chat: Qwen routing
# ---------------------------------------------------------------------------

def test_qwen_chat_without_any_route_is_honest_400(monkeypatch):
    h, calls = do_chat(monkeypatch, {
        "messages": [{"role": "user", "content": "hi"}], "model": "qwen"})
    assert h._status == 400
    assert "not configured" in response_json(h)["error"]
    assert calls == []  # no provider was ever contacted


def test_qwen_chat_groq_route_reaches_real_groq_attempt(monkeypatch):
    monkeypatch.setenv("GROQ_KEYS", "k1,k2")
    monkeypatch.setitem(app.KEY_POOLS, "qwen", ["k1", "k2"])
    monkeypatch.setitem(app.KEY_INDEX, "qwen", 0)
    h, calls = do_chat(monkeypatch, {
        "messages": [{"role": "user", "content": "hi"}], "model": "qwen"})
    assert h._status == 200
    out = response_json(h)
    assert out["ok"] is True
    assert out["model"] == "qwen/qwen3.8-27b"
    assert len(calls) == 1
    assert calls[0].full_url == "https://api.groq.com/openai/v1/chat/completions"
    assert calls[0].headers["Authorization"] == "Bearer k1"
    sent = json.loads(calls[0].data.decode())
    assert sent["model"] == "qwen/qwen3.8-27b"


def test_qwen_chat_falls_back_to_ollama_without_groq_keys(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    h, calls = do_chat(monkeypatch, {
        "messages": [{"role": "user", "content": "hi"}], "model": "qwen"})
    assert h._status == 200
    out = response_json(h)
    assert out["model"] == "qwen2.5:7b"
    assert len(calls) == 1
    assert calls[0].full_url == "http://127.0.0.1:11434/v1/chat/completions"
    assert "Authorization" not in calls[0].headers  # local: no auth header


def test_qwen_chat_explicit_ollama_model_uses_ollama_route(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    h, calls = do_chat(monkeypatch, {
        "messages": [{"role": "user", "content": "hi"}],
        "model": "qwen", "model_name": "qwen2.5:7b"})
    assert h._status == 200
    assert response_json(h)["model"] == "qwen2.5:7b"
    assert calls[0].full_url == "http://127.0.0.1:11434/v1/chat/completions"


def test_qwen_chat_saved_ollama_url_wins_over_env(monkeypatch):
    # Save the URL through the settings endpoint, then chat with no env set.
    h = make_handler("POST", "/api/settings",
                     {"ollama_base_url": "http://127.0.0.1:11434"})
    h.do_POST()
    assert h._status == 200
    h, calls = do_chat(monkeypatch, {
        "messages": [{"role": "user", "content": "hi"}], "model": "qwen"})
    assert h._status == 200
    assert calls[0].full_url == "http://127.0.0.1:11434/v1/chat/completions"


def test_chat_persists_provider_model_per_session(monkeypatch):
    monkeypatch.setenv("GROQ_KEYS", "k1")
    monkeypatch.setitem(app.KEY_POOLS, "qwen", ["k1"])
    monkeypatch.setitem(app.KEY_INDEX, "qwen", 0)
    h, _ = do_chat(monkeypatch, {
        "messages": [{"role": "user", "content": "hi"}], "model": "qwen"})
    sid = response_json(h)["session_id"]
    sessions = get_state()["sessions"]
    mine = next(s for s in sessions if s["id"] == sid)
    assert mine["model"] == "qwen:qwen/qwen3.8-27b"


def test_groq_model_switch_changes_model_sent(monkeypatch):
    monkeypatch.setenv("GROQ_KEYS", "k1")
    monkeypatch.setitem(app.KEY_POOLS, "groq", ["k1"])
    monkeypatch.setitem(app.KEY_INDEX, "groq", 0)
    h, calls = do_chat(monkeypatch, {
        "messages": [{"role": "user", "content": "hi"}],
        "model": "groq", "model_name": "openai/gpt-oss-120b"})
    assert h._status == 200
    sent = json.loads(calls[0].data.decode())
    assert sent["model"] == "openai/gpt-oss-120b"


def test_local_chat_no_longer_double_paths_llm_url(monkeypatch):
    # LLM_URL default already ends with /v1/chat/completions; the old code
    # appended it a second time. With no OLLAMA_BASE_URL the endpoint must
    # be exactly LLM_URL.
    h, calls = do_chat(monkeypatch, {
        "messages": [{"role": "user", "content": "hi"}], "model": "local"})
    assert h._status == 200
    assert calls[0].full_url == "http://127.0.0.1:11434/v1/chat/completions"
