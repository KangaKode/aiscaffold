"""
EvidenceEnforcementPipeline -- orchestrates all evidence validators.

Runs FactChecker -> EvidenceLevelEnforcer -> CitationValidator -> MathVerifier
for Phase 1 round-table analyses. Rejected results can be routed through a
correction prompt when an LLM client is configured. If still failing, the
response passes through with warnings attached.

Keep this file under 150 lines.
"""

import logging
from typing import Any

from .citation_validator import CitationValidator, SourceRegistry
from .evidence_levels import EvidenceLevelEnforcer
from .fact_checker import FactChecker
from .math_verifier import GroundTruthProvider, MathVerifier
from .models import ValidationResult, Violation

logger = logging.getLogger(__name__)


class EvidenceEnforcementPipeline:
    """Orchestrates evidence validation and optional correction.

    Usage:
        pipeline = EvidenceEnforcementPipeline(llm_client=llm)
        result = await pipeline.validate("analyst", response_text, task)
        if result.outcome == "rejected" and result.corrected_content:
            # Use corrected_content instead
    """

    def __init__(
        self,
        llm_client: Any = None,
        source_registry: SourceRegistry | None = None,
        ground_truth: GroundTruthProvider | None = None,
        max_retries: int = 2,
    ):
        self._llm = llm_client
        self._max_retries = max_retries
        self._validators = [
            FactChecker(),
            EvidenceLevelEnforcer(),
            CitationValidator(registry=source_registry),
            MathVerifier(provider=ground_truth),
        ]

    async def validate(
        self, agent_name: str, response_text: str, task: Any = None
    ) -> ValidationResult:
        """Run all validators on response text. Reject+rewrite if needed."""
        all_violations: list[Violation] = []

        for validator in self._validators:
            result = validator.check(response_text)
            all_violations.extend(result.violations)

        critical_count = sum(1 for v in all_violations if v.severity == "critical")

        if critical_count >= 3 and self._llm:
            for attempt in range(self._max_retries):
                logger.info(
                    f"[Enforcement] {agent_name}: {critical_count} critical violations, "
                    f"attempting rewrite ({attempt + 1}/{self._max_retries})"
                )
                corrected = await self._rewrite(response_text, all_violations)
                if corrected:
                    recheck_violations: list[Violation] = []
                    for validator in self._validators:
                        r = validator.check(corrected)
                        recheck_violations.extend(r.violations)

                    recheck_critical = sum(
                        1 for v in recheck_violations if v.severity == "critical"
                    )
                    if recheck_critical < 3:
                        logger.info(
                            f"[Enforcement] {agent_name}: rewrite accepted "
                            f"({recheck_critical} critical remaining)"
                        )
                        return ValidationResult(
                            outcome="challenged" if recheck_violations else "accepted",
                            violations=recheck_violations,
                            corrected_content=corrected,
                        )
                    all_violations = recheck_violations
                    response_text = corrected

            logger.warning(
                f"[Enforcement] {agent_name}: rewrite failed after "
                f"{self._max_retries} attempts, passing with warnings"
            )
            return ValidationResult(
                outcome="challenged",
                violations=all_violations,
                corrected_content=response_text,
            )

        if critical_count >= 3:
            outcome = "rejected"
        elif all_violations:
            outcome = "challenged"
        else:
            outcome = "accepted"

        return ValidationResult(outcome=outcome, violations=all_violations)

    async def _rewrite(
        self, response_text: str, violations: list[Violation]
    ) -> str | None:
        """Send correction prompt to LLM with specific violations."""
        if not self._llm:
            return None

        try:
            from ..llm import CacheablePrompt

            violation_list = "\n".join(
                f"- [{v.severity.upper()}] {v.message} (found: '{v.location}'). "
                f"Fix: {v.suggestion}"
                for v in violations
                if v.severity == "critical"
            )

            prompt = CacheablePrompt(
                system=(
                    "You are a response corrector. Rewrite the agent response to fix "
                    "all listed violations. Preserve the original findings and evidence "
                    "but remove speculation, opinions, hedging, and fake confidence scores. "
                    "Use evidence level tags: [VERIFIED: source:ref], [CORROBORATED: src1 + src2], "
                    "[INDICATED: source], or [POSSIBLE]. Return ONLY the corrected response."
                ),
                context=f"Original response:\n{response_text[:3000]}",
                user_message=f"Fix these violations:\n{violation_list}",
            )

            response = await self._llm.call(
                prompt=prompt, role="enforcement_rewrite", temperature=0.1
            )
            return response.content if response and response.content else None
        except Exception as e:
            logger.warning(f"[Enforcement] Rewrite failed: {e}")
            return None
