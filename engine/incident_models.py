from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class IncidentStatus(Enum):
    """Lifecycle states for an incident."""
    ACTIVE = "ACTIVE"
    RESOLVED = "RESOLVED"
    ACKNOWLEDGED = "ACKNOWLEDGED"


class FindingRole(Enum):
    """How a finding relates to an incident."""
    ROOT_CAUSE = "ROOT_CAUSE"
    SYMPTOM = "SYMPTOM"
    RELATED = "RELATED"
    CONTRIBUTING = "CONTRIBUTING"


@dataclass
class Incident:
    """A group of correlated findings that represents a single operational problem."""
    id: str
    title: str
    scope: str
    resource: str
    resource_uid: str
    severity: str
    status: IncidentStatus
    root_cause: str | None
    correlation_rule: str
    correlation_reason: str
    first_seen: datetime
    last_seen: datetime
    resolved_at: datetime | None = None
    finding_count: int = 0
    suppressed_symptom_count: int = 0
    
    def to_dict(self) -> dict:
        """Convert to dict for serialization (DB, JSON, LLM context)."""
        return {
            "id": self.id,
            "title": self.title,
            "scope": self.scope,
            "resource": self.resource,
            "resource_uid": self.resource_uid,
            "severity": self.severity,
            "status": self.status.value,
            "root_cause": self.root_cause,
            "correlation_rule": self.correlation_rule,
            "correlation_reason": self.correlation_reason,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "finding_count": self.finding_count,
            "suppressed_symptom_count": self.suppressed_symptom_count
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'Incident':
        """Create incident from dict (for DB deserialization)."""
        return cls(
            id=data["id"],
            title=data["title"],
            scope=data["scope"],
            resource=data["resource"],
            resource_uid=data["resource_uid"],
            severity=data["severity"],
            status=IncidentStatus(data["status"]),
            root_cause=data.get("root_cause"),
            correlation_rule=data["correlation_rule"],
            correlation_reason=data["correlation_reason"],
            first_seen=datetime.fromisoformat(data["first_seen"]),
            last_seen=datetime.fromisoformat(data["last_seen"]),
            resolved_at=datetime.fromisoformat(data["resolved_at"]) if data.get("resolved_at") else None,
            finding_count=data.get("finding_count", 0),
            suppressed_symptom_count=data.get("suppressed_symptom_count", 0)
        )


@dataclass
class IncidentFinding:
    """Junction record linking a finding to an incident with a role."""
    incident_id: str
    finding_id: int
    role: FindingRole
    added_at: datetime
    
    def to_dict(self) -> dict:
        """Convert to dict for serialization."""
        return {
            "incident_id": self.incident_id,
            "finding_id": self.finding_id,
            "role": self.role.value,
            "added_at": self.added_at.isoformat()
        }
