"""
Core safety agents -- meta-agents that ensure deliberation quality.

These agents automatically participate in every round table session.
They evaluate the process (reasoning, completeness, evidence) rather
than the domain content. Disable with include_core_agents=False in
RoundTableConfig if you have a specific reason to opt out.

Six core agents:
  - Skeptic: challenges assumptions, demands evidence
  - Quality: tracks requirement coverage, finds gaps
  - Evidence: grades claim strength, flags speculation
  - FactChecker: challenges speculation language in deliberation
  - Citation: enforces evidence level tagging on all findings
  - Sentinel: AI-powered guard screening input for extraction and output for leaks
"""

from .citation_agent import CitationAgent
from .evidence import EvidenceAgent
from .fact_checker_agent import FactCheckerAgent
from .quality import QualityAgent
from .sentinel import SentinelAgent
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
        FactCheckerAgent(llm_client=llm_client),
        CitationAgent(llm_client=llm_client),
        SentinelAgent(llm_client=llm_client),
    ]
