"""
Multi-agent orchestration.

Two interaction modes:
  - RoundTable: Full 4-phase deliberation (all agents, evidence-based consensus)
  - ChatOrchestrator: Lightweight real-time chat (1-3 agents, cross-checked)
"""
from .round_table import RoundTable, RoundTableConfig, AgentProtocol  # noqa: F401
from .chat_orchestrator import ChatOrchestrator, ChatConfig  # noqa: F401
from .agent_router import AgentRouter  # noqa: F401
from .scope_filter import ScopeFilter  # noqa: F401
from .identity_check import verify_agent_identity  # noqa: F401
from .autonomy import AutonomyPolicy, DEFAULT_POLICIES, resolve_policy  # noqa: F401
from .deliberation_audit import (  # noqa: F401
    DeliberationAuditor,
    audited_round_table,
    new_correlation_id,
)

__all__ = [
    "RoundTable",
    "RoundTableConfig",
    "AgentProtocol",
    "ChatOrchestrator",
    "ChatConfig",
    "AgentRouter",
    "ScopeFilter",
    "verify_agent_identity",
    "AutonomyPolicy",
    "DEFAULT_POLICIES",
    "resolve_policy",
    "DeliberationAuditor",
    "audited_round_table",
    "new_correlation_id",
]
