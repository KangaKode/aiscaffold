"""ScopeFilter -- enforces agent access_scopes on task context data.

Standalone module that filters task context by an agent's declared
access_scopes. Used by orchestrators before sending data to agents.

Defense-in-depth: even if an agent fabricates data source claims in its
output, the input filtering ensures it never received that data.
"""

from __future__ import annotations

import logging
from typing import Any

from ..agents.capability import AgentCapability

logger = logging.getLogger(__name__)


class ScopeFilter:
    """Filters task context and validates output against agent scopes."""

    def filter_data(
        self,
        task_context: dict[str, Any],
        capability: AgentCapability | None,
    ) -> dict[str, Any]:
        """Return only the task_context keys the agent is authorized to access.

        Agents with no capability, or with empty access_scopes, receive the
        full context unfiltered (zero-config backward-compatible default).

        Args:
            task_context: Full context dict from the task pipeline.
            capability: Agent's declared capability, or None for legacy agents.

        Returns:
            Filtered dict containing only authorized keys.
        """
        if capability is None or not capability.access_scopes:
            return task_context

        if capability.is_meta_agent:
            result = {}
            if "peer_analyses" in task_context:
                result["peer_analyses"] = task_context["peer_analyses"]
            for scope in capability.access_scopes:
                if scope in task_context:
                    result[scope] = task_context[scope]
            return result

        scopes = set(capability.access_scopes)
        return {k: v for k, v in task_context.items() if k in scopes}

    def get_available_sources(
        self,
        task_context: dict[str, Any],
        capability: AgentCapability | None,
    ) -> list[str]:
        """Return the list of data source keys available after filtering."""
        filtered = self.filter_data(task_context, capability)
        return list(filtered.keys())

    def check_output_sources(
        self,
        findings: list[dict[str, Any]],
        capability: AgentCapability | None,
    ) -> list[str]:
        """Check if agent findings cite data sources outside the agent's scopes.

        Each finding is a dict that may carry a ``data_sources`` list naming
        the context keys the finding is based on. Findings without that key
        are not checked. Agents with no capability or empty scopes are exempt.

        Returns a list of violation descriptions (empty if clean).
        """
        if capability is None or not capability.access_scopes:
            return []

        allowed = set(capability.access_scopes)
        if capability.is_meta_agent:
            allowed.add("peer_analyses")

        violations = []
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            description = str(finding.get("finding", ""))[:60]
            for source in finding.get("data_sources", []) or []:
                if source not in allowed:
                    logger.warning(
                        "[ScopeFilter] Output violation: finding cites '%s' outside access_scopes",
                        source,
                    )
                    violations.append(
                        f"Finding '{description}...' cites data_source '{source}' not in access_scopes"
                    )
        return violations
