"""Composio MCP tool layer — config only, no key in code.

Set COMPOSIO_API_KEY + COMPOSIO_MCP_URL in the host environment. The URL is
guarded by the Vektor egress allowlist (egress.py).

This module speaks MCP over streamable HTTP: initialize, initialized, then
tools/list or tools/call. Nothing is sent until Wallace approves the exact job;
status() is a read-only handshake used by the dashboard.
"""
import json
import os
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

PROTOCOL_VERSION = "2025-03-26"
STATUS_TTL = 60
_lock = threading.Lock()
_session = None
_status_cache = {"at": 0.0, "url": "", "value": None}
_next_id = [0]


class MCPError(Exception):
    """Raised when the MCP server rejects a request or cannot be reached."""


def _url():
    return (os.environ.get("COMPOSIO_MCP_URL")
            or os.environ.get("MCP_SERVER_URL") or "").strip()


def mcp_url():
    """Return the configured MCP URL, guarded by the egress allowlist."""
    from vektor_cloud.egress import guard_url
    url = _url()
    if not url:
        return ""
    guard_url(url)  # raises EgressBlocked if not allowlisted
    return url


def configured():
    return bool(os.environ.get("COMPOSIO_API_KEY") and mcp_url())


def _headers(session_id=None):
    url = _url()
    host = (urlsplit(url).hostname or "").lower()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "VektorCode/1.0",
    }
    key = (os.environ.get("COMPOSIO_API_KEY") or "").strip()
    if key:
        if host.endswith("composio.dev"):
            headers["x-consumer-api-key"] = key
        else:
            headers["x-api-key"] = key
            headers["Authorization"] = "Bearer " + key
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return headers


def _parse_sse(raw, expected_id=None):
    events, buffer = [], []
    for line in raw.splitlines():
        if not line.strip():
            if buffer:
                events.append("\n".join(buffer))
                buffer = []
        elif line.startswith("data:"):
            buffer.append(line[5:].lstrip())
    if buffer:
        events.append("\n".join(buffer))
    parsed = []
    for event in events:
        try:
            parsed.append(json.loads(event))
        except ValueError:
            continue
    for obj in parsed:
        if expected_id is not None and obj.get("id") != expected_id:
            continue
        if "result" in obj or "error" in obj:
            return obj
    return parsed[-1] if parsed else None


def _post(payload, session_id=None, timeout=20):
    url = mcp_url()
    if not url:
        raise MCPError("COMPOSIO_MCP_URL is not set")
    request = Request(
        url, data=json.dumps(payload).encode(),
        headers=_headers(session_id), method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            content_type = response.headers.get("Content-Type", "")
            new_session = response.headers.get("Mcp-Session-Id")
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise MCPError(f"MCP HTTP {exc.code}: {detail}".strip())
    except (URLError, TimeoutError, OSError) as exc:
        raise MCPError(f"MCP connection failed: {exc}")
    if not raw.strip():
        return None, new_session
    if ("text/event-stream" in content_type
            or raw.lstrip().startswith(("event:", "data:"))):
        return _parse_sse(raw, payload.get("id")), new_session
    try:
        return json.loads(raw), new_session
    except ValueError:
        raise MCPError("MCP server returned invalid JSON")


def _open_session(force=False):
    global _session
    if _session and not force:
        return _session
    _next_id[0] += 1
    result, session_id = _post({
        "jsonrpc": "2.0",
        "id": _next_id[0],
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "VektorCode", "version": "1.0"},
        },
    })
    if result is None:
        raise MCPError("MCP initialize returned no response")
    if result.get("error"):
        raise MCPError("MCP initialize failed: "
                       + json.dumps(result["error"])[:240])
    protocol = (result.get("result") or {}).get(
        "protocolVersion", PROTOCOL_VERSION)
    # Notifications are fire-and-forget; some servers answer 202 with no body.
    try:
        _post({"jsonrpc": "2.0", "method": "notifications/initialized"},
              session_id=session_id, timeout=10)
    except MCPError:
        pass
    _session = {"id": session_id, "protocol": protocol, "at": time.time()}
    return _session


def _rpc(method, params, timeout=20):
    global _session
    with _lock:
        session = _open_session()
        for attempt in range(2):
            _next_id[0] += 1
            result, new_session = _post({
                "jsonrpc": "2.0",
                "id": _next_id[0],
                "method": method,
                "params": params,
            }, session_id=session["id"], timeout=timeout)
            if new_session:
                session["id"] = new_session
            if result is None:
                raise MCPError(f"MCP {method} returned no response")
            if not result.get("error"):
                return result.get("result")
            message = json.dumps(result["error"])[:240]
            # A restarting server may forget the session; re-handshake once.
            if attempt == 0 and ("session" in message.lower()
                                 or "initialized" in message.lower()):
                _session = None
                session = _open_session(force=True)
                continue
            raise MCPError(f"MCP {method} failed: {message}")
    raise MCPError(f"MCP {method} failed")


def list_tools(timeout=10):
    result = _rpc("tools/list", {}, timeout=timeout)
    return (result or {}).get("tools", [])


def call_tool(tool_name, arguments):
    """Call one MCP tool. Returns {'ok': bool, ...} and never leaks the key."""
    try:
        result = _rpc(
            "tools/call", {"name": tool_name, "arguments": arguments or {}},
            timeout=60)
        return {"ok": True, "result": result}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:240]}


def status(force=False):
    """Read-only MCP handshake result, cached briefly for /api/state."""
    url = _url()
    now = time.time()
    cached = _status_cache["value"]
    if (not force and cached and _status_cache["url"] == url
            and now - _status_cache["at"] < STATUS_TTL):
        return dict(cached)

    value = {
        "configured": bool(url),
        "connected": False,
        "transport": "streamable-http",
        "tool_count": 0,
        "tools": [],
        "protocol": None,
        "error": None,
    }
    if not url:
        value["error"] = "COMPOSIO_MCP_URL is not set"
    else:
        try:
            mcp_url()  # egress check before any network attempt
            if not os.environ.get("COMPOSIO_API_KEY"):
                value["error"] = "COMPOSIO_API_KEY is not set"
            else:
                tools = list_tools()
                session = _session or {}
                value.update({
                    "connected": True,
                    "tool_count": len(tools),
                    "tools": [t.get("name") for t in tools[:20]
                              if isinstance(t, dict)],
                    "protocol": session.get("protocol"),
                })
        except Exception as exc:
            value["error"] = str(exc)[:240]

    _status_cache.update({"at": now, "url": url, "value": dict(value)})
    return value


def reset_session():
    """Drop the cached MCP session (used by tests and after auth changes)."""
    global _session
    with _lock:
        _session = None
    _status_cache.update({"at": 0.0, "url": "", "value": None})
