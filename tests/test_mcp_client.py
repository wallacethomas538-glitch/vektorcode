"""Universal MCP client tests (2026-11-07, Wallace's order): the generic
streamable-HTTP + SSE client, the Composio compatibility shim, and the
MCP-only egress hosts. Lab tests only: a mock MCP server runs on local
HTTP (stdlib http.server); no live Render, no live Supabase, no live
DeepWiki/Composio.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from vektor_cloud import composio_mcp, mcp_client
from vektor_cloud.egress import EgressBlocked, guard_url, load_mcp_hosts
from vektor_cloud.mcp_client import MCPError, MCPServerConfig


# ---------------------------------------------------------------------------
# Mock MCP server: streamable HTTP on /mcp, legacy SSE on /sse + /messages
# ---------------------------------------------------------------------------

DEFAULT_TOOLS = [
    {"name": "read_widget", "description": "Read a widget",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "search_widgets", "description": "Search widgets",
     "inputSchema": {"type": "object",
                     "properties": {"q": {"type": "string"}}}},
    {"name": "delete_widget", "description": "Delete a widget",
     "inputSchema": {"type": "object", "properties": {}}},
]

COMPOSIO_META_TOOLS = [
    {"name": "COMPOSIO_SEARCH_TOOLS", "description": "Search Composio tools",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "COMPOSIO_MULTI_EXECUTE_TOOL", "description": "Run tools",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "COMPOSIO_PROXY_EXECUTE", "description": "Run one tool",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "COMPOSIO_GET_TOOL_SCHEMAS", "description": "Tool schemas",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "COMPOSIO_MANAGE_CONNECTIONS",
     "description": "Manage connections",
     "inputSchema": {"type": "object", "properties": {}}},
]


class MockMCPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    tools = list(DEFAULT_TOOLS)
    require_auth = None      # (header, value) or None
    fail_with = None         # HTTP status or None
    call_text = None         # override the tools/call result text or None
    calls = []               # (tool_name, arguments) for tools/call
    requests = []            # (method, path) for every request
    sse_clients = {}         # session_id -> GET handler
    sse_lock = threading.Lock()

    def log_message(self, *_):
        pass

    def _json(self, status, payload, ctype="application/json", headers=None):
        raw = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def _check_auth(self):
        auth = type(self).require_auth
        if auth is None:
            return True
        header, value = auth
        return self.headers.get(header) == value

    def _rpc(self, payload):
        method = payload.get("method")
        rid = payload.get("id")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "serverInfo": {"name": "MockMCP", "version": "1.0"}}}
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": rid,
                    "result": {"tools": list(type(self).tools)}}
        if method == "tools/call":
            params = payload.get("params", {})
            type(self).calls.append(
                (params.get("name"), params.get("arguments") or {}))
            text = (type(self).call_text
                    or ("called " + str(params.get("name"))))
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": text}],
                "isError": False}}
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": "Method not found"}}

    @staticmethod
    def _sse_event(obj):
        return ("event: message\ndata: " + json.dumps(obj) + "\n\n").encode()

    def do_POST(self):
        type(self).requests.append(("POST", self.path))
        if type(self).fail_with:
            self._json(type(self).fail_with, {"error": "mock failure"})
            return
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if not self._check_auth():
            self._json(401, {"error": "unauthorized"})
            return
        if self.path.startswith("/messages"):
            # SSE transport: push the result onto the held GET stream.
            result = self._rpc(payload)
            if result is not None:
                session_id = parse_qs(
                    urlparse(self.path).query).get("session_id", [""])[0]
                with type(self).sse_lock:
                    client = type(self).sse_clients.get(session_id)
                if client is not None:
                    try:
                        client.wfile.write(self._sse_event(result))
                        client.wfile.flush()
                    except OSError:
                        pass
            self._json(202, None)
            return
        result = self._rpc(payload)
        if result is None:
            self._json(202, None)
            return
        headers = {}
        if payload.get("method") == "initialize":
            headers["Mcp-Session-Id"] = "mock-session-1"
        raw = self._sse_event(result)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        type(self).requests.append(("GET", self.path))
        if not self.path.startswith("/sse"):
            self.send_error(404)
            return
        if not self._check_auth():
            self._json(401, {"error": "unauthorized"})
            return
        session_id = f"sse-{time.time_ns()}"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(
            f"event: endpoint\ndata: /messages?session_id={session_id}\n\n"
            .encode())
        self.wfile.flush()
        with type(self).sse_lock:
            type(self).sse_clients[session_id] = self
        try:
            while not self.server.stop_event.is_set():
                time.sleep(0.05)
        finally:
            with type(self).sse_lock:
                type(self).sse_clients.pop(session_id, None)


@pytest.fixture
def mock_server():
    MockMCPHandler.tools = list(DEFAULT_TOOLS)
    MockMCPHandler.require_auth = None
    MockMCPHandler.fail_with = None
    MockMCPHandler.call_text = None
    MockMCPHandler.calls = []
    MockMCPHandler.requests = []
    MockMCPHandler.sse_clients = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockMCPHandler)
    server.daemon_threads = True
    server.stop_event = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.stop_event.set()
    server.shutdown()
    server.server_close()
    mcp_client.reset_all()


def mock_url(server, path="/mcp"):
    return f"http://127.0.0.1:{server.server_address[1]}{path}"


def streamable_config(server, **kw):
    cfg = {"id": "mock", "url": mock_url(server),
           "transport": "streamable-http"}
    cfg.update(kw)
    return MCPServerConfig(**cfg)


def sse_config(server, **kw):
    cfg = {"id": "mock", "url": mock_url(server, "/sse"),
           "transport": "sse"}
    cfg.update(kw)
    return MCPServerConfig(**cfg)


def dead_port():
    """A loopback port with nothing listening on it."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockMCPHandler)
    port = server.server_address[1]
    server.server_close()
    return port


# ---------------------------------------------------------------------------
# 1. Streamable HTTP: handshake, list, call
# ---------------------------------------------------------------------------

def test_streamable_handshake_list_call(mock_server):
    cfg = streamable_config(mock_server)
    assert mcp_client.configured(cfg) is True
    tools = mcp_client.list_tools(cfg)
    assert [t["name"] for t in tools] == [
        "read_widget", "search_widgets", "delete_widget"]
    # initialize + notifications/initialized + tools/list.
    assert len(MockMCPHandler.requests) == 3
    result = mcp_client.call_tool(cfg, "read_widget", {"id": 7})
    assert result["ok"] is True
    assert "read_widget" in json.dumps(result["result"])
    assert MockMCPHandler.calls == [("read_widget", {"id": 7})]
    # The session was reused: the call added one request, no new handshake.
    assert len(MockMCPHandler.requests) == 4


def test_status_caches_then_force_refreshes(mock_server):
    cfg = streamable_config(mock_server)
    first = mcp_client.status(cfg, force=True)
    assert first["connected"] is True
    assert first["tool_count"] == 3
    assert first["protocol"] == "2025-03-26"
    seen = len(MockMCPHandler.requests)
    second = mcp_client.status(cfg)
    assert second == first
    assert len(MockMCPHandler.requests) == seen  # cached: no new requests
    mcp_client.status(cfg, force=True)
    assert len(MockMCPHandler.requests) > seen


def test_sse_handshake_list_call(mock_server):
    cfg = sse_config(mock_server)
    tools = mcp_client.list_tools(cfg)
    assert [t["name"] for t in tools] == [
        "read_widget", "search_widgets", "delete_widget"]
    result = mcp_client.call_tool(cfg, "search_widgets", {"q": "x"})
    assert result["ok"] is True
    assert MockMCPHandler.calls == [("search_widgets", {"q": "x"})]
    assert mcp_client.status(cfg)["connected"] is True


# ---------------------------------------------------------------------------
# 2. Failure modes: bad URL, wrong auth, server down
# ---------------------------------------------------------------------------

def test_bad_url_refused_before_any_network_call(monkeypatch):
    cfg = MCPServerConfig(id="evil", url="https://evil.example.com/mcp")
    calls = []

    def boom(*args, **kwargs):
        calls.append(1)
        raise AssertionError("no network call may happen for a bad URL")

    monkeypatch.setattr(mcp_client, "urlopen", boom)
    status = mcp_client.status(cfg)
    assert status["connected"] is False
    assert "allowlist" in (status["error"] or "")
    assert calls == []
    with pytest.raises(EgressBlocked):
        mcp_client.mcp_url(cfg)


def test_wrong_auth_401_then_right_key(mock_server, monkeypatch):
    MockMCPHandler.require_auth = ("x-api-key", "secret123")
    cfg = streamable_config(mock_server, auth_kind="api-key",
                            auth_env="MOCK_KEY")
    monkeypatch.setenv("MOCK_KEY", "wrong-key")
    status = mcp_client.status(cfg, force=True)
    assert status["connected"] is False
    assert "401" in (status["error"] or "")
    assert mcp_client.call_tool(cfg, "read_widget", {})["ok"] is False
    # The right key connects; the chat path must never be blocked by this.
    monkeypatch.setenv("MOCK_KEY", "secret123")
    status = mcp_client.status(cfg, force=True)
    assert status["connected"] is True
    assert status["tool_count"] == 3


def test_server_down_status_false_and_call_fails():
    cfg = MCPServerConfig(id="dead",
                          url=f"http://127.0.0.1:{dead_port()}/mcp")
    status = mcp_client.status(cfg)
    assert status["connected"] is False
    assert status["error"]
    assert mcp_client.call_tool(cfg, "read_widget", {})["ok"] is False


def test_unconfigured_server_never_touches_network(monkeypatch):
    cfg = MCPServerConfig(id="nourl", url="")
    calls = []

    def boom(*args, **kwargs):
        calls.append(1)
        raise AssertionError("no network call for an unconfigured server")

    monkeypatch.setattr(mcp_client, "urlopen", boom)
    status = mcp_client.status(cfg)
    assert status["configured"] is False
    assert status["connected"] is False
    assert "not set" in (status["error"] or "")
    assert calls == []


# ---------------------------------------------------------------------------
# 3. Composio shim: same behaviour through the old import path
# ---------------------------------------------------------------------------

def test_shim_status_without_url(monkeypatch):
    monkeypatch.delenv("COMPOSIO_MCP_URL", raising=False)
    monkeypatch.delenv("MCP_SERVER_URL", raising=False)
    composio_mcp.reset()
    result = composio_mcp.status(force=True)
    assert result["configured"] is False
    assert result["connected"] is False


def test_shim_full_handshake_and_tool_list(monkeypatch):
    monkeypatch.setenv("COMPOSIO_MCP_URL", "https://connect.composio.dev/mcp")
    monkeypatch.setenv("COMPOSIO_API_KEY", "test-key-not-real")
    calls = []

    def fake_post(cfg, payload, session_id=None, timeout=20):
        calls.append(payload["method"])
        method = payload["method"]
        if method == "initialize":
            return ({"jsonrpc": "2.0", "id": payload["id"],
                     "result": {"protocolVersion": "2025-03-26",
                                "capabilities": {}}}, "session-123")
        if method == "notifications/initialized":
            return None, None
        if method == "tools/list":
            return ({"jsonrpc": "2.0", "id": payload["id"],
                     "result": {"tools": [{"name": "gmail_send"},
                                          {"name": "github_search"}]}},
                    None)
        raise AssertionError(method)

    monkeypatch.setattr(mcp_client, "_post_http", fake_post)
    composio_mcp.reset()
    result = composio_mcp.status(force=True)

    assert result["connected"] is True
    assert result["tool_count"] == 2
    assert result["tools"] == ["gmail_send", "github_search"]
    assert result["protocol"] == "2025-03-26"
    assert calls[:3] == ["initialize", "notifications/initialized",
                         "tools/list"]


def test_shim_call_tool_uses_mcp_tools_call(monkeypatch):
    monkeypatch.setenv("COMPOSIO_MCP_URL", "https://connect.composio.dev/mcp")
    monkeypatch.setenv("COMPOSIO_API_KEY", "test-key-not-real")
    seen = []

    def fake_post(cfg, payload, session_id=None, timeout=20):
        method = payload["method"]
        if method == "initialize":
            return ({"jsonrpc": "2.0", "id": payload["id"],
                     "result": {"protocolVersion": "2025-03-26"}},
                    "session-456")
        if method == "notifications/initialized":
            return None, None
        if method == "tools/call":
            seen.append(payload["params"])
            return ({"jsonrpc": "2.0", "id": payload["id"],
                     "result": {"content": [{"type": "text",
                                             "text": "ok"}]}}, None)
        raise AssertionError(method)

    monkeypatch.setattr(mcp_client, "_post_http", fake_post)
    composio_mcp.reset()
    result = composio_mcp.call_tool("gmail_send", {"to": "a@b.c"})

    assert result["ok"] is True
    assert seen == [{"name": "gmail_send", "arguments": {"to": "a@b.c"}}]


# ---------------------------------------------------------------------------
# 4. Egress: registry hosts are MCP-only (mechanism B)
# ---------------------------------------------------------------------------

def test_egress_extra_hosts_mcp_only():
    url = "https://mcp.example.com/mcp"
    with pytest.raises(EgressBlocked):
        guard_url(url)
    # With the registry host added, the MCP guard passes.
    guard_url(url, extra_hosts=frozenset({"mcp.example.com"}))
    # The web path never sees registry hosts.
    from vektor_cloud.egress import is_allowed
    assert is_allowed("mcp.example.com") is False
    assert is_allowed("mcp.example.com",
                      extra_hosts=frozenset({"mcp.example.com"})) is True


def test_load_mcp_hosts_never_raises():
    class ExplodingStore:
        def mcp_host_list(self):
            raise RuntimeError("db down")

    assert load_mcp_hosts(ExplodingStore()) == frozenset()

    class GoodStore:
        def mcp_host_list(self):
            return ["MCP.Example.COM", "other.example"]

    assert load_mcp_hosts(GoodStore()) == {"mcp.example.com",
                                           "other.example"}
