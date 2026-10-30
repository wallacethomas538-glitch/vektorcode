"""Persistent memory — durable local memory for VektorCode (Phase 6).

Local-first: SQLite file under ~/.vektor/. No cloud, no sync.
"""
import os
import sqlite3
import time

KINDS = ("fact", "preference", "decision", "lesson")

_SCHEMA = ("CREATE TABLE IF NOT EXISTS memory("
           "kind TEXT, key TEXT, value TEXT, updated_at TEXT, "
           "PRIMARY KEY(kind, key))")


class Memory:
    def __init__(self, path: str | None = None):
        self.path = path or os.path.expanduser(
            "~/.vektor/vektorcode_memory.db")
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def remember(self, kind: str, key: str, value: str) -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown memory kind: {kind!r}")
        self._conn.execute(
            "INSERT OR REPLACE INTO memory(kind, key, value, updated_at)"
            " VALUES (?,?,?,?)",
            (kind, key, value, time.strftime("%Y-%m-%dT%H:%M:%S")))
        self._conn.commit()

    def recall(self, kind: str, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM memory WHERE kind=? AND key=?",
            (kind, key)).fetchone()
        return row[0] if row else None

    def recall_all(self, kind: str) -> list:
        rows = self._conn.execute(
            "SELECT key, value, updated_at FROM memory WHERE kind=? ORDER BY key",
            (kind,)).fetchall()
        return [{"key": k, "value": v, "updated_at": t} for k, v, t in rows]

    def close(self) -> None:
        self._conn.close()
