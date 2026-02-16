"""
Multi-agent orchestration.

Two interaction modes:
  - RoundTable: Full 4-phase deliberation (all agents, evidence-based consensus)
  - ChatOrchestrator: Lightweight real-time chat (1-3 agents, cross-checked)
"""
from .round_table import RoundTable, RoundTableConfig, AgentProtocol  # noqa: F401
from .chat_orchestrator import ChatOrchestrator, ChatConfig  # noqa: F401
from .agent_router import AgentRouter  # noqa: F401

__all__ = [
    "RoundTable",
    "RoundTableConfig",
    "AgentProtocol",
    "ChatOrchestrator",
    "ChatConfig",
    "AgentRouter",
]
