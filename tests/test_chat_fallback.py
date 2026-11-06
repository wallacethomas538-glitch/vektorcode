"""Cross-provider fallback for /api/chat.

Wallace 2026-11-06: "LLM should rotate until a good key is approved."
When the selected provider's call fails with a rotatable status
(400/401/403/404/429/500/502/503/504) or the provider is unreachable,
chat tries the next configured provider in FALLBACK_ORDER until one
answers. Free-tier models only. 400/404 rotate too: a retired model id
answers 404 on Groq and 400 on Gemini, and the next provider may still
be alive. 503 rotates too: one overloaded provider must never kill the
whole chain (found live 2026-11-06 — every provider 503'd and the chain
aborted on the first one).

Uses the same bare-Handler harness as test_qwen_models (no socket).
"""
import io
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

import pytest

import app
from app import FALLBACK_ORDER, FREE_MODELS, QWEN_ROUTES


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


def http_403(url):
    return HTTPError(url, 403, "Forbidden", {}, None)


def http_error(url, code, msg):
    return HTTPError(url, code, msg, {}, None)


# ---------------------------------------------------------------------------
# 400/404 rotate too: a retired model id answers 404 on Groq, 400 on
# Gemini — the chain must move to the next provider, not abort.
# ---------------------------------------------------------------------------

def test_fallback_rotates_on_404_to_next_provider(monkeypatch):
    monkeypatch.setenv("GROQ_KEYS", "k-groq-dead")
    monkeypatch.setenv("OPENROUTER_KEYS", "k-or-good")
    set_pool(monkeypatch, "groq", ["k-groq-dead"])
    set_pool(monkeypatch, "openrouter", ["k-or-good"])

    calls = []

    def fake_urlopen(req, timeout=120):
        calls.append(req)
        if "api.groq.com" in req.full_url:
            raise http_error(req.full_url, 404, "Not Found")
        return FakeResponse(llm_reply("from openrouter"))

    monkeypatch.setattr(app, "urlopen", fake_urlopen)
    h = make_handler("POST", "/api/chat", {
        "messages": [{"role": "user", "content": "hi"}], "model": "groq"})
    h.do_POST()

    assert h._status == 200
    out = response_json(h)
    assert out["ok"] is True
    assert out["provider"] == "OpenRouter"
    assert out["message"] == "from openrouter"
    assert [urlparse(c.full_url).netloc for c in calls] == [
        "api.groq.com", "openrouter.ai"]


def test_fallback_rotates_on_400_to_next_provider(monkeypatch):
    monkeypatch.setenv("GEMINI_KEYS", "k-gemini-dead")
    monkeypatch.setenv("OPENROUTER_KEYS", "k-or-good")
    set_pool(monkeypatch, "gemini", ["k-gemini-dead"])
    set_pool(monkeypatch, "openrouter", ["k-or-good"])

    calls = []

    def fake_urlopen(req, timeout=120):
        calls.append(req)
        if "generativelanguage.googleapis.com" in req.full_url:
            raise http_error(req.full_url, 400, "Bad Request")
        return FakeResponse(llm_reply("from openrouter"))

    monkeypatch.setattr(app, "urlopen", fake_urlopen)
    h = make_handler("POST", "/api/chat", {
        "messages": [{"role": "user", "content": "hi"}], "model": "gemini"})
    h.do_POST()

    assert h._status == 200
    out = response_json(h)
    assert out["ok"] is True
    assert out["provider"] == "OpenRouter"
    assert out["message"] == "from openrouter"
    assert [urlparse(c.full_url).netloc for c in calls] == [
        "generativelanguage.googleapis.com", "openrouter.ai"]


def test_stream_fallback_rotates_on_404_to_next_provider(monkeypatch):
    monkeypatch.setenv("GROQ_KEYS", "k-groq-dead")
    monkeypatch.setenv("OPENROUTER_KEYS", "k-or-good")
    set_pool(monkeypatch, "groq", ["k-groq-dead"])
    set_pool(monkeypatch, "openrouter", ["k-or-good"])

    calls = []

    def fake_urlopen(req, timeout=120):
        calls.append(req)
        if "api.groq.com" in req.full_url:
            raise http_error(req.full_url, 404, "Not Found")
        return FakeResponse(llm_reply("streamed ok"))

    monkeypatch.setattr(app, "urlopen", fake_urlopen)
    h = make_handler("POST", "/api/chat", {
        "messages": [{"role": "user", "content": "hi"}],
        "model": "groq", "stream": True})
    h.do_POST()

    assert h._status == 200
    body = h.wfile.getvalue().decode()
    assert "streamed ok" in body
    assert '"provider": "OpenRouter"' in body
    assert [urlparse(c.full_url).netloc for c in calls] == [
        "api.groq.com", "openrouter.ai"]


# ---------------------------------------------------------------------------
# 503 rotates too: one overloaded provider must never kill the chain.
# Found live 2026-11-06 — Groq, Gemini, OpenRouter, Meta and Together all
# answered 503 and the old code aborted on the first one.
# ---------------------------------------------------------------------------

def test_fallback_rotates_on_503_to_next_provider(monkeypatch):
    monkeypatch.setenv("GROQ_KEYS", "k-groq-busy")
    monkeypatch.setenv("OPENROUTER_KEYS", "k-or-good")
    set_pool(monkeypatch, "groq", ["k-groq-busy"])
    set_pool(monkeypatch, "openrouter", ["k-or-good"])

    calls = []

    def fake_urlopen(req, timeout=120):
        calls.append(req)
        if "api.groq.com" in req.full_url:
            raise http_error(req.full_url, 503, "Service Unavailable")
        return FakeResponse(llm_reply("from openrouter"))

    monkeypatch.setattr(app, "urlopen", fake_urlopen)
    h = make_handler("POST", "/api/chat", {
        "messages": [{"role": "user", "content": "hi"}], "model": "groq"})
    h.do_POST()

    assert h._status == 200
    out = response_json(h)
    assert out["ok"] is True
    assert out["provider"] == "OpenRouter"
    assert [urlparse(c.full_url).netloc for c in calls] == [
        "api.groq.com", "openrouter.ai"]


def test_stream_fallback_rotates_on_503_to_next_provider(monkeypatch):
    monkeypatch.setenv("GROQ_KEYS", "k-groq-busy")
    monkeypatch.setenv("OPENROUTER_KEYS", "k-or-good")
    set_pool(monkeypatch, "groq", ["k-groq-busy"])
    set_pool(monkeypatch, "openrouter", ["k-or-good"])

    calls = []

    def fake_urlopen(req, timeout=120):
        calls.append(req)
        if "api.groq.com" in req.full_url:
            raise http_error(req.full_url, 503, "Service Unavailable")
        return FakeResponse(llm_reply("streamed ok"))

    monkeypatch.setattr(app, "urlopen", fake_urlopen)
    h = make_handler("POST", "/api/chat", {
        "messages": [{"role": "user", "content": "hi"}],
        "model": "groq", "stream": True})
    h.do_POST()

    assert h._status == 200
    body = h.wfile.getvalue().decode()
    assert "streamed ok" in body
    assert '"provider": "OpenRouter"' in body
    assert [urlparse(c.full_url).netloc for c in calls] == [
        "api.groq.com", "openrouter.ai"]


def test_provider_error_body_is_included_in_detail():
    # The body is the diagnosis ('model overloaded' vs 'no provider'),
    # so the error string must carry it, truncated.
    fp = io.BytesIO(b'{"error": {"message": "model overloaded, try again"}}')
    err = HTTPError("https://api.groq.com/openai/v1/chat/completions",
                    503, "Service Unavailable", {}, fp)
    detail = app.llm_http_error_detail(err)
    assert "503" in detail
    assert "model overloaded" in detail
    # A body-less error (fp None) must not crash the helper.
    assert "503" in app.llm_http_error_detail(
        HTTPError("https://x.test/", 503, "Service Unavailable", {}, None))


def test_all_provider_attempts_listed_in_failure(monkeypatch):
    # When every provider fails, the response lists each attempt with
    # its own error — one test run diagnoses all keys at once instead
    # of only showing the last provider's error.
    monkeypatch.setenv("GROQ_KEYS", "k-groq-dead")
    monkeypatch.setenv("OPENROUTER_KEYS", "k-or-dead")
    set_pool(monkeypatch, "groq", ["k-groq-dead"])
    set_pool(monkeypatch, "openrouter", ["k-or-dead"])
    set_pool(monkeypatch, "qwen", ["k-groq-dead"])

    def fake_urlopen(req, timeout=120):
        if "api.groq.com" in req.full_url:
            raise http_error(req.full_url, 503, "Service Unavailable")
        if "openrouter.ai" in req.full_url:
            raise http_error(req.full_url, 403, "Forbidden")
        if "127.0.0.1:11434" in req.full_url:
            raise URLError("connection refused")
        raise http_error(req.full_url, 503, "Service Unavailable")

    monkeypatch.setattr(app, "urlopen", fake_urlopen)
    h = make_handler("POST", "/api/chat", {
        "messages": [{"role": "user", "content": "hi"}], "model": "groq"})
    h.do_POST()

    assert h._status == 502
    out = response_json(h)
    assert out["ok"] is False
    attempts = out["attempts"]
    # Groq, OpenRouter, Qwen-via-Groq, then Local (unreachable, always
    # configured off-Render). Meta/Together/Gemini unconfigured: skipped.
    assert [a["provider"] for a in attempts] == [
        "Groq", "OpenRouter", "Qwen", "Local (Ollama)"]
    assert "503" in attempts[0]["error"]
    assert "403" in attempts[1]["error"]
    assert attempts[0]["model"] == FREE_MODELS["groq"][0]


def test_keys_are_stripped_before_use(monkeypatch):
    # Keys pasted into Render with spaces/newlines around them (e.g.
    # "key1, key2, " or a trailing newline) used to be sent verbatim:
    # "Bearer  sk-..." reads as a missing auth header to OpenRouter,
    # and Google rejects the padded key as invalid. Pools strip.
    set_pool(monkeypatch, "groq", ["  gsk-padded  ", "", "gsk-clean"])
    assert app.get_next_key("groq") == "gsk-padded"
    assert app.get_next_key("groq") == "gsk-clean"
    keys, _ = app._provider_keys(None, "groq")
    assert keys == ["gsk-padded", "gsk-clean"]


def test_chat_requests_send_a_browser_user_agent(monkeypatch):
    # Groq and Together sit behind Cloudflare, which rejects the
    # default Python-urllib user agent with 403 / error code 1010.
    # Every LLM request must carry a browser user agent instead.
    set_pool(monkeypatch, "groq", ["k-groq"])
    monkeypatch.setenv("GROQ_KEYS", "k-groq")
    seen = {}

    def fake_urlopen(req, timeout=120):
        seen["ua"] = req.get_header("User-agent")
        seen["auth"] = req.get_header("Authorization")
        return FakeResponse({"choices": [{"message": {"content": "hello"}}]})

    monkeypatch.setattr(app, "urlopen", fake_urlopen)
    h = make_handler("POST", "/api/chat", {
        "messages": [{"role": "user", "content": "hi"}], "model": "groq"})
    h.do_POST()

    assert response_json(h)["ok"] is True
    assert seen["ua"] and "Mozilla" in seen["ua"]
    assert "Python-urllib" not in seen["ua"]
    assert seen["auth"] == "Bearer k-groq"


# ---------------------------------------------------------------------------
# Live model IDs only: retired Groq/Gemini ids must be gone (2026-11-06).
# Groq retired qwen/qwen3-32b (2026-07-17) and llama-3.1-8b-instant +
# llama-3.3-70b-versatile (2026-08-16); Google dropped gemini-2.0-*.
# ---------------------------------------------------------------------------

def test_groq_free_models_contain_only_live_ids():
    retired = {"qwen/qwen3-32b", "llama-3.3-70b-versatile",
               "llama-3.1-8b-instant",
               "meta-llama/llama-4-scout-17b-16e-instruct"}
    assert not (retired & set(FREE_MODELS["groq"]))
    assert FREE_MODELS["groq"] == ["qwen/qwen3.8-27b",
                                    "openai/gpt-oss-120b",
                                    "openai/gpt-oss-20b"]
    # Qwen still the default, just the live id; routes to Groq.
    assert FREE_MODELS["qwen"][0] == "qwen/qwen3.8-27b"
    assert QWEN_ROUTES.get("qwen/qwen3.8-27b") == "groq"
    assert "qwen/qwen3-32b" not in QWEN_ROUTES


def test_gemini_free_models_contain_no_retired_2_0_models():
    assert not any(m.startswith("gemini-2.0")
                   for m in FREE_MODELS["gemini"])
    assert FREE_MODELS["gemini"] == ["gemini-3.8-flash", "gemini-3.6-flash",
                                     "gemini-3.5-flash-lite",
                                     "gemini-2.5-flash"]


def test_openrouter_free_list_includes_crouton_alpha():
    # Crouton Alpha (stealth/crouton-alpha), released Nov 3 2026: free
    # ($0 in/out, preview), 1M context, built for coding/agentic/tool-use.
    # Added as an extra free option; the existing entries stay put.
    # Unit-test only — no live OpenRouter call is made here.
    assert "stealth/crouton-alpha" in FREE_MODELS["openrouter"]
    for existing in ("meta-llama/llama-3.3-70b-instruct:free",
                     "qwen/qwen3-32b:free", "deepseek/deepseek-r1:free",
                     "google/gemma-3-27b-it:free"):
        assert existing in FREE_MODELS["openrouter"]


def test_free_cascade_chat_models_use_live_ids():
    # The agent_api.py/CLI cascade (NOT the /api/chat path) still pointed
    # at retired ids: Groq llama-3.3-70b-versatile, Gemini gemini-2.0-flash,
    # Qwen qwen/qwen3-32b. They now use the same live free ids as app.py.
    from vektor_cloud.free_cascade import _CHAT_MODELS
    from vektor_cloud.free_providers import QWEN_GROQ_MODEL, qwen_model
    retired = {"qwen/qwen3-32b", "llama-3.3-70b-versatile",
               "llama-3.1-8b-instant", "gemini-2.0-flash"}
    assert not (retired & {m for m in _CHAT_MODELS.values() if m})
    assert _CHAT_MODELS["GROQ"] == "qwen/qwen3.8-27b"
    assert _CHAT_MODELS["GEMINI"] == "gemini-3.8-flash"
    assert QWEN_GROQ_MODEL == "qwen/qwen3.8-27b"
    assert qwen_model("groq") == "qwen/qwen3.8-27b"


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    old_db = app.DB
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    monkeypatch.delenv("VEKTORCODE_TOOL_PASSWORD", raising=False)
    monkeypatch.delenv("VEKTORCODE_SESSION_SECRET", raising=False)
    monkeypatch.delenv("GROQ_KEYS", raising=False)
    monkeypatch.delenv("OPENROUTER_KEYS", raising=False)
    monkeypatch.delenv("META_KEYS", raising=False)
    monkeypatch.delenv("TOGETHER_KEYS", raising=False)
    monkeypatch.delenv("GEMINI_KEYS", raising=False)
    monkeypatch.delenv("MISTRAL_KEYS", raising=False)
    monkeypatch.delenv("HUGGINGFACE_KEYS", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("QWEN_KEYS", raising=False)
    app.DB = tmp_path / "t.db"
    app._STORES.clear()
    yield
    app.DB = old_db
    app._STORES.clear()


def set_pool(monkeypatch, pid, keys):
    monkeypatch.setitem(app.KEY_POOLS, pid, list(keys))
    monkeypatch.setitem(app.KEY_INDEX, pid, 0)


# ---------------------------------------------------------------------------
# Fallback order
# ---------------------------------------------------------------------------

def test_fallback_order_is_free_providers_only():
    assert FALLBACK_ORDER == ["groq", "openrouter", "meta", "together",
                              "qwen", "gemini", "mistral", "huggingface",
                              "local"]
    for pid in FALLBACK_ORDER:
        assert FREE_MODELS[pid], pid


# ---------------------------------------------------------------------------
# Non-streaming: provider 1 fails 403, provider 2 answers
# ---------------------------------------------------------------------------

def test_fallback_groq_403_to_openrouter(monkeypatch):
    monkeypatch.setenv("GROQ_KEYS", "k-groq-dead")
    monkeypatch.setenv("OPENROUTER_KEYS", "k-or-good")
    set_pool(monkeypatch, "groq", ["k-groq-dead"])
    set_pool(monkeypatch, "openrouter", ["k-or-good"])

    calls = []

    def fake_urlopen(req, timeout=120):
        calls.append(req)
        if "api.groq.com" in req.full_url:
            raise http_403(req.full_url)
        return FakeResponse(llm_reply("from openrouter"))

    monkeypatch.setattr(app, "urlopen", fake_urlopen)
    h = make_handler("POST", "/api/chat", {
        "messages": [{"role": "user", "content": "hi"}], "model": "groq"})
    h.do_POST()

    assert h._status == 200
    out = response_json(h)
    assert out["ok"] is True
    assert out["provider"] == "OpenRouter"  # the answering provider is reported
    assert out["message"] == "from openrouter"
    assert out["model"] == FREE_MODELS["openrouter"][0]  # free-tier model only
    assert [urlparse(c.full_url).netloc for c in calls] == [
        "api.groq.com", "openrouter.ai"]
    assert calls[1].headers["Authorization"] == "Bearer k-or-good"


def test_fallback_rotates_keys_within_provider_before_moving_on(monkeypatch):
    # Groq has two keys: first dead, second good. Fallback must try the
    # second key before ever leaving Groq ("rotate until a good key").
    monkeypatch.setenv("GROQ_KEYS", "k-dead,k-good")
    set_pool(monkeypatch, "groq", ["k-dead", "k-good"])

    calls = []

    def fake_urlopen(req, timeout=120):
        calls.append(req)
        if req.headers.get("Authorization") == "Bearer k-dead":
            raise http_403(req.full_url)
        return FakeResponse(llm_reply("from groq key 2"))

    monkeypatch.setattr(app, "urlopen", fake_urlopen)
    h = make_handler("POST", "/api/chat", {
        "messages": [{"role": "user", "content": "hi"}], "model": "groq"})
    h.do_POST()

    assert h._status == 200
    out = response_json(h)
    assert out["ok"] is True
    assert out["provider"] == "Groq"
    assert out["message"] == "from groq key 2"
    assert len(calls) == 2
    assert all("api.groq.com" in c.full_url for c in calls)


def test_fallback_all_providers_fail_returns_last_error(monkeypatch):
    monkeypatch.setenv("GROQ_KEYS", "k-groq-dead")
    monkeypatch.setenv("OPENROUTER_KEYS", "k-or-dead")
    monkeypatch.setenv("META_KEYS", "k-meta-dead")
    monkeypatch.setenv("TOGETHER_KEYS", "k-tog-dead")
    set_pool(monkeypatch, "groq", ["k-groq-dead"])
    set_pool(monkeypatch, "openrouter", ["k-or-dead"])
    set_pool(monkeypatch, "meta", ["k-meta-dead"])
    set_pool(monkeypatch, "together", ["k-tog-dead"])
    set_pool(monkeypatch, "qwen", ["k-groq-dead"])

    calls = []

    def fake_urlopen(req, timeout=120):
        calls.append(req)
        if "127.0.0.1:11434" in req.full_url:
            raise URLError("connection refused")
        raise http_403(req.full_url)

    monkeypatch.setattr(app, "urlopen", fake_urlopen)
    h = make_handler("POST", "/api/chat", {
        "messages": [{"role": "user", "content": "hi"}], "model": "groq"})
    h.do_POST()

    assert h._status == 502
    out = response_json(h)
    assert out["ok"] is False
    # Same error shape as a single-provider failure, so the UI still works.
    assert out["retryable"] is True
    assert "session_id" in out
    # Every configured provider was attempted, in fallback order.
    hosts = [urlparse(c.full_url).netloc for c in calls]
    assert hosts[0] == "api.groq.com"
    assert "openrouter.ai" in hosts
    assert "api.llama.com" in hosts
    assert "api.together.xyz" in hosts
    assert hosts.count("api.groq.com") == 2  # groq, then qwen via groq
    assert hosts[-1] == "127.0.0.1:11434"  # local last, unreachable
    # The last provider's error is what comes back.
    assert out["error"].startswith("LLM unavailable")


def test_fallback_skips_unconfigured_providers(monkeypatch):
    # Only Groq is configured (and dead) — no other provider should be
    # attempted except qwen-via-groq (same dead key) and local, which is
    # always configured off-Render.
    monkeypatch.setenv("GROQ_KEYS", "k-groq-dead")
    set_pool(monkeypatch, "groq", ["k-groq-dead"])
    set_pool(monkeypatch, "qwen", ["k-groq-dead"])

    calls = []

    def fake_urlopen(req, timeout=120):
        calls.append(req)
        if "127.0.0.1:11434" in req.full_url:
            raise URLError("connection refused")
        raise http_403(req.full_url)

    monkeypatch.setattr(app, "urlopen", fake_urlopen)
    h = make_handler("POST", "/api/chat", {
        "messages": [{"role": "user", "content": "hi"}], "model": "groq"})
    h.do_POST()

    assert h._status == 502
    hosts = [urlparse(c.full_url).netloc for c in calls]
    # groq, then qwen-via-groq (same dead key), then local. Nothing else.
    assert hosts == ["api.groq.com", "api.groq.com", "127.0.0.1:11434"]


# ---------------------------------------------------------------------------
# Streaming: same fallback before committing to SSE
# ---------------------------------------------------------------------------

def test_stream_fallback_groq_403_to_openrouter(monkeypatch):
    monkeypatch.setenv("GROQ_KEYS", "k-groq-dead")
    monkeypatch.setenv("OPENROUTER_KEYS", "k-or-good")
    set_pool(monkeypatch, "groq", ["k-groq-dead"])
    set_pool(monkeypatch, "openrouter", ["k-or-good"])

    calls = []

    def fake_urlopen(req, timeout=120):
        calls.append(req)
        if "api.groq.com" in req.full_url:
            raise http_403(req.full_url)
        return FakeResponse(llm_reply("streamed ok"))

    monkeypatch.setattr(app, "urlopen", fake_urlopen)
    h = make_handler("POST", "/api/chat", {
        "messages": [{"role": "user", "content": "hi"}],
        "model": "groq", "stream": True})
    h.do_POST()

    assert h._status == 200
    body = h.wfile.getvalue().decode()
    assert "streamed ok" in body
    assert '"provider": "OpenRouter"' in body
    assert [urlparse(c.full_url).netloc for c in calls] == [
        "api.groq.com", "openrouter.ai"]


def test_stream_rotates_keys_within_provider_before_moving_on(monkeypatch):
    # Streaming had the same bug _chat_once already fixed: the request
    # block sat OUTSIDE the key loop, so streaming only ever tried the
    # LAST key per provider. Groq has two keys: first dead, second good.
    # Streaming must try the second key before ever leaving Groq.
    monkeypatch.setenv("GROQ_KEYS", "k-dead,k-good")
    set_pool(monkeypatch, "groq", ["k-dead", "k-good"])

    calls = []

    def fake_urlopen(req, timeout=120):
        calls.append(req)
        if req.headers.get("Authorization") == "Bearer k-dead":
            raise http_403(req.full_url)
        return FakeResponse(llm_reply("from groq key 2"))

    monkeypatch.setattr(app, "urlopen", fake_urlopen)
    h = make_handler("POST", "/api/chat", {
        "messages": [{"role": "user", "content": "hi"}],
        "model": "groq", "stream": True})
    h.do_POST()

    assert h._status == 200
    body = h.wfile.getvalue().decode()
    assert "from groq key 2" in body
    assert '"provider": "Groq"' in body
    assert len(calls) == 2
    assert all("api.groq.com" in c.full_url for c in calls)
