"""NVIDIA NIM as a chat provider (Wallace's order, 2026-11-09).

Free endpoints on build.nvidia.com, OpenAI-compatible at
https://integrate.api.nvidia.com/v1/chat/completions. No card, 1,000
calls/mo (5,000 after phone verification), 40 req/min, eval-only terms.
Env var NVIDIA_NIM_KEYS (plural, Wallace's naming rule). Mid-cascade:
FALLBACK_ORDER right after openrouter.

All tests mocked (no network, no real key). Uses the same bare-Handler
harness as test_qwen_models (no socket).
"""
import io
import json
from urllib.error import HTTPError
from urllib.parse import urlparse

import pytest

import app
from app import FALLBACK_ORDER, FREE_MODELS, PROVIDERS, provider_models
from vektor_cloud.egress import VEKTOR_EGRESS_ALLOWLIST
from vektor_cloud.storage import DashboardStore


NVIDIA_ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"
NVIDIA_DEFAULT = "deepseek-ai/deepseek-v4.1-flash"
NVIDIA_FREE_MODELS = [
    "deepseek-ai/deepseek-v4.1-flash",
    "z-ai/glm-5.3-flash",
    "nvidia/nemotron-3-ultra-550b-a55b",
    "google/gemma-4-31b-it",
    "openai/gpt-oss-20b",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
    "mistralai/mistral-large-4-1t-a52b",
]


# ---------------------------------------------------------------------------
# Fake-handler harness (mirrors test_qwen_models)
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


def http_error(url, code, msg):
    return HTTPError(url, code, msg, {}, None)


def set_pool(monkeypatch, pid, keys):
    monkeypatch.setitem(app.KEY_POOLS, pid, list(keys))
    monkeypatch.setitem(app.KEY_INDEX, pid, 0)


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    old_db = app.DB
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    monkeypatch.delenv("VEKTORCODE_TOOL_PASSWORD", raising=False)
    monkeypatch.delenv("VEKTORCODE_SESSION_SECRET", raising=False)
    monkeypatch.delenv("GROQ_KEYS", raising=False)
    monkeypatch.delenv("OPENROUTER_KEYS", raising=False)
    monkeypatch.delenv("NVIDIA_NIM_KEYS", raising=False)
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
# Registration
# ---------------------------------------------------------------------------

def test_nvidia_provider_registered():
    entry = next((p for p in PROVIDERS if p[0] == "nvidia"), None)
    assert entry is not None
    pid, name, env, url, model = entry
    assert name == "NVIDIA NIM"
    assert env == "NVIDIA_NIM_KEYS"
    assert url == NVIDIA_ENDPOINT
    assert model == NVIDIA_DEFAULT


def test_nvidia_free_models_exact():
    assert FREE_MODELS["nvidia"] == NVIDIA_FREE_MODELS
    assert FREE_MODELS["nvidia"][0] == NVIDIA_DEFAULT
    # No paid flagship models in the list.
    paid = {"gpt-4o", "gpt-4.1", "claude-sonnet-4-5", "grok-3-mini",
            "grok-4", "gemini-2.5-pro"}
    assert not (paid & set(FREE_MODELS["nvidia"]))


def test_nvidia_fallback_order_after_openrouter():
    assert "nvidia" in FALLBACK_ORDER
    assert FALLBACK_ORDER.index("nvidia") == FALLBACK_ORDER.index("openrouter") + 1
    assert FALLBACK_ORDER.index("nvidia") < FALLBACK_ORDER.index("local")


def test_nvidia_egress_host_allowlisted():
    assert "integrate.api.nvidia.com" in VEKTOR_EGRESS_ALLOWLIST
    # api.nvidia.com is a DIFFERENT host and stays off the list.
    assert "api.nvidia.com" not in VEKTOR_EGRESS_ALLOWLIST


# ---------------------------------------------------------------------------
# provider_models() / /api/state
# ---------------------------------------------------------------------------

def test_nvidia_unconfigured_without_key():
    state = get_state()
    nvidia = next(m for m in state["models"] if m["id"] == "nvidia")
    assert nvidia["configured"] is False
    assert nvidia["key_env"] == "NVIDIA_NIM_KEYS"
    assert nvidia["api_url"] == NVIDIA_ENDPOINT
    assert nvidia["models"] == NVIDIA_FREE_MODELS


def test_nvidia_configured_with_key(monkeypatch):
    monkeypatch.setenv("NVIDIA_NIM_KEYS", "nv1")
    entry = next(m for m in provider_models() if m["id"] == "nvidia")
    assert entry["configured"] is True
    assert entry["model"] == NVIDIA_DEFAULT
    state = get_state()
    assert next(m for m in state["models"]
                if m["id"] == "nvidia")["configured"] is True


# ---------------------------------------------------------------------------
# Chat: mocked calls, no network, no real key
# ---------------------------------------------------------------------------

def test_nvidia_chat_mocked_success(monkeypatch):
    monkeypatch.setenv("NVIDIA_NIM_KEYS", "nv1,nv2")
    set_pool(monkeypatch, "nvidia", ["nv1", "nv2"])
    h, calls = do_chat(monkeypatch, {
        "messages": [{"role": "user", "content": "hi"}], "model": "nvidia"})
    assert h._status == 200
    out = response_json(h)
    assert out["ok"] is True
    assert out["provider"] == "NVIDIA NIM"
    assert out["model"] == NVIDIA_DEFAULT
    assert len(calls) == 1
    assert calls[0].full_url == NVIDIA_ENDPOINT
    assert calls[0].headers["Authorization"] == "Bearer nv1"
    sent = json.loads(calls[0].data.decode())
    assert sent["model"] == NVIDIA_DEFAULT


def test_nvidia_chat_model_switch_sends_free_model(monkeypatch):
    monkeypatch.setenv("NVIDIA_NIM_KEYS", "nv1")
    set_pool(monkeypatch, "nvidia", ["nv1"])
    h, calls = do_chat(monkeypatch, {
        "messages": [{"role": "user", "content": "hi"}],
        "model": "nvidia", "model_name": "z-ai/glm-5.3-flash"})
    assert h._status == 200
    assert response_json(h)["model"] == "z-ai/glm-5.3-flash"
    sent = json.loads(calls[0].data.decode())
    assert sent["model"] == "z-ai/glm-5.3-flash"


def test_nvidia_chat_without_key_is_honest_400(monkeypatch):
    h, calls = do_chat(monkeypatch, {
        "messages": [{"role": "user", "content": "hi"}], "model": "nvidia"})
    assert h._status == 400
    assert "NVIDIA NIM is not configured" in response_json(h)["error"]
    assert calls == []  # no provider was ever contacted


def test_nvidia_chat_paid_model_rejected(monkeypatch):
    monkeypatch.setenv("NVIDIA_NIM_KEYS", "nv1")
    set_pool(monkeypatch, "nvidia", ["nv1"])
    h = make_handler("POST", "/api/model",
                     {"model": "nvidia", "model_name": "gpt-4o"})
    h.do_POST()
    assert h._status == 400


# ---------------------------------------------------------------------------
# Fallback: Groq 429 rotates to NVIDIA (mid-cascade, after openrouter
# which is unconfigured here and skipped)
# ---------------------------------------------------------------------------

def test_fallback_rotates_from_groq_to_nvidia(monkeypatch):
    monkeypatch.setenv("GROQ_KEYS", "k-groq-dead")
    monkeypatch.setenv("NVIDIA_NIM_KEYS", "nv-good")
    set_pool(monkeypatch, "groq", ["k-groq-dead"])
    set_pool(monkeypatch, "nvidia", ["nv-good"])

    calls = []

    def fake_urlopen(req, timeout=120):
        calls.append(req)
        if "api.groq.com" in req.full_url:
            raise http_error(req.full_url, 429, "Too Many Requests")
        return FakeResponse(llm_reply("from nvidia"))

    monkeypatch.setattr(app, "urlopen", fake_urlopen)
    h = make_handler("POST", "/api/chat", {
        "messages": [{"role": "user", "content": "hi"}], "model": "groq"})
    h.do_POST()

    assert h._status == 200
    out = response_json(h)
    assert out["ok"] is True
    assert out["provider"] == "NVIDIA NIM"
    assert out["model"] == NVIDIA_DEFAULT
    assert out["message"] == "from nvidia"
    assert [urlparse(c.full_url).netloc for c in calls] == [
        "api.groq.com", "integrate.api.nvidia.com"]
