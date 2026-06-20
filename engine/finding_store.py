from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from config import DB_PATH
from analyzers.base import Finding


class FindingStore:
    """Persists findings to SQLite and deduplicates within a cooldown window."""

    COOLDOWN_SECONDS = 300  # 5 minutes

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        """Create tables, indexes, and run lightweight column migrations."""
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resource_uid TEXT NOT NULL,
                analyzer TEXT NOT NULL,
                reason TEXT NOT NULL,
                resource TEXT NOT NULL,
                kind TEXT NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                owner_chain TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                suppressed_count INTEGER DEFAULT 0,
                log_snippet TEXT DEFAULT '',
                UNIQUE(resource_uid, analyzer, reason)
            )
        """)
        try:
            self.conn.execute("ALTER TABLE findings ADD COLUMN log_snippet TEXT DEFAULT ''")
            self.conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_findings_last_seen 
            ON findings(last_seen DESC)
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_findings_resource 
            ON findings(resource_uid)
        """)
        self.conn.commit()

    def filter_new(self, findings: list[Finding]) -> list[Finding]:
        """Return findings that are new or past cooldown; suppress the rest."""
        now_dt = datetime.utcnow().replace(tzinfo=None)
        now_iso = now_dt.isoformat()
        emit = []

        with self._lock:
            for f in findings:
                cursor = self.conn.execute(
                    "SELECT id, last_seen, suppressed_count FROM findings WHERE resource_uid = ? AND analyzer = ? AND reason = ?",
                    (f.resource_uid, f.analyzer, f.reason)
                )
                row = cursor.fetchone()

                if row is None:
                    cursor = self.conn.execute("""
                        INSERT INTO findings 
                        (resource_uid, analyzer, reason, resource, kind, severity, message, owner_chain, first_seen, last_seen, suppressed_count, log_snippet)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                    """, (
                        f.resource_uid,
                        f.analyzer,
                        f.reason,
                        f.resource,
                        f.kind,
                        f.severity.name,
                        f.message,
                        ",".join(f.owner_chain) if f.owner_chain else "",
                        f.timestamp or now_iso,
                        f.timestamp or now_iso,
                        f.log_snippet or "",
                    ))
                    self.conn.commit()
                    object.__setattr__(f, 'id', cursor.lastrowid)
                    emit.append(f)
                else:
                    finding_id, last_seen_iso, suppressed_count = row
                    last_seen_dt = datetime.fromisoformat(last_seen_iso)
                    time_since = (now_dt - last_seen_dt).total_seconds()

                    if time_since > self.COOLDOWN_SECONDS:
                        self.conn.execute("""
                            UPDATE findings 
                            SET last_seen = ?, message = ?, owner_chain = ?, suppressed_count = 0, log_snippet = ?
                            WHERE resource_uid = ? AND analyzer = ? AND reason = ?
                        """, (
                            now_iso,
                            f.message,
                            ",".join(f.owner_chain) if f.owner_chain else "",
                            f.log_snippet or "",
                            f.resource_uid,
                            f.analyzer,
                            f.reason
                        ))
                        self.conn.commit()
                        object.__setattr__(f, 'id', finding_id)
                        emit.append(f)
                    else:
                        self.conn.execute("""
                            UPDATE findings 
                            SET suppressed_count = suppressed_count + 1
                            WHERE resource_uid = ? AND analyzer = ? AND reason = ?
                        """, (f.resource_uid, f.analyzer, f.reason))
                        self.conn.commit()

        return emit

    def get_recent(self, limit: int = 50) -> list[dict]:
        """Return recent findings ordered by last_seen."""
        with self._lock:
            cursor = self.conn.execute("""
                SELECT resource, kind, severity, message, analyzer, reason, 
                       owner_chain, first_seen, last_seen, suppressed_count
                FROM findings
                ORDER BY last_seen DESC
                LIMIT ?
            """, (limit,))
            rows = cursor.fetchall()
        
        findings = []
        for row in rows:
            findings.append({
                "resource": row[0],
                "kind": row[1],
                "severity": row[2],
                "message": row[3],
                "analyzer": row[4],
                "reason": row[5],
                "owner_chain": row[6].split(",") if row[6] else [],
                "first_seen": row[7],
                "last_seen": row[8],
                "suppressed_count": row[9]
            })
        return findings

    def clear_old(self, max_age_days: int = 30):
        """Delete findings older than max_age_days to prevent DB bloat."""
        cutoff = datetime.utcnow().timestamp() - (max_age_days * 86400)
        cutoff_iso = datetime.fromtimestamp(cutoff).isoformat()

        with self._lock:
            self.conn.execute(
                "DELETE FROM findings WHERE last_seen < ?",
                (cutoff_iso,)
            )
            self.conn.commit()

    def find_by_owner_chain(
        self, owner_pattern: str, kind: str, cutoff_iso: str
    ) -> list[dict]:
        """Return findings matching an owner_chain LIKE pattern, kind, and recency cutoff."""
        with self._lock:
            rows = self.conn.execute("""
                SELECT id, resource, kind, severity, reason, owner_chain
                FROM findings
                WHERE kind = ?
                  AND owner_chain LIKE ?
                  AND severity IN ('CRITICAL', 'WARNING')
                  AND last_seen >= ?
                ORDER BY last_seen DESC
            """, (kind, owner_pattern, cutoff_iso)).fetchall()

        return [
            {
                "id":          r[0],
                "resource":    r[1],
                "scope":       r[2],
                "severity":    r[3],
                "reason":      r[4],
                "owner_chain": r[5],
            }
            for r in rows
        ]

    def close(self):
        """Close the database connection."""
        if self.conn:
            self.conn.close()
