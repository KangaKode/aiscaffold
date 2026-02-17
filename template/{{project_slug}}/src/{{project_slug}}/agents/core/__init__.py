"""
Core safety agents -- meta-agents that ensure deliberation quality.

These agents automatically participate in every round table session.
They evaluate the process (reasoning, completeness, evidence) rather
than the domain content. Disable with include_core_agents=False in
RoundTableConfig if you have a specific reason to opt out.
"""

from .evidence import EvidenceAgent
from .quality import QualityAgent
from .skeptic import SkepticAgent


def get_core_agents(llm_client=None) -> list:
    """Create and return all core safety agents.

    Args:
        llm_client: LLM client for agent reasoning. Without it,
            core agents return static placeholder analyses.
    """
    return [
        SkepticAgent(llm_client=llm_client),
        QualityAgent(llm_client=llm_client),
        EvidenceAgent(llm_client=llm_client),
    ]
