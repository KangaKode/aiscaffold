"""
EvidenceLevelEnforcer -- validates provided evidence tags follow required format.

Four evidence levels (strongest to weakest):
  VERIFIED:      [VERIFIED: source:reference] -- direct proof at specific location
  CORROBORATED:  [CORROBORATED: source_1 + source_2] -- 2+ independent sources agree
  INDICATED:     [INDICATED: source_name] -- single source suggests pattern, gaps exist
  POSSIBLE:      [POSSIBLE] followed by what would confirm/deny

Keep this file under 120 lines.
"""

import logging
import re

from .models import ValidationResult, Violation

logger = logging.getLogger(__name__)

VERIFIED_PATTERN = re.compile(
    r"\[VERIFIED:\s*([^\]]+)\]", re.IGNORECASE
)
CORROBORATED_PATTERN = re.compile(
    r"\[CORROBORATED:\s*([^\]]+)\]", re.IGNORECASE
)
INDICATED_PATTERN = re.compile(
    r"\[INDICATED:\s*([^\]]+)\]", re.IGNORECASE
)
POSSIBLE_PATTERN = re.compile(
    r"\[POSSIBLE\]", re.IGNORECASE
)


class EvidenceLevelEnforcer:
    """Validates evidence level tags in agent responses.

    Usage:
        enforcer = EvidenceLevelEnforcer()
        result = enforcer.check("[VERIFIED: okta_logs:row_456] User logged in")
        # result.outcome == "accepted"
    """

    def check(self, text: str) -> ValidationResult:
        """Validate all evidence level tags in text."""
        violations = []

        for match in VERIFIED_PATTERN.finditer(text):
            content = match.group(1).strip()
            if ":" not in content:
                violations.append(Violation(
                    rule="evidence_level:verified_missing_reference",
                    severity="critical",
                    message="VERIFIED claims must cite source:reference (e.g. [VERIFIED: logs:row_42])",
                    location=match.group(0),
                    suggestion=f"Add a specific reference: [VERIFIED: {content}:reference]",
                ))

        for match in CORROBORATED_PATTERN.finditer(text):
            content = match.group(1).strip()
            sources = [s.strip() for s in content.split("+")]
            if len(sources) < 2:
                violations.append(Violation(
                    rule="evidence_level:corroborated_insufficient_sources",
                    severity="critical",
                    message="CORROBORATED claims must name 2+ sources separated by + (e.g. [CORROBORATED: logs + alerts])",
                    location=match.group(0),
                    suggestion=f"Add a second source: [CORROBORATED: {content} + another_source]",
                ))

        for match in INDICATED_PATTERN.finditer(text):
            content = match.group(1).strip()
            if not content:
                violations.append(Violation(
                    rule="evidence_level:indicated_missing_source",
                    severity="warning",
                    message="INDICATED claims should name the source (e.g. [INDICATED: access_logs])",
                    location=match.group(0),
                    suggestion="Name the data source: [INDICATED: source_name]",
                ))

        outcome = "challenged" if violations else "accepted"
        return ValidationResult(outcome=outcome, violations=violations)
