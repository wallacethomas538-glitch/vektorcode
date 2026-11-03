"""MCP handshake tests with a fake transport (no network, no real key)."""
from vektor_cloud import composio_mcp


def test_status_without_url(monkeypatch):
    monkeypatch.delenv("COMPOSIO_MCP_URL", raising=False)
    monkeypatch.delenv("MCP_SERVER_URL", raising=False)
    composio_mcp.reset_session()
    result = composio_mcp.status(force=True)
    assert result["configured"] is False
    assert result["connected"] is False


def test_full_handshake_and_tool_list(monkeypatch):
    monkeypatch.setenv("COMPOSIO_MCP_URL", "https://connect.composio.dev/mcp")
    monkeypatch.setenv("COMPOSIO_API_KEY", "test-key-not-real")
    calls = []

    def fake_post(payload, session_id=None, timeout=20):
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

    monkeypatch.setattr(composio_mcp, "_post", fake_post)
    composio_mcp.reset_session()
    result = composio_mcp.status(force=True)

    assert result["connected"] is True
    assert result["tool_count"] == 2
    assert result["tools"] == ["gmail_send", "github_search"]
    assert result["protocol"] == "2025-03-26"
    assert calls[:3] == ["initialize", "notifications/initialized",
                         "tools/list"]


def test_call_tool_uses_mcp_tools_call(monkeypatch):
    monkeypatch.setenv("COMPOSIO_MCP_URL", "https://connect.composio.dev/mcp")
    monkeypatch.setenv("COMPOSIO_API_KEY", "test-key-not-real")
    seen = []

    def fake_post(payload, session_id=None, timeout=20):
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

    monkeypatch.setattr(composio_mcp, "_post", fake_post)
    composio_mcp.reset_session()
    result = composio_mcp.call_tool("gmail_send", {"to": "a@b.c"})

    assert result["ok"] is True
    assert seen == [{"name": "gmail_send", "arguments": {"to": "a@b.c"}}]
