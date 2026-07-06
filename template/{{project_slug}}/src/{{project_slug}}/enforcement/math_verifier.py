"""
MathVerifier -- validates numeric claims against ground truth.

Uses a pluggable GroundTruthProvider protocol so projects can wire in
their computed data. Ships with a no-op default that skips verification.

Keep this file under 120 lines. (Raised from 80: security-review fixes
grew the module past its original budget.)
"""

import logging
import re
from typing import Protocol, runtime_checkable

from .models import ValidationResult, Violation

logger = logging.getLogger(__name__)

NUMERIC_CLAIM_PATTERN = re.compile(
    r"(\d+\.?\d*)\s*%|(\d+\.?\d*)\s+(instances?|occurrences?|events?|records?|times?)"
)


@runtime_checkable
class GroundTruthProvider(Protocol):
    """Protocol for providing numeric ground truth values.

    Projects implement this to connect their computed metrics.
    """

    def get_value(self, metric_name: str) -> float | None:
        """Get a computed ground truth value. Returns None if unknown."""
        ...


class DefaultGroundTruthProvider:
    """No-op provider that skips all verification.

    Used when a project hasn't configured its own ground truth.
    """

    def get_value(self, metric_name: str) -> float | None:
        return None


class MathVerifier:
    """Validates numeric claims against ground truth when available.

    Usage:
        verifier = MathVerifier(provider=MyDataProvider())
        result = verifier.check("The error rate was 12.3%")
    """

    def __init__(self, provider: GroundTruthProvider | None = None):
        self._provider = provider or DefaultGroundTruthProvider()

    def check(self, text: str) -> ValidationResult:
        """Check numeric claims in text against ground truth. No-op with default provider."""
        if isinstance(self._provider, DefaultGroundTruthProvider):
            return ValidationResult(outcome="accepted", violations=[])

        violations = []
        for match in NUMERIC_CLAIM_PATTERN.finditer(text):
            claimed_str = match.group(1) or match.group(2)
            if not claimed_str:
                continue
            try:
                claimed = float(claimed_str)
            except ValueError:
                continue

            context = text[max(0, match.start() - 50):match.end() + 50]
            ground_truth = self._provider.get_value(context)
            if ground_truth is not None and abs(claimed - ground_truth) > 0.01:
                violations.append(Violation(
                    rule="math:numeric_mismatch",
                    severity="critical",
                    message=f"Claimed {claimed_str} but ground truth is {ground_truth}",
                    location=match.group(0),
                    suggestion=f"Correct the value to {ground_truth}",
                ))

        return ValidationResult(
            outcome="challenged" if violations else "accepted",
            violations=violations,
        )
