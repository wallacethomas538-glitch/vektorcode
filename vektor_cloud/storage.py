"""Durable dashboard storage.

Supabase REST is used when SUPABASE_URL and SUPABASE_KEY are set. Keys are
read from the environment only. When they are absent — or Supabase is
unreachable — the existing SQLite file remains the fallback so the dashboard
keeps working.

Expected Supabase tables are in supabase/schema.sql.
"""
import json
import os
import re
import sqlite3
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

SETTINGS_TABLE = "vektor_settings"
SESSIONS_TABLE = "vektor_sessions"
RECEIPTS_TABLE = "vektor_receipts"
MESSAGES_TABLE = "vektor_messages"
MEMORY_TABLE = "vektor_memory"
MCP_SERVERS_TABLE = "vektor_mcp_servers"
MCP_HOSTS_TABLE = "vektor_mcp_hosts"

# MCP registry validation. Same transport/auth vocab as
# vektor_cloud/mcp_client.py. The registry stores env var NAMES only —
# never a secret value.
MCP_TRANSPORTS = ("streamable-http", "sse")
MCP_AUTH_KINDS = ("none", "bearer", "api-key", "header")
_MCP_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_MCP_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
_MCP_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_MCP_HEADER_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")

# Cross-session memory kinds. Same four kinds as vektor_cloud/memory.py
# (fact / preference / decision / lesson). The web backend stores them in
# the DB (SQLite mirror + Supabase), never in files — files are ephemeral
# on Render and lost on redeploy.
MEMORY_KINDS = ("fact", "preference", "decision", "lesson")

# Session expiry: sessions whose `updated` is strictly older than this are
# purged on read (list_sessions) and by POST /api/session/expired.
# Wallace's order 2026-11-07: 3 days.
SESSION_TTL_SECONDS = 3 * 24 * 3600

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, action TEXT,"
    " detail TEXT, sensitive INTEGER, approved INTEGER, created REAL)",
    "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, title TEXT,"
    " model TEXT, tokens INTEGER, cost REAL, active INTEGER, created REAL,"
    " updated REAL)",
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,"
    " value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY,"
    " session_id TEXT, role TEXT, content TEXT, model TEXT, provider TEXT,"
    " created REAL)",
    "CREATE INDEX IF NOT EXISTS messages_session_idx"
    " ON messages (session_id, created)",
    "CREATE TABLE IF NOT EXISTS memory (kind TEXT, key TEXT, value TEXT,"
    " updated_at REAL, PRIMARY KEY(kind, key))",
    "CREATE TABLE IF NOT EXISTS mcp_servers (id TEXT PRIMARY KEY,"
    " name TEXT, url TEXT NOT NULL, transport TEXT NOT NULL,"
    " auth_kind TEXT NOT NULL, auth_header TEXT, auth_env TEXT,"
    " enabled INTEGER NOT NULL, expose_to_model INTEGER NOT NULL,"
    " updated_at REAL)",
    "CREATE TABLE IF NOT EXISTS mcp_hosts (host TEXT PRIMARY KEY,"
    " added_by TEXT, created REAL)",
)


class StorageError(Exception):
    """Raised when the configured remote store cannot serve a request."""


def supabase_config():
    """Return (url, key) from the environment, or None when not configured."""
    url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("SUPABASE_KEY") or "").strip()
    if not url or not key:
        return None
    return url, key


def sqlite_connect(path):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    for statement in _SCHEMA:
        conn.execute(statement)
    conn.commit()
    return conn


def validate_memory(kind, key, value):
    """Validate a memory row. Raises ValueError on bad input.
    Returns (kind, key, value) with the key stripped."""
    if kind not in MEMORY_KINDS:
        raise ValueError(
            f"kind must be one of {', '.join(MEMORY_KINDS)}")
    if not isinstance(key, str) or not 1 <= len(key.strip()) <= 120:
        raise ValueError("key must be 1-120 characters")
    if not isinstance(value, str) or not 1 <= len(value) <= 12000:
        raise ValueError("value must be 1-12000 characters")
    return kind, key.strip(), value


def validate_mcp_server(data):
    """Validate an MCP registry row. Raises ValueError on bad input.

    Returns a normalized dict (no secret — only the env var NAME).
    Shape only is validated here: the egress allowlist is checked at
    call time, after the server's host has been added to
    vektor_mcp_hosts by mcp_server_save.
    """
    if not isinstance(data, dict):
        raise ValueError("server must be an object")
    server_id = str(data.get("id") or "").strip()
    if not _MCP_ID_RE.match(server_id):
        raise ValueError(
            "id must be a lowercase slug (letters, digits, -, _),"
            " starting with a letter, max 32 characters")
    name = str(data.get("name") or server_id).strip()
    if not 1 <= len(name) <= 80:
        raise ValueError("name must be 1-80 characters")
    url = str(data.get("url") or "").strip()
    if not 1 <= len(url) <= 2000:
        raise ValueError("url must be 1-2000 characters")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("url must start with http:// or https://")
    host = (parsed.hostname or "").lower()
    loopback = host in ("localhost", "127.0.0.1", "::1")
    if parsed.scheme == "http" and not loopback:
        raise ValueError("url must use https (plain http is loopback-only)")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("url must not contain credentials")
    if parsed.fragment:
        raise ValueError("url must not contain a fragment")
    if parsed.port not in (None, 443) and not loopback:
        raise ValueError("url must use port 443 (or none)")
    transport = str(data.get("transport") or "streamable-http").strip()
    if transport not in MCP_TRANSPORTS:
        raise ValueError(
            f"transport must be one of {', '.join(MCP_TRANSPORTS)}")
    auth_kind = str(data.get("auth_kind") or "none").strip()
    if auth_kind not in MCP_AUTH_KINDS:
        raise ValueError(
            f"auth_kind must be one of {', '.join(MCP_AUTH_KINDS)}")
    auth_header = str(data.get("auth_header") or "").strip()
    if auth_header and not _MCP_HEADER_RE.match(auth_header):
        raise ValueError("auth_header must be a valid header name")
    auth_env = str(data.get("auth_env") or "").strip()
    if auth_env and not _MCP_ENV_RE.match(auth_env):
        raise ValueError("auth_env must be an UPPER_CASE env var name")
    if auth_kind != "none" and not auth_env:
        raise ValueError(
            "auth_env is required when auth_kind is not 'none'"
            " (the key itself lives in the Render env, never here)")
    return {
        "id": server_id,
        "name": name,
        "url": url,
        "transport": transport,
        "auth_kind": auth_kind,
        "auth_header": auth_header,
        "auth_env": auth_env,
        "enabled": bool(data.get("enabled", True)),
        "expose_to_model": bool(data.get("expose_to_model", True)),
    }


def validate_mcp_host(host):
    """Validate a bare MCP hostname. Raises ValueError on bad input.
    Returns the normalized (lowercase) hostname."""
    h = str(host or "").strip().lower()
    if not 1 <= len(h) <= 253:
        raise ValueError("host must be 1-253 characters")
    if not _MCP_HOST_RE.match(h) or ".." in h:
        raise ValueError("host must be a bare hostname (no scheme,"
                         " port, or path)")
    return h


class SQLiteStore:
    def __init__(self, path):
        self.path = path

    def get_setting(self, key, default):
        conn = sqlite_connect(self.path)
        try:
            row = conn.execute(
                "SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default
        finally:
            conn.close()

    def set_setting(self, key, value):
        conn = sqlite_connect(self.path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO settings VALUES (?,?)",
                (key, json.dumps(value)))
            conn.commit()
        finally:
            conn.close()

    def add_receipt(self, action, detail, sensitive, approved):
        rid = str(uuid.uuid4())
        conn = sqlite_connect(self.path)
        try:
            conn.execute(
                "INSERT INTO receipts VALUES (?,?,?,?,?,?)",
                (rid, action, json.dumps(detail), int(sensitive),
                 int(approved), time.time()))
            conn.commit()
        finally:
            conn.close()
        return rid

    def list_receipts(self, limit=30):
        conn = sqlite_connect(self.path)
        try:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM receipts ORDER BY created DESC LIMIT ?",
                (limit,))]
        finally:
            conn.close()

    def list_sessions(self):
        conn = sqlite_connect(self.path)
        try:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM sessions ORDER BY updated DESC")]
        finally:
            conn.close()

    def create_session(self, session_id, title, model):
        now = time.time()
        conn = sqlite_connect(self.path)
        try:
            conn.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
                (session_id, title, model, 0, 0, 1, now, now))
            conn.commit()
        finally:
            conn.close()

    def ensure_session(self, session_id, title, model):
        """Create the session only if it does not already exist."""
        now = time.time()
        conn = sqlite_connect(self.path)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO sessions VALUES (?,?,?,?,?,?,?,?)",
                (session_id, title, model, 0, 0, 1, now, now))
            conn.commit()
        finally:
            conn.close()

    def set_session_model(self, session_id, model):
        """Remember which provider:model a session is using (per-session
        persistence of the model selection)."""
        conn = sqlite_connect(self.path)
        try:
            conn.execute("UPDATE sessions SET model=? WHERE id=?",
                         (model, session_id))
            conn.commit()
        finally:
            conn.close()

    def add_message(self, session_id, role, content, model=None,
                    provider=None):
        mid = str(uuid.uuid4())
        conn = sqlite_connect(self.path)
        try:
            conn.execute(
                "INSERT INTO messages VALUES (?,?,?,?,?,?,?)",
                (mid, session_id, role, content, model, provider,
                 time.time()))
            conn.execute(
                "UPDATE sessions SET updated=? WHERE id=?",
                (time.time(), session_id))
            conn.commit()
        finally:
            conn.close()
        return mid

    def list_messages(self, session_id, limit=20):
        """Last `limit` messages of a session, oldest first."""
        conn = sqlite_connect(self.path)
        try:
            rows = conn.execute(
                "SELECT * FROM (SELECT * FROM messages WHERE session_id=?"
                " ORDER BY created DESC, rowid DESC LIMIT ?)"
                " ORDER BY created ASC, rowid ASC",
                (session_id, limit)).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def resume_session(self, session_id):
        conn = sqlite_connect(self.path)
        try:
            conn.execute("UPDATE sessions SET active=0")
            conn.execute(
                "UPDATE sessions SET active=1, updated=? WHERE id=?",
                (time.time(), session_id))
            conn.commit()
        finally:
            conn.close()

    def delete_session(self, session_id):
        conn = sqlite_connect(self.path)
        try:
            conn.execute("DELETE FROM messages WHERE session_id=?",
                         (session_id,))
            conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            conn.commit()
        finally:
            conn.close()

    def delete_expired_sessions(self, cutoff):
        """Delete sessions with updated strictly older than `cutoff`, plus
        their messages. Receipts, settings and memory are preserved.
        Returns the number of sessions deleted."""
        conn = sqlite_connect(self.path)
        try:
            conn.execute(
                "DELETE FROM messages WHERE session_id IN"
                " (SELECT id FROM sessions WHERE updated < ?)", (cutoff,))
            cur = conn.execute(
                "DELETE FROM sessions WHERE updated < ?", (cutoff,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def memory_list(self):
        """All memories, most-recent-first."""
        conn = sqlite_connect(self.path)
        try:
            return [dict(row) for row in conn.execute(
                "SELECT kind, key, value, updated_at FROM memory"
                " ORDER BY updated_at DESC")]
        finally:
            conn.close()

    def memory_get(self, kind, key):
        conn = sqlite_connect(self.path)
        try:
            row = conn.execute(
                "SELECT kind, key, value, updated_at FROM memory"
                " WHERE kind=? AND key=?", (kind, key)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def memory_save(self, kind, key, value):
        kind, key, value = validate_memory(kind, key, value)
        now = time.time()
        conn = sqlite_connect(self.path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO memory VALUES (?,?,?,?)",
                (kind, key, value, now))
            conn.commit()
        finally:
            conn.close()
        return {"kind": kind, "key": key, "value": value,
                "updated_at": now}

    def memory_delete(self, kind, key):
        conn = sqlite_connect(self.path)
        try:
            cur = conn.execute(
                "DELETE FROM memory WHERE kind=? AND key=?", (kind, key))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    # --- MCP registry (universal MCP, 2026-11-07) ---

    def mcp_server_list(self):
        """All registered MCP servers, by id."""
        conn = sqlite_connect(self.path)
        try:
            rows = conn.execute(
                "SELECT id, name, url, transport, auth_kind, auth_header,"
                " auth_env, enabled, expose_to_model, updated_at"
                " FROM mcp_servers ORDER BY id").fetchall()
            return [self._mcp_server_row(dict(row)) for row in rows]
        finally:
            conn.close()

    @staticmethod
    def _mcp_server_row(row):
        row["enabled"] = bool(row["enabled"])
        row["expose_to_model"] = bool(row["expose_to_model"])
        return row

    def mcp_server_get(self, server_id):
        conn = sqlite_connect(self.path)
        try:
            row = conn.execute(
                "SELECT id, name, url, transport, auth_kind, auth_header,"
                " auth_env, enabled, expose_to_model, updated_at"
                " FROM mcp_servers WHERE id=?", (server_id,)).fetchone()
            return self._mcp_server_row(dict(row)) if row else None
        finally:
            conn.close()

    def mcp_server_save(self, data):
        row = validate_mcp_server(data)
        now = time.time()
        host = validate_mcp_host(urlsplit(row["url"]).hostname)
        conn = sqlite_connect(self.path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO mcp_servers VALUES"
                " (?,?,?,?,?,?,?,?,?,?)",
                (row["id"], row["name"], row["url"], row["transport"],
                 row["auth_kind"], row["auth_header"], row["auth_env"],
                 int(row["enabled"]), int(row["expose_to_model"]), now))
            # Mechanism B: the server's host is allowlisted for MCP
            # calls only, at the same time as the server is saved.
            conn.execute(
                "INSERT OR IGNORE INTO mcp_hosts VALUES (?,?,?)",
                (host, "wallace", now))
            conn.commit()
        finally:
            conn.close()
        row["updated_at"] = now
        return row

    def mcp_server_delete(self, server_id):
        if server_id == "composio":
            raise ValueError(
                "composio is the default server and cannot be deleted")
        conn = sqlite_connect(self.path)
        try:
            cur = conn.execute(
                "DELETE FROM mcp_servers WHERE id=?", (server_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def mcp_host_list(self):
        """Wallace-managed MCP hosts, alphabetical."""
        conn = sqlite_connect(self.path)
        try:
            return [row["host"] for row in conn.execute(
                "SELECT host FROM mcp_hosts ORDER BY host").fetchall()]
        finally:
            conn.close()

    def mcp_host_add(self, host):
        host = validate_mcp_host(host)
        conn = sqlite_connect(self.path)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO mcp_hosts VALUES (?,?,?)",
                (host, "wallace", time.time()))
            conn.commit()
        finally:
            conn.close()
        return host

    def mcp_host_remove(self, host):
        host = validate_mcp_host(host)
        conn = sqlite_connect(self.path)
        try:
            cur = conn.execute(
                "DELETE FROM mcp_hosts WHERE host=?", (host,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


class SupabaseStore:
    """Minimal PostgREST client for the dashboard tables."""

    def __init__(self, url, key):
        self.url = url
        self.key = key

    def _request(self, method, table, query=None, payload=None,
                 prefer=None, timeout=10):
        endpoint = f"{self.url}/rest/v1/{table}"
        if query:
            endpoint += "?" + urlencode(query, safe=".*")
        headers = {
            "apikey": self.key,
            "Authorization": "Bearer " + self.key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        data = None if payload is None else json.dumps(payload).encode()
        req = Request(endpoint, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise StorageError(
                f"Supabase HTTP {exc.code}: {detail}".strip())
        except (URLError, TimeoutError, OSError) as exc:
            raise StorageError(f"Supabase unreachable: {exc}")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise StorageError("Supabase returned invalid JSON")

    def get_setting(self, key, default):
        rows = self._request(
            "GET", SETTINGS_TABLE,
            {"select": "value", "key": f"eq.{quote(key, safe='')}",
             "limit": "1"})
        return rows[0]["value"] if rows else default

    def set_setting(self, key, value):
        self._request(
            "POST", SETTINGS_TABLE,
            {"on_conflict": "key"},
            [{"key": key, "value": value, "updated_at": time.time()}],
            "resolution=merge-duplicates,return=minimal")

    def add_receipt(self, action, detail, sensitive, approved):
        rid = str(uuid.uuid4())
        self._request(
            "POST", RECEIPTS_TABLE, None,
            [{"id": rid, "action": action, "detail": detail,
              "sensitive": bool(sensitive), "approved": bool(approved),
              "created": time.time()}],
            "return=minimal")
        return rid

    def list_receipts(self, limit=30):
        return self._request(
            "GET", RECEIPTS_TABLE,
            {"select": "*", "order": "created.desc", "limit": str(limit)})

    def list_sessions(self):
        return self._request(
            "GET", SESSIONS_TABLE,
            {"select": "*", "order": "updated.desc"})

    def create_session(self, session_id, title, model):
        now = time.time()
        self._request(
            "POST", SESSIONS_TABLE, None,
            [{"id": session_id, "title": title, "model": model,
              "tokens": 0, "cost": 0, "active": True,
              "created": now, "updated": now}],
            "return=minimal")

    def ensure_session(self, session_id, title, model):
        rows = self._request(
            "GET", SESSIONS_TABLE,
            {"select": "id",
             "id": "eq." + quote(session_id, safe=''), "limit": "1"})
        if not rows:
            self.create_session(session_id, title, model)

    def set_session_model(self, session_id, model):
        """Remember which provider:model a session is using (per-session
        persistence of the model selection)."""
        self._request(
            "PATCH", SESSIONS_TABLE,
            {"id": "eq." + quote(session_id, safe='')},
            {"model": model})

    def add_message(self, session_id, role, content, model=None,
                    provider=None):
        mid = str(uuid.uuid4())
        self._request(
            "POST", MESSAGES_TABLE, None,
            [{"id": mid, "session_id": session_id, "role": role,
              "content": content, "model": model, "provider": provider,
              "created": time.time()}],
            "return=minimal")
        return mid

    def list_messages(self, session_id, limit=20):
        rows = self._request(
            "GET", MESSAGES_TABLE,
            {"select": "*",
             "session_id": "eq." + quote(session_id, safe=''),
             "order": "created.desc", "limit": str(limit)})
        return list(reversed(rows or []))

    def resume_session(self, session_id):
        self._request(
            "PATCH", SESSIONS_TABLE, {"active": "eq.true"},
            {"active": False}, "return=minimal")
        self._request(
            "PATCH", SESSIONS_TABLE,
            {"id": "eq." + quote(session_id, safe='')},
            {"active": True, "updated": time.time()}, "return=minimal")

    def delete_session(self, session_id):
        self._request(
            "DELETE", MESSAGES_TABLE,
            {"session_id": "eq." + quote(session_id, safe='')},
            None, "return=minimal")
        self._request(
            "DELETE", SESSIONS_TABLE,
            {"id": "eq." + quote(session_id, safe='')},
            None, "return=minimal")

    def delete_expired_sessions(self, cutoff):
        """Delete sessions with updated strictly older than `cutoff`.
        Messages cascade via the FK in supabase/schema.sql. Receipts,
        settings and memory are preserved. Returns the number deleted."""
        rows = self._request(
            "GET", SESSIONS_TABLE,
            {"select": "id", "updated": f"lt.{cutoff}"})
        count = len(rows or [])
        if not count:
            return 0
        self._request(
            "DELETE", SESSIONS_TABLE,
            {"updated": f"lt.{cutoff}"}, None, "return=minimal")
        return count

    def memory_list(self):
        """All memories, most-recent-first."""
        return self._request(
            "GET", MEMORY_TABLE,
            {"select": "kind,key,value,updated_at",
             "order": "updated_at.desc"}) or []

    def memory_get(self, kind, key):
        rows = self._request(
            "GET", MEMORY_TABLE,
            {"select": "kind,key,value,updated_at",
             "kind": "eq." + quote(kind, safe=''),
             "key": "eq." + quote(key, safe=''), "limit": "1"})
        return rows[0] if rows else None

    def memory_save(self, kind, key, value):
        kind, key, value = validate_memory(kind, key, value)
        now = time.time()
        self._request(
            "POST", MEMORY_TABLE,
            {"on_conflict": "kind,key"},
            [{"kind": kind, "key": key, "value": value,
              "updated_at": now}],
            "resolution=merge-duplicates,return=minimal")
        return {"kind": kind, "key": key, "value": value,
                "updated_at": now}

    def memory_delete(self, kind, key):
        # PostgREST DELETE returns no row count here; True means the
        # request succeeded.
        self._request(
            "DELETE", MEMORY_TABLE,
            {"kind": "eq." + quote(kind, safe=''),
             "key": "eq." + quote(key, safe='')},
            None, "return=minimal")
        return True

    # --- MCP registry (universal MCP, 2026-11-07) ---

    def mcp_server_list(self):
        """All registered MCP servers, by id."""
        return self._request(
            "GET", MCP_SERVERS_TABLE,
            {"select": "id,name,url,transport,auth_kind,auth_header,"
                       "auth_env,enabled,expose_to_model,updated_at",
             "order": "id.asc"}) or []

    def mcp_server_get(self, server_id):
        rows = self._request(
            "GET", MCP_SERVERS_TABLE,
            {"select": "id,name,url,transport,auth_kind,auth_header,"
                       "auth_env,enabled,expose_to_model,updated_at",
             "id": "eq." + quote(server_id, safe=''), "limit": "1"})
        return rows[0] if rows else None

    def mcp_server_save(self, data):
        row = validate_mcp_server(data)
        now = time.time()
        host = validate_mcp_host(urlsplit(row["url"]).hostname)
        row["updated_at"] = now
        self._request(
            "POST", MCP_SERVERS_TABLE,
            {"on_conflict": "id"},
            [row], "resolution=merge-duplicates,return=minimal")
        # Mechanism B: the server's host is allowlisted for MCP
        # calls only, at the same time as the server is saved.
        self._request(
            "POST", MCP_HOSTS_TABLE,
            {"on_conflict": "host"},
            [{"host": host, "added_by": "wallace", "created": now}],
            "resolution=merge-duplicates,return=minimal")
        return row

    def mcp_server_delete(self, server_id):
        if server_id == "composio":
            raise ValueError(
                "composio is the default server and cannot be deleted")
        # PostgREST DELETE returns no row count here; True means the
        # request succeeded.
        self._request(
            "DELETE", MCP_SERVERS_TABLE,
            {"id": "eq." + quote(server_id, safe='')},
            None, "return=minimal")
        return True

    def mcp_host_list(self):
        """Wallace-managed MCP hosts, alphabetical."""
        rows = self._request(
            "GET", MCP_HOSTS_TABLE,
            {"select": "host", "order": "host.asc"}) or []
        return [row["host"] for row in rows]

    def mcp_host_add(self, host):
        host = validate_mcp_host(host)
        self._request(
            "POST", MCP_HOSTS_TABLE,
            {"on_conflict": "host"},
            [{"host": host, "added_by": "wallace",
              "created": time.time()}],
            "resolution=merge-duplicates,return=minimal")
        return host

    def mcp_host_remove(self, host):
        host = validate_mcp_host(host)
        self._request(
            "DELETE", MCP_HOSTS_TABLE,
            {"host": "eq." + quote(host, safe='')},
            None, "return=minimal")
        return True


class DashboardStore:
    """Supabase first, SQLite fallback. Failed remotes back off for 60s."""

    def __init__(self, db_path):
        self.sqlite = SQLiteStore(db_path)
        config = supabase_config()
        self._remote = SupabaseStore(*config) if config else None
        self._remote_down_until = 0.0
        self.last_error = None

    @property
    def backend(self):
        return "supabase" if self._remote else "sqlite"

    def status(self):
        remote_down = time.time() < self._remote_down_until
        return {
            "backend": "sqlite" if remote_down or not self._remote
            else "supabase",
            "configured_backend": self.backend,
            "supabase_configured": bool(self._remote),
            "supabase_error": self.last_error,
        }

    def _call(self, name, *args, **kwargs):
        if self._remote and time.time() >= self._remote_down_until:
            try:
                return getattr(self._remote, name)(*args, **kwargs)
            except Exception as exc:
                # Fall back locally; remember why so /api/state can report it.
                self.last_error = str(exc)[:240]
                self._remote_down_until = time.time() + 60
        return getattr(self.sqlite, name)(*args, **kwargs)

    def get_setting(self, key, default):
        return self._call("get_setting", key, default)

    def set_setting(self, key, value):
        return self._call("set_setting", key, value)

    def add_receipt(self, action, detail, sensitive=False, approved=False):
        return self._call("add_receipt", action, detail, sensitive, approved)

    def list_receipts(self, limit=30):
        return self._call("list_receipts", limit)

    def list_sessions(self):
        # Purge-on-read: sessions older than 3 days are deleted before the
        # list is served. A failed purge never blocks the listing.
        try:
            self.delete_expired_sessions(
                time.time() - SESSION_TTL_SECONDS)
        except Exception as exc:
            self.last_error = str(exc)[:240]
        return self._call("list_sessions")

    def delete_expired_sessions(self, cutoff):
        return self._call("delete_expired_sessions", cutoff)

    def memory_list(self):
        return self._call("memory_list")

    def memory_get(self, kind, key):
        return self._call("memory_get", kind, key)

    def memory_save(self, kind, key, value):
        return self._call("memory_save", kind, key, value)

    def memory_delete(self, kind, key):
        return self._call("memory_delete", kind, key)

    def mcp_server_list(self):
        return self._call("mcp_server_list")

    def mcp_server_get(self, server_id):
        return self._call("mcp_server_get", server_id)

    def mcp_server_save(self, data):
        return self._call("mcp_server_save", data)

    def mcp_server_delete(self, server_id):
        return self._call("mcp_server_delete", server_id)

    def mcp_host_list(self):
        return self._call("mcp_host_list")

    def mcp_host_add(self, host):
        return self._call("mcp_host_add", host)

    def mcp_host_remove(self, host):
        return self._call("mcp_host_remove", host)

    def create_session(self, session_id, title, model):
        return self._call("create_session", session_id, title, model)

    def ensure_session(self, session_id, title, model):
        return self._call("ensure_session", session_id, title, model)

    def add_message(self, session_id, role, content, model=None,
                    provider=None):
        return self._call("add_message", session_id, role, content,
                          model, provider)

    def list_messages(self, session_id, limit=20):
        return self._call("list_messages", session_id, limit)

    def resume_session(self, session_id):
        return self._call("resume_session", session_id)

    def delete_session(self, session_id):
        return self._call("delete_session", session_id)
