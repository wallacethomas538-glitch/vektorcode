"""Generic MCP client — any streamable-HTTP or SSE MCP server.

Generalizes the proven Composio client (initialize -> tools/list ->
tools/call, protocol 2025-03-26) to any server, parameterized by URL +
transport + auth. Stdio is NOT supported (no local child process on
Render free) — see the universal MCP design.

Secrets never live here: the config carries the env var NAME only, and
the value is read from the environment at call time, stripped, and
never logged, never returned by any endpoint, never written to a receipt.

Per-server state: sessions, status cache and tool-list cache are all
keyed by server id, so one slow or dead server never blocks another.
A server that never answers is `connected: False` with the error
string — it never blocks chat.
"""
import json
import os
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

PROTOCOL_VERSION = "2025-03-26"
STATUS_TTL = 60
LIST_TTL = 60

_TRANSPORTS = ("streamable-http", "sse")
_AUTH_KINDS = ("none", "bearer", "api-key", "header")

# One lock per server id, so a slow server cannot block the others.
_locks = {}
_locks_guard = threading.Lock()
_sessions = {}       # id -> {"id", "protocol", "at", "sse": _SSEConnection|None}
_status_cache = {}   # id -> {"at", "value"}
_list_cache = {}     # (id, url) -> {"at", "tools"}
_next_id = [0]


class MCPError(Exception):
    """Raised when the MCP server rejects a request or cannot be reached."""


class MCPServerConfig:
    """One MCP server. Carries no secret — only the env var NAME."""

    def __init__(self, id, url, transport="streamable-http",
                 auth_kind="none", auth_header="", auth_env="",
                 enabled=True, expose_to_model=True, name="",
                 extra_hosts=frozenset(), url_env=""):
        self.id = id
        self.url = (url or "").strip()
        self.transport = transport
        self.auth_kind = auth_kind
        self.auth_header = (auth_header or "").strip()
        self.auth_env = (auth_env or "").strip()
        self.enabled = bool(enabled)
        self.expose_to_model = bool(expose_to_model)
        self.name = name or id
        self.extra_hosts = frozenset(extra_hosts or ())
        # Env var the URL comes from, when it does (Composio): used in
        # the status error so the label names the exact var to set.
        self.url_env = url_env

    def public_dict(self):
        """Registry-safe shape: no secret, only the env var name."""
        return {"id": self.id, "name": self.name, "url": self.url,
                "transport": self.transport, "auth_kind": self.auth_kind,
                "auth_header": self.auth_header, "auth_env": self.auth_env,
                "enabled": self.enabled,
                "expose_to_model": self.expose_to_model}


def _lock_for(server_id):
    with _locks_guard:
        lock = _locks.get(server_id)
        if lock is None:
            lock = threading.Lock()
            _locks[server_id] = lock
        return lock


def _next_rpc_id():
    _next_id[0] += 1
    return _next_id[0]


def _secret(cfg):
    """Secret value for this server, read from env at call time."""
    if cfg.auth_kind in ("bearer", "api-key", "header") and cfg.auth_env:
        return (os.environ.get(cfg.auth_env) or "").strip()
    return ""


def _auth_headers(cfg):
    """Only the auth headers for this server, from env at call time."""
    headers = {}
    secret = _secret(cfg)
    if secret:
        if cfg.auth_kind == "bearer":
            headers["Authorization"] = "Bearer " + secret
        else:
            headers[cfg.auth_header or "x-api-key"] = secret
    return headers


def _headers(cfg, session_id=None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "VektorCode/1.0",
    }
    headers.update(_auth_headers(cfg))
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return headers


def mcp_url(cfg):
    """Return the configured URL, guarded by the egress allowlist.

    The registry hosts (extra_hosts) are additive for MCP calls only;
    the hardcoded allowlist is never mutated at runtime."""
    from vektor_cloud.egress import guard_url
    if not cfg.url:
        return ""
    guard_url(cfg.url, extra_hosts=cfg.extra_hosts)
    return cfg.url


def configured(cfg):
    """True when the server can be called: URL set, and its key set
    when the auth kind needs one. No network is touched."""
    if not cfg.url:
        return False
    if cfg.auth_kind in ("bearer", "api-key", "header") and cfg.auth_env:
        return bool((os.environ.get(cfg.auth_env) or "").strip())
    return True


def _parse_sse_events(raw):
    """Parse an SSE body into [(event_type, data_string), ...]."""
    events = []
    event_type, data_lines = "message", []
    for line in raw.splitlines():
        if not line.strip():
            if data_lines:
                events.append((event_type, "\n".join(data_lines)))
            event_type, data_lines = "message", []
        elif line.startswith("event:"):
            event_type = line[6:].strip() or "message"
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        events.append((event_type, "\n".join(data_lines)))
    return events


def _sse_result(raw, expected_id=None):
    """First JSON-RPC result/error object in an SSE body, or None."""
    for _etype, data in _parse_sse_events(raw):
        try:
            obj = json.loads(data)
        except ValueError:
            continue
        if expected_id is not None and obj.get("id") != expected_id:
            continue
        if "result" in obj or "error" in obj:
            return obj
    return None


def _post_http(cfg, payload, session_id=None, timeout=30):
    """One streamable-HTTP POST. Returns (result_obj|None, session_id)."""
    url = mcp_url(cfg)
    if not url:
        raise MCPError("MCP URL is not set")
    request = Request(
        url, data=json.dumps(payload).encode(),
        headers=_headers(cfg, session_id), method="POST")
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
        return _sse_result(raw, payload.get("id")), new_session
    try:
        return json.loads(raw), new_session
    except ValueError:
        raise MCPError("MCP server returned invalid JSON")


class _SSEConnection:
    """One legacy SSE transport connection: a GET stream held open while
    JSON-RPC requests are POSTed to the endpoint URL the server names.

    Reads are sequential on the single GET stream (no reader thread):
    after each POST the caller reads the stream until the matching
    `message` event arrives, bounded by the call timeout.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.response = None
        self.endpoint = None
        self._sock = None  # raw socket, for per-read timeouts (see below)

    def connect(self, timeout=20):
        """Open the GET stream and read until the `endpoint` event."""
        url = mcp_url(self.cfg)
        if not url:
            raise MCPError("MCP URL is not set")
        headers = {"Accept": "text/event-stream",
                   "User-Agent": "VektorCode/1.0"}
        headers.update(_auth_headers(self.cfg))
        request = Request(url, headers=headers, method="GET")
        try:
            self.response = urlopen(request, timeout=timeout)
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise MCPError(f"MCP HTTP {exc.code}: {detail}".strip())
        except (URLError, TimeoutError, OSError) as exc:
            raise MCPError(f"MCP connection failed: {exc}")
        # The raw socket, so each read can be bounded by the call's own
        # deadline (see _read_event). Private attribute, guarded.
        try:
            self._sock = self.response.fp.raw._sock
        except Exception:
            self._sock = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            event = self._read_event(deadline)
            if event is None:
                break
            etype, data = event
            if etype == "endpoint" and data.strip():
                self.endpoint = data.strip()
                return self
        raise MCPError("MCP SSE server sent no endpoint event")

    def _read_event(self, deadline):
        """Read one SSE event from the stream. Returns (type, data) or
        None on EOF. Raises MCPError on timeout.

        The wait is bounded by the call deadline: the socket timeout is
        set to the remaining time before each readline. (select() on the
        stream cannot be used here — the response's buffered reader
        slurps the first events into userspace during the headers read,
        so select() on the fd reports nothing while readline() would
        return immediately.) A read that times out poisons the stream,
        so the caller drops the session and re-handshakes next time."""
        event_type, data_lines = "message", []
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise MCPError("MCP SSE read timed out")
            if self._sock is not None:
                try:
                    self._sock.settimeout(remaining)
                except Exception:
                    pass
            try:
                line = self.response.readline()
            except (TimeoutError, OSError) as exc:
                raise MCPError(f"MCP SSE read failed: {exc}")
            if not line:
                return None
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            if not text.strip():
                if data_lines:
                    return event_type, "\n".join(data_lines)
                continue
            if text.startswith("event:"):
                event_type = text[6:].strip() or "message"
            elif text.startswith("data:"):
                data_lines.append(text[5:].lstrip())

    def call(self, payload, timeout=20):
        """POST one JSON-RPC request, then read the stream until its
        `message` event arrives. Returns the parsed object or None.
        Notifications (no id) get no reply: return right after the POST
        instead of waiting for an event that will never come."""
        if not self.endpoint:
            raise MCPError("MCP SSE connection has no endpoint URL")
        endpoint = self.endpoint
        if endpoint.startswith("/"):
            base = urlsplit(self.cfg.url)
            endpoint = f"{base.scheme}://{base.netloc}{endpoint}"
        request = Request(
            endpoint, data=json.dumps(payload).encode(),
            headers=_headers(self.cfg), method="POST")
        try:
            with urlopen(request, timeout=timeout) as response:
                response.read()
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise MCPError(f"MCP HTTP {exc.code}: {detail}".strip())
        except (URLError, TimeoutError, OSError) as exc:
            raise MCPError(f"MCP connection failed: {exc}")
        if payload.get("id") is None:
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            event = self._read_event(deadline)
            if event is None:
                return None
            etype, data = event
            if etype != "message":
                continue
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            if payload.get("id") is not None and obj.get("id") != payload["id"]:
                continue
            return obj
        return None

    def close(self):
        try:
            if self.response is not None:
                self.response.close()
        except Exception:
            pass
        self.response = None
        self.endpoint = None


def _open_session(cfg, force=False):
    """Handshake with one server. Returns the session dict."""
    session = _sessions.get(cfg.id)
    if session and not force:
        return session
    if cfg.transport == "sse":
        conn = _SSEConnection(cfg)
        conn.connect(timeout=30)
        result = conn.call({
            "jsonrpc": "2.0", "id": _next_rpc_id(), "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "VektorCode", "version": "1.0"},
            },
        }, timeout=30)
        if result is None:
            conn.close()
            raise MCPError("MCP initialize returned no response")
        if result.get("error"):
            conn.close()
            raise MCPError("MCP initialize failed: "
                           + json.dumps(result["error"])[:240])
        protocol = (result.get("result") or {}).get(
            "protocolVersion", PROTOCOL_VERSION)
        try:
            conn.call({"jsonrpc": "2.0",
                       "method": "notifications/initialized"}, timeout=10)
        except MCPError:
            pass
        session = {"id": None, "protocol": protocol, "at": time.time(),
                   "sse": conn}
        _sessions[cfg.id] = session
        return session
    # streamable-http
    result, session_id = _post_http(cfg, {
        "jsonrpc": "2.0",
        "id": _next_rpc_id(),
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
    # Notifications are fire-and-forget; some servers answer 202 with no body,
    # and a slow server may hold the response — wait at most 5s, then move on.
    try:
        _post_http(cfg, {"jsonrpc": "2.0",
                         "method": "notifications/initialized"},
                   session_id=session_id, timeout=5)
    except MCPError:
        pass
    session = {"id": session_id, "protocol": protocol, "at": time.time(),
               "sse": None}
    _sessions[cfg.id] = session
    return session


def _rpc(cfg, method, params, timeout=20):
    """One JSON-RPC call against a server, with one re-handshake when a
    restarting server forgets the session. Transport-dispatched."""
    with _lock_for(cfg.id):
        session = _open_session(cfg)
        for attempt in range(2):
            if cfg.transport == "sse":
                conn = session.get("sse")
                if conn is None:
                    session = _open_session(cfg, force=True)
                    conn = session.get("sse")
                try:
                    result = conn.call({
                        "jsonrpc": "2.0", "id": _next_rpc_id(),
                        "method": method, "params": params,
                    }, timeout=timeout)
                except MCPError:
                    # A stalled or poisoned SSE stream must not break the
                    # next call: drop the session so it re-handshakes.
                    _close_session(cfg.id)
                    raise
            else:
                result, new_session = _post_http(cfg, {
                    "jsonrpc": "2.0", "id": _next_rpc_id(),
                    "method": method, "params": params,
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
                _close_session(cfg.id)
                session = _open_session(cfg, force=True)
                continue
            raise MCPError(f"MCP {method} failed: {message}")
    raise MCPError(f"MCP {method} failed")


def _close_session(server_id):
    session = _sessions.pop(server_id, None)
    if session and session.get("sse") is not None:
        session["sse"].close()


def list_tools(cfg, timeout=20, force=False):
    """Tool list for one server, cached briefly. Raises MCPError when
    the server cannot be reached."""
    key = (cfg.id, cfg.url)
    now = time.time()
    cached = _list_cache.get(key)
    if not force and cached and now - cached["at"] < LIST_TTL:
        return list(cached["tools"])
    result = _rpc(cfg, "tools/list", {}, timeout=timeout)
    tools = (result or {}).get("tools", [])
    _list_cache[key] = {"at": now, "tools": list(tools)}
    return list(tools)


def call_tool(cfg, tool_name, arguments):
    """Call one MCP tool. Returns {'ok': bool, ...} and never leaks
    the key."""
    try:
        result = _rpc(
            cfg, "tools/call",
            {"name": tool_name, "arguments": arguments or {}},
            timeout=60)
        return {"ok": True, "result": result}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:240]}


def status(cfg, force=False):
    """Read-only handshake result for one server, cached briefly."""
    now = time.time()
    cached = _status_cache.get(cfg.id)
    if (not force and cached and cached.get("url") == cfg.url
            and now - cached["at"] < STATUS_TTL):
        return dict(cached["value"])

    value = {
        "id": cfg.id,
        "name": cfg.name,
        "configured": configured(cfg),
        "connected": False,
        "transport": cfg.transport,
        "tool_count": 0,
        "tools": [],
        "protocol": None,
        "error": None,
    }
    if not cfg.url:
        value["error"] = f"{cfg.url_env or 'MCP URL'} is not set"
    elif not configured(cfg):
        value["error"] = f"{cfg.auth_env} is not set"
    else:
        try:
            mcp_url(cfg)  # egress check before any network attempt
            tools = list_tools(cfg, force=force)
            session = _sessions.get(cfg.id) or {}
            value.update({
                "connected": True,
                "tool_count": len(tools),
                "tools": [t.get("name") for t in tools[:20]
                          if isinstance(t, dict)],
                "protocol": session.get("protocol"),
            })
        except Exception as exc:
            value["error"] = str(exc)[:240]

    _status_cache[cfg.id] = {"at": now, "url": cfg.url,
                             "value": dict(value)}
    return value


def reset_server(server_id):
    """Drop one server's session and caches (tests, auth changes)."""
    _close_session(server_id)
    _status_cache.pop(server_id, None)
    for key in [k for k in _list_cache if k[0] == server_id]:
        del _list_cache[key]


def reset_all():
    """Drop every server's session and caches (tests only)."""
    for server_id in list(_sessions):
        _close_session(server_id)
    _status_cache.clear()
    _list_cache.clear()
