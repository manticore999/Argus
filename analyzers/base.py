"""
Core abstractions for the analyzer framework.

An Analyzer is a stateless, deterministic function that inspects
a single resource snapshot and returns zero or more Findings.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Severity(Enum):
    """Issue severity levels, ordered by urgency (higher value = more severe)."""
    INFO = 0
    WARNING = 1
    CRITICAL = 2


@dataclass(frozen=True)
class Finding:
    """
    A single detected issue from an analyzer (atomic observation).

    Attributes:
        analyzer:    Name of the analyzer that produced this finding.
        resource:    Identifier string, e.g. "default/my-pod".
        kind:        Kubernetes resource kind, e.g. "Pod".
        severity:    How urgent is this.
        message:     Human-readable explanation of the issue.
        reason:      Machine-readable failure reason, e.g. "CrashLoopBackOff".
        resource_uid: UID of the resource (for deduplication).
        owner_chain: List of owner resource identifiers (e.g. ["Deployment/default/api-server"]).
        timestamp:   When this finding was observed.
        raw_context: Optional dict with extra data for downstream consumers.
        id:          Database ID (set by FindingStore after persistence).
    """
    analyzer: str
    resource: str
    kind: str
    severity: Severity
    message: str
    reason: str
    resource_uid: str = "unknown"
    owner_chain: list[str] = field(default_factory=list)
    timestamp: str = ""  # ISO format, set by caller
    raw_context: dict[str, Any] = field(default_factory=dict)
    # Last N lines of container logs at time of detection.
    # Empty string if the container hadn't started yet or logs were unavailable.
    # This is the primary context the LLM will use to diagnose root cause.
    log_snippet: str = ""
    id: int | None = None  # DB ID, set after persistence


class Analyzer(ABC):
    """
    Base class for all analyzers.

    Subclasses declare which resource kinds they handle
    and implement the `analyze` method.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique name for this analyzer."""

    @property
    @abstractmethod
    def supported_kinds(self) -> list[str]:
        """Kubernetes resource kinds this analyzer can inspect."""

    @abstractmethod
    def analyze(self, snapshot: dict[str, Any]) -> list[Finding]:
        """
        Inspect a resource snapshot and return findings.

        Args:
            snapshot: A dict representing one Kubernetes resource.
                      Must contain at least {"kind": "...", "metadata": {...}, ...}.

        Returns:
            A list of Finding objects. Empty list means no issues detected.
        """
