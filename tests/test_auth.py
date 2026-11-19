"""Tests for authentication enforcement (Hypit auth follow-up, 2026-11-18).

Covers Big Chat's follow-up items 1-3 and 5, plus the second
follow-up (2026-11-18, commit 71b1d7f4 review):
- fail-closed behavior when VEKTORCODE_TOOL_PASSWORD is missing
  (open only for local dev; refused on Render),
- minimal /api/state for unauthenticated callers,
- unauthorized (no-cookie) access to every protected write/action
  endpoint, including POST /api/health, POST /api/media and POST
  /api/tool for a NON-risky tool (file_ops is the only one),
- missing-password + RENDER=true refusal for those endpoints too,
- legitimate authenticated behavior still works on them,
- the login flow and the Secure cookie flag.

Pattern follows tests/test_shell_tool.py: a live in-process server.
Auth is read from os.environ per request, so tests set/unset
VEKTORCODE_TOOL_PASSWORD and RENDER around each case and restore
them afterwards.
"""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import app

PASSWORD = "test-tool-password"


def _http(port, method, path, payload=None, headers=None):
    """Returns (status, response-headers, parsed-body)."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method,
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.headers, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, json.loads(exc.read() or b"{}")


# (method, path, payload) for every protected write/action endpoint.
# The auth check runs before payload validation, so minimal bodies 401.
# /api/tool needs a known RISKY tool: an unknown tool 400s before auth.
# file_ops is the only NON-risky tool: it must 401 too (follow-up 2).
PROTECTED_POST = [
    ("POST", "/api/chat", {"message": "hi", "session_id": "t"}),
    ("POST", "/api/shell/approve", {"approval_id": "no-such-id"}),
    ("POST", "/api/skill/save", {"title": "t", "content": "c"}),
    ("POST", "/api/session", {"title": "t"}),
    ("POST", "/api/session/delete", {"id": "x"}),
    ("POST", "/api/session/resume", {"id": "x"}),
    ("POST", "/api/session/expired", {}),
    ("POST", "/api/kill", {}),
    ("POST", "/api/model", {"model": "groq"}),
    ("POST", "/api/settings", {}),
    ("POST", "/api/tool", {"tool": "shell", "enabled": True}),
    ("POST", "/api/tool", {"tool": "file_ops", "enabled": True}),
    ("POST", "/api/health", {}),
    ("POST", "/api/media", {"kind": "image", "prompt": "a test image"}),
    ("POST", "/api/mcp", {"tool": "COMPOSIO_SEARCH_TOOLS"}),
    ("POST", "/api/mcp/servers", {"id": "x", "url": "https://example.com/mcp"}),
    ("POST", "/api/memory", {}),
    ("POST", "/api/memory/save", {"title": "t", "content": "c"}),
    ("POST", "/api/web-search", {"query": "test"}),
    ("POST", "/api/receipt", {"action": "test"}),
    ("POST", "/api/voice/token", {}),
]

PROTECTED_GET = [
    ("GET", "/api/shell/approvals", None),
    ("GET", "/api/mcp/servers", None),
    ("GET", "/api/session/x/messages", None),
    ("GET", "/api/memory", None),
]

PROTECTED_DELETE = [
    ("DELETE", "/api/mcp/servers", {"id": "x"}),
    ("DELETE", "/api/memory", {"kind": "fact", "key": "x"}),
]


class AuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.old_db, cls.old_state = app.DB, app.VEKTOR_STATE
        app.DB = Path(cls.tmp.name) / "test.db"
        app.VEKTOR_STATE = Path(cls.tmp.name) / "state"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        app.DB, app.VEKTOR_STATE = cls.old_db, cls.old_state
        cls.tmp.cleanup()

    def setUp(self):
        self._saved_env = {k: os.environ.get(k) for k in
                           ("VEKTORCODE_TOOL_PASSWORD", "RENDER")}
        os.environ.pop("VEKTORCODE_TOOL_PASSWORD", None)
        os.environ.pop("RENDER", None)
        app._SHELL_APPROVALS.clear()
        app._SHELL_CALL_COUNTS.clear()

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _call(self, method, path, payload=None, headers=None):
        return _http(self.port, method, path, payload, headers)

    def _login(self, password, headers=None):
        status, resp_headers, _ = self._call(
            "POST", "/api/login", {"password": password}, headers)
        cookie = resp_headers.get("Set-Cookie") or ""
        return status, cookie

    def test_protected_endpoints_401_without_cookie(self):
        """With a password set, every protected endpoint 401s with no cookie."""
        os.environ["VEKTORCODE_TOOL_PASSWORD"] = PASSWORD
        for method, path, payload in (PROTECTED_POST + PROTECTED_GET
                                      + PROTECTED_DELETE):
            status, _, body = self._call(method, path, payload)
            self.assertEqual(status, 401, f"{method} {path} -> {status}: {body}")

    def test_missing_password_fails_closed_on_render(self):
        """No password + RENDER: protected endpoints and login all refuse."""
        os.environ["RENDER"] = "true"
        status, _, body = self._call(
            "POST", "/api/chat", {"message": "hi", "session_id": "t"})
        self.assertEqual(status, 401, body)
        # Follow-up 2: the newly gated writes refuse here too, not just
        # with a configured password.
        for method, path, payload in [
                ("POST", "/api/health", {}),
                ("POST", "/api/media",
                 {"kind": "image", "prompt": "a test image"}),
                ("POST", "/api/tool", {"tool": "file_ops", "enabled": True}),
                ("POST", "/api/tool", {"tool": "shell", "enabled": True})]:
            status, _, body = self._call(method, path, payload)
            self.assertEqual(status, 401, f"{method} {path} -> {status}: {body}")
        status, _, _ = self._call(
            "POST", "/api/login", {"password": "anything"})
        self.assertEqual(status, 401)
        status, _, body = self._call("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(set(body.keys()), {"auth"})
        self.assertFalse(body["auth"]["enabled"])
        self.assertFalse(body["auth"]["authenticated"])
        self.assertTrue(body["auth"]["setup_required"])

    def test_authenticated_writes_still_work(self):
        """Logged in, the newly gated writes behave as before."""
        os.environ["VEKTORCODE_TOOL_PASSWORD"] = PASSWORD
        status, cookie = self._login(PASSWORD)
        self.assertEqual(status, 204)
        headers = {"Cookie": cookie.split(";")[0]}
        # Health refresh: only the local provider is configured in the
        # test env (no provider keys), so the probe is a fast local
        # connection-refused, not an outbound call.
        status, _, body = self._call("POST", "/api/health", {}, headers)
        self.assertEqual(status, 200, body)
        self.assertIn("health", body)
        self.assertIn("providers", body["health"])
        # Media: returns a provider URL; no outbound call is made.
        status, _, body = self._call(
            "POST", "/api/media", {"kind": "image", "prompt": "a cat"},
            headers)
        self.assertEqual(status, 200, body)
        self.assertTrue(body.get("url", "").startswith("https://"))
        # Non-risky tool toggle persists and shows in state.
        status, _, body = self._call(
            "POST", "/api/tool", {"tool": "file_ops", "enabled": True},
            headers)
        self.assertEqual(status, 200, body)
        status, _, body = self._call("GET", "/api/state", headers=headers)
        tools = {t["id"]: t for t in body.get("tools", [])}
        self.assertTrue(tools["file_ops"]["enabled"])

    def test_missing_password_open_locally(self):
        """No password + no RENDER (local dev): behavior stays open."""
        status, _, body = self._call(
            "POST", "/api/chat", {"message": "hi", "session_id": "t"})
        self.assertNotEqual(status, 401, body)
        status, _, body = self._call("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertIn("models", body)
        self.assertIn("sessions", body)
        self.assertIn("receipts", body)

    def test_state_minimal_unauthenticated(self):
        """With a password set, /api/state leaks nothing before login."""
        os.environ["VEKTORCODE_TOOL_PASSWORD"] = PASSWORD
        status, _, body = self._call("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(set(body.keys()), {"auth"})
        self.assertTrue(body["auth"]["enabled"])
        self.assertFalse(body["auth"]["authenticated"])
        self.assertFalse(body["auth"]["setup_required"])
        self.assertNotIn("sessions", body)
        self.assertNotIn("receipts", body)
        self.assertNotIn("models", body)

    def test_login_flow_and_cookie(self):
        """Wrong password 401s; right password sets a working cookie."""
        os.environ["VEKTORCODE_TOOL_PASSWORD"] = PASSWORD
        status, cookie = self._login("wrong-password")
        self.assertEqual(status, 401)
        self.assertNotIn("vc_auth=", cookie)
        status, cookie = self._login(PASSWORD)
        self.assertEqual(status, 204)
        self.assertIn("vc_auth=", cookie)
        headers = {"Cookie": cookie.split(";")[0]}
        status, _, body = self._call("GET", "/api/state", headers=headers)
        self.assertEqual(status, 200)
        self.assertTrue(body["auth"]["authenticated"])
        self.assertIn("models", body)
        self.assertIn("sessions", body)
        status, _, body = self._call(
            "POST", "/api/session", {"title": "t"}, headers)
        self.assertNotEqual(status, 401, body)

    def test_cookie_secure_flag(self):
        """Secure is set for HTTPS/Render logins, not for plain local HTTP."""
        os.environ["VEKTORCODE_TOOL_PASSWORD"] = PASSWORD
        _, cookie = self._login(
            PASSWORD, {"X-Forwarded-Proto": "https"})
        self.assertIn("; Secure", cookie)
        os.environ["RENDER"] = "true"
        _, cookie = self._login(PASSWORD)
        self.assertIn("; Secure", cookie)
        del os.environ["RENDER"]
        _, cookie = self._login(PASSWORD)
        self.assertNotIn("; Secure", cookie)


if __name__ == "__main__":
    unittest.main()
