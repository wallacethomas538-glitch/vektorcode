"""Durable dashboard storage.

Supabase REST is used when SUPABASE_URL and SUPABASE_KEY are set. Keys are
read from the environment only. When they are absent — or Supabase is
unreachable — the existing SQLite file remains the fallback so the dashboard
keeps working.

Expected Supabase tables are in supabase/schema.sql.
"""
import json
import os
import sqlite3
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

SETTINGS_TABLE = "vektor_settings"
SESSIONS_TABLE = "vektor_sessions"
RECEIPTS_TABLE = "vektor_receipts"
MESSAGES_TABLE = "vektor_messages"

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
        return self._call("list_sessions")

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
