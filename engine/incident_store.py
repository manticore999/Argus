from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

from config import DB_PATH
from engine.incident_models import Incident, IncidentStatus, FindingRole


class IncidentStore:
    """Persistence layer for incidents and incident-finding relationships."""
    
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._init_db()
    
    def _init_db(self):
        """Create tables and indexes; run lightweight column migrations."""
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.row_factory = sqlite3.Row
        
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS incidents (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                scope TEXT NOT NULL,
                resource TEXT NOT NULL,
                resource_uid TEXT NOT NULL,
                severity TEXT NOT NULL,
                status TEXT NOT NULL,
                root_cause TEXT,
                correlation_rule TEXT NOT NULL,
                correlation_reason TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                resolved_at TEXT,
                finding_count INTEGER DEFAULT 0,
                suppressed_symptom_count INTEGER DEFAULT 0,
                diagnosis TEXT DEFAULT NULL
            )
        """)

        try:
            self.conn.execute("ALTER TABLE incidents ADD COLUMN diagnosis TEXT DEFAULT NULL")
            self.conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists
        
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS incident_findings (
                incident_id TEXT NOT NULL,
                finding_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                added_at TEXT NOT NULL,
                PRIMARY KEY (incident_id, finding_id),
                FOREIGN KEY (incident_id) REFERENCES incidents(id) ON DELETE CASCADE
            )
        """)
        
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_incidents_status 
            ON incidents(status, last_seen DESC)
        """)
        
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_incidents_resource 
            ON incidents(scope, resource_uid, status)
        """)
        
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_incident_findings_incident 
            ON incident_findings(incident_id)
        """)
        
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_incident_findings_role 
            ON incident_findings(finding_id, role)
        """)
        
        self.conn.commit()
    
    def create_incident(
        self,
        title: str,
        scope: str,
        resource: str,
        resource_uid: str,
        severity: str,
        root_cause: str | None,
        correlation_rule: str,
        correlation_reason: str
    ) -> Incident:
        """Create and persist a new incident; return the Incident object."""
        incident_id = str(uuid.uuid4())
        now = datetime.utcnow()
        
        incident = Incident(
            id=incident_id,
            title=title,
            scope=scope,
            resource=resource,
            resource_uid=resource_uid,
            severity=severity,
            status=IncidentStatus.ACTIVE,
            root_cause=root_cause,
            correlation_rule=correlation_rule,
            correlation_reason=correlation_reason,
            first_seen=now,
            last_seen=now,
            resolved_at=None,
            finding_count=0,
            suppressed_symptom_count=0
        )
        
        with self._lock:
            self.conn.execute("""
                INSERT INTO incidents (
                    id, title, scope, resource, resource_uid, severity, status,
                    root_cause, correlation_rule, correlation_reason,
                    first_seen, last_seen, resolved_at, finding_count, suppressed_symptom_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                incident.id,
                incident.title,
                incident.scope,
                incident.resource,
                incident.resource_uid,
                incident.severity,
                incident.status.value,
                incident.root_cause,
                incident.correlation_rule,
                incident.correlation_reason,
                incident.first_seen.isoformat(),
                incident.last_seen.isoformat(),
                None,
                incident.finding_count,
                incident.suppressed_symptom_count
            ))
            self.conn.commit()

        print(f"[Incident] Created {incident_id} — {title} ({scope}/{resource})")
        return incident
    
    def link_finding_to_incident(
        self,
        incident_id: str,
        finding_id: int,
        role: FindingRole
    ) -> None:
        """Link a finding to an incident with a FindingRole."""
        added_at = datetime.utcnow()
        
        with self._lock:
            self.conn.execute("""
                INSERT OR IGNORE INTO incident_findings (
                    incident_id, finding_id, role, added_at
                ) VALUES (?, ?, ?, ?)
            """, (incident_id, finding_id, role.value, added_at.isoformat()))
            
            if role == FindingRole.SYMPTOM:
                self.conn.execute("""
                    UPDATE incidents 
                    SET suppressed_symptom_count = suppressed_symptom_count + 1,
                        finding_count = finding_count + 1,
                        last_seen = ?
                    WHERE id = ?
                """, (datetime.utcnow().isoformat(), incident_id))
            else:
                self.conn.execute("""
                    UPDATE incidents 
                    SET finding_count = finding_count + 1,
                        last_seen = ?
                    WHERE id = ?
                """, (datetime.utcnow().isoformat(), incident_id))
            
            self.conn.commit()

    def get_incident_by_id(self, incident_id: str) -> Incident | None:
        """Return the Incident for the given id, or None."""
        with self._lock:
            cursor = self.conn.execute("""
                SELECT * FROM incidents WHERE id = ?
            """, (incident_id,))
            row = cursor.fetchone()
        
        if not row:
            return None
        
        return self._row_to_incident(row)
    
    def find_incident_by_resource(
        self,
        scope: str,
        resource_uid: str,
        status: IncidentStatus = IncidentStatus.ACTIVE
    ) -> Incident | None:
        """Return the most recent incident matching scope + resource_uid + status, or None."""
        with self._lock:
            cursor = self.conn.execute("""
                SELECT * FROM incidents 
                WHERE scope = ? AND resource_uid = ? AND status = ?
                ORDER BY last_seen DESC
                LIMIT 1
            """, (scope, resource_uid, status.value))
            row = cursor.fetchone()
        
        if not row:
            return None
        
        return self._row_to_incident(row)
    
    def list_active_incidents(self) -> list[Incident]:
        """Return all ACTIVE incidents ordered by last_seen descending."""
        with self._lock:
            cursor = self.conn.execute("""
                SELECT * FROM incidents 
                WHERE status = ?
                ORDER BY last_seen DESC
            """, (IncidentStatus.ACTIVE.value,))
            return [self._row_to_incident(row) for row in cursor.fetchall()]
    
    def resolve_incident(self, incident_id: str) -> None:
        """Mark an incident as RESOLVED."""
        now = datetime.utcnow()
        with self._lock:
            self.conn.execute("""
                UPDATE incidents 
                SET status = ?, resolved_at = ?
                WHERE id = ?
            """, (IncidentStatus.RESOLVED.value, now.isoformat(), incident_id))
            self.conn.commit()
        
        incident = self.get_incident_by_id(incident_id)
        if incident:
            print(f"[Incident] Resolved {incident_id} — {incident.title}")
    
    def reopen_incident(self, incident_id: str) -> None:
        """Re-open a resolved incident."""
        now = datetime.utcnow()
        with self._lock:
            self.conn.execute("""
                UPDATE incidents 
                SET status = ?, resolved_at = NULL, last_seen = ?
                WHERE id = ?
            """, (IncidentStatus.ACTIVE.value, now.isoformat(), incident_id))
            self.conn.commit()
        print(f"[Incident] Re-opened {incident_id}")

    def _row_to_incident(self, row: sqlite3.Row) -> Incident:
        """Convert a database row to an Incident object."""
        return Incident.from_dict(dict(row))
    
    def save_diagnosis(self, incident_id: str, diagnosis) -> None:
        """Persist a Diagnosis object as JSON on the incident row."""
        import json
        with self._lock:
            self.conn.execute(
                "UPDATE incidents SET diagnosis = ? WHERE id = ?",
                (json.dumps(diagnosis.model_dump()), incident_id),
            )
            self.conn.commit()

    def get_diagnosis(self, incident_id: str) -> dict | None:
        """Return the stored diagnosis dict for an incident, or None."""
        import json
        with self._lock:
            row = self.conn.execute(
                "SELECT diagnosis FROM incidents WHERE id = ?", (incident_id,)
            ).fetchone()
        if not row or not row["diagnosis"]:
            return None
        return json.loads(row["diagnosis"])

    def get_linked_findings(self, incident_id: str) -> list[dict]:
        """Return findings linked to an incident with their role."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT f.*, if2.role FROM incident_findings if2 "
                "JOIN findings f ON f.id = if2.finding_id "
                "WHERE if2.incident_id = ?",
                (incident_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def search_incidents(
        self, resource_pattern: str = "", limit: int = 10
    ) -> list[dict]:
        """Return recent incidents matching an optional resource LIKE pattern."""
        with self._lock:
            if resource_pattern:
                rows = self.conn.execute(
                    "SELECT id, title, severity, status, correlation_rule, finding_count, "
                    "first_seen, last_seen, resolved_at, diagnosis IS NOT NULL AS has_diagnosis "
                    "FROM incidents WHERE resource LIKE ? ORDER BY last_seen DESC LIMIT ?",
                    (f"%{resource_pattern}%", limit),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT id, title, severity, status, correlation_rule, finding_count, "
                    "first_seen, last_seen, resolved_at, diagnosis IS NOT NULL AS has_diagnosis "
                    "FROM incidents ORDER BY last_seen DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        """Close the database connection."""
        if self.conn:
            self.conn.close()
