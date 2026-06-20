"""Chat session persistence with rolling conversation summarisation."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import DB_PATH


RECENT_WINDOW     = 12
SUMMARY_THRESHOLD = 24
MAX_HISTORY       = 40


class SessionStore:
    """Lightweight SQLite store for chat sessions with rolling summarisation."""

    def __init__(self, db_path: str = DB_PATH) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_tables()

    def _init_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id          TEXT PRIMARY KEY,
                title       TEXT NOT NULL DEFAULT '',
                summary     TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chat_messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT NOT NULL REFERENCES chat_sessions(id),
                role        TEXT NOT NULL,
                content     TEXT NOT NULL,
                timestamp   TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_messages_session
                ON chat_messages(session_id, id);
        """)
        # Migrate: add summary column if upgrading from old schema
        try:
            self._conn.execute(
                "ALTER TABLE chat_sessions ADD COLUMN summary TEXT NOT NULL DEFAULT ''"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
        self._conn.commit()

    # ── Session CRUD ─────────────────────────────────────────────────────────

    def create_session(self, title: str = "") -> str:
        """Create a new session and return its ID."""
        sid = str(uuid.uuid4())
        now = _now_iso()
        self._conn.execute(
            "INSERT INTO chat_sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (sid, title, now, now),
        )
        self._conn.commit()
        return sid

    def list_sessions(self, limit: int = 20) -> list[dict]:
        """Return recent sessions, newest first."""
        rows = self._conn.execute(
            "SELECT id, title, created_at, updated_at FROM chat_sessions "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_session(self, session_id: str) -> None:
        """Delete a session and all its messages."""
        self._conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
        self._conn.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
        self._conn.commit()

    # ── Message operations ───────────────────────────────────────────────────

    def append_message(self, session_id: str, role: str, content: Any) -> None:
        """
        Append a message to the session.

        ``content`` can be a string (plain text message) or any JSON-serialisable
        structure (e.g. a list of Gemini Part dicts for tool calls/responses).
        """
        now = _now_iso()
        serialised = content if isinstance(content, str) else json.dumps(content)
        self._conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, timestamp) "
            "VALUES (?, ?, ?, ?)",
            (session_id, role, serialised, now),
        )
        self._conn.execute(
            "UPDATE chat_sessions SET updated_at = ? WHERE id = ?",
            (now, session_id),
        )
        self._conn.commit()

    def get_history(self, session_id: str, limit: int = MAX_HISTORY) -> list[dict]:
        """
        Load recent messages for a session.

        Returns list of ``{"role": ..., "content": ...}`` dicts in chronological order.
        Content is returned as-is (string) — the caller can json.loads() if needed.
        """
        rows = self._conn.execute(
            "SELECT role, content FROM ("
            "  SELECT role, content, id FROM chat_messages "
            "  WHERE session_id = ? ORDER BY id DESC LIMIT ?"
            ") sub ORDER BY id ASC",
            (session_id, limit),
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in rows]

    # ── Summary operations ───────────────────────────────────────────────────

    def get_summary(self, session_id: str) -> str:
        """Return the rolling conversation summary (empty string if none)."""
        row = self._conn.execute(
            "SELECT summary FROM chat_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return row["summary"] if row else ""

    def update_summary(self, session_id: str, summary: str) -> None:
        """Replace the rolling summary for a session."""
        self._conn.execute(
            "UPDATE chat_sessions SET summary = ? WHERE id = ?",
            (summary, session_id),
        )
        self._conn.commit()

    def message_count(self, session_id: str) -> int:
        """Return total number of messages in a session."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM chat_messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return row["cnt"] if row else 0

    def get_oldest_messages(self, session_id: str, count: int) -> list[dict]:
        """
        Return the *oldest* ``count`` messages (id, role, content) in order.
        Used to grab the batch that will be compressed into a summary.
        """
        rows = self._conn.execute(
            "SELECT id, role, content FROM chat_messages "
            "WHERE session_id = ? ORDER BY id ASC LIMIT ?",
            (session_id, count),
        ).fetchall()
        return [{"id": r["id"], "role": r["role"], "content": r["content"]} for r in rows]

    def delete_messages_up_to(self, session_id: str, max_id: int) -> int:
        """
        Delete messages with id ≤ ``max_id`` for the given session.
        Returns the number of rows deleted.
        """
        cur = self._conn.execute(
            "DELETE FROM chat_messages WHERE session_id = ? AND id <= ?",
            (session_id, max_id),
        )
        self._conn.commit()
        return cur.rowcount

    def session_exists(self, session_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM chat_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return row is not None

    def update_title(self, session_id: str, title: str) -> None:
        self._conn.execute(
            "UPDATE chat_sessions SET title = ? WHERE id = ?",
            (title, session_id),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
