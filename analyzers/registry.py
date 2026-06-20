"""
Analyzer registry and runner.

The registry collects all analyzer instances.
The runner takes a snapshot and returns findings from every compatible analyzer.
"""

from __future__ import annotations

from typing import Any

from analyzers.base import Analyzer, Finding


class AnalyzerRegistry:
    """
    Holds registered analyzers and runs them against snapshots.

    Usage:
        registry = AnalyzerRegistry()
        registry.register(PodCrashLoopAnalyzer())
        registry.register(DeploymentUnavailableAnalyzer())

        findings = registry.run(some_pod_snapshot)
    """

    def __init__(self) -> None:
        self._analyzers: list[Analyzer] = []

    def register(self, analyzer: Analyzer) -> None:
        """Add an analyzer to the registry."""
        self._analyzers.append(analyzer)

    @property
    def analyzers(self) -> list[Analyzer]:
        return list(self._analyzers)

    def run(self, snapshot: dict[str, Any]) -> list[Finding]:
        """
        Run all compatible analyzers against a snapshot.

        An analyzer is compatible if the snapshot's "kind"
        is in the analyzer's supported_kinds.

        Returns combined findings from all analyzers (may be empty).
        """
        kind = snapshot.get("kind", "")
        findings: list[Finding] = []

        for analyzer in self._analyzers:
            if kind in analyzer.supported_kinds:
                try:
                    findings.extend(analyzer.analyze(snapshot))
                except Exception as e:
                    resource = snapshot.get("metadata", {}).get("name", "?")
                    print(f"[Registry] {analyzer.name} crashed on {kind}/{resource}: {e}")

        return findings
