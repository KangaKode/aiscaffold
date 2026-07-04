"""
Evidence Enforcement Pipeline -- validates agent responses for quality and honesty.

Runs after each agent's analysis (Phase 1) and before challenge (Phase 2).
Responses that fail validation can be rejected or routed through an LLM
correction prompt when enforcement is configured for that workflow.

Components:
  - FactChecker: Scans for banned speculation/opinion/hedging language
  - EvidenceLevelEnforcer: Validates VERIFIED/CORROBORATED/INDICATED/POSSIBLE format
  - CitationValidator: Checks that cited sources exist (pluggable)
  - MathVerifier: Validates numeric claims against ground truth (pluggable)
  - EvidenceEnforcementPipeline: Orchestrates validators and optional correction
"""

from .models import ValidationResult, Violation
from .pipeline import EvidenceEnforcementPipeline
from .signer import SignedOutput, sign_output, verify_output

__all__ = [
    "EvidenceEnforcementPipeline",
    "ValidationResult",
    "Violation",
    "SignedOutput",
    "sign_output",
    "verify_output",
]
