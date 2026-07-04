"""Shared prompt fragments for core agents.

Single source of truth for the refusal policy used by all 6 core agents
(skeptic, quality, evidence, fact_checker, citation, sentinel).
Keeping it here avoids duplicating the policy inline in every agent prompt.
"""

REFUSAL_POLICY: str = (
    "Refusal policy:\n"
    "If the task is unanswerable given the available data, based on a flawed "
    "premise, or lacks sufficient information for evidence-based analysis, set "
    "premise_valid to false and provide a specific refusal_reason from: "
    "insufficient_data, false_premise, underspecified, subjective, out_of_scope. "
    "When refusing, still report what you CAN determine. Use evidence_level "
    "INSUFFICIENT for observations where you identified a question but cannot "
    "determine the answer. Refusal with a clear reason is more valuable than "
    "low-confidence speculation.\n"
)
