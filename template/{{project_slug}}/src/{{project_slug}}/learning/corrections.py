"""
Corrections -- user-approved fixes to agent claims.

When an agent states something wrong, a user (or another agent) proposes a
correction. Corrections only influence behavior after human approval, then
get rendered into a prompt-injectable block so agents stop repeating the
original claim.

Lifecycle: PROPOSED -> APPROVED -> RETIRED (REJECTED is a terminal branch).

Four-eyes rule: by default the approver must be a different person than the
proposer (require_four_eyes=True). Single-operator deployments can pass
require_four_eyes=False to allow self-approval.

Security: PII is redacted from correction text before it is persisted
(security.pii), and rendered correction text passes through
sanitize_for_prompt so stored content cannot inject into prompts.
Rendering respects a character budget (CORRECTION_CONTEXT_BUDGET env
override). An optional ContentPolicy can gate propose().

Keep this file under 400 lines.
"""

import json
import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..security.pii import redact_pii
from ..security.prompt_guard import sanitize_for_prompt
from .store import LearningStore

logger = logging.getLogger(__name__)

# Correction lifecycle statuses
STATUS_PROPOSED = "proposed"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_RETIRED = "retired"

DEFAULT_CONTEXT_BUDGET_CHARS = 4000
MAX_FIELD_RENDER_CHARS = 500


@dataclass
class Correction:
    """
    A correction to an agent claim -- mirrors the corrections table.

    evidence_level: How well-supported the correction is. Projects define
                    their own scale (e.g., "anecdotal", "documented",
                    "verified").
    status: One of the STATUS_* constants.
    created_by / approved_by: User identifiers for the four-eyes check.
    """

    agent_id: str = ""
    original_claim: str = ""
    corrected_claim: str = ""
    reason: str = ""
    evidence_level: str = ""
    tenant_id: str = "default"
    session_id: str = ""
    status: str = STATUS_PROPOSED
    created_by: str = ""
    approved_by: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())


class CorrectionsManager:
    """
    Manages the correction lifecycle on top of a LearningStore.

    Usage:
        store = get_learning_store()
        mgr = CorrectionsManager(store, checkin_manager=CheckInManager())

        c = mgr.propose(
            agent_id="analyst",
            original_claim="The API rate limit is 100/min",
            corrected_claim="The API rate limit is 60/min",
            reason="Confirmed in vendor docs",
            evidence_level="documented",
            created_by="alice",
        )
        mgr.approve(c.id, approved_by="bob")  # four-eyes: bob != alice

        block = mgr.get_approved_for_context(agent_id="analyst")
        # -> prompt-injectable text, capped at the character budget

    on_approve: optional callback invoked (best-effort) with the approved
    Correction. Use it to feed downstream systems -- e.g., wire an
    AgentTrustManager by building a FeedbackSignal(signal_type=
    SignalType.MODIFY, agent_id=correction.agent_id) and calling
    update_from_signal.

    content_policy: optional ContentPolicy instance. When set, propose()
    screens the correction text: "rejected" raises ValueError, "flagged"
    is recorded in metadata but still routes to the normal check-in flow.
    """

    def __init__(
        self,
        store: LearningStore,
        checkin_manager=None,
        require_four_eyes: bool = True,
        on_approve: Callable[[Correction], None] | None = None,
        content_policy=None,
    ):
        self._store = store
        self._checkin_manager = checkin_manager
        self._require_four_eyes = require_four_eyes
        self._on_approve = on_approve
        self._content_policy = content_policy

    def propose(
        self,
        agent_id: str,
        original_claim: str,
        corrected_claim: str,
        reason: str,
        evidence_level: str,
        tenant_id: str = "default",
        session_id: str = "",
        created_by: str = "",
    ) -> Correction:
        """
        Persist a PROPOSED correction and (if configured) open a check-in.

        PII in original_claim / corrected_claim / reason is redacted
        before anything is persisted (counts land in metadata). When a
        content policy is configured, "rejected" text raises ValueError;
        "flagged" text is recorded in metadata and still proposed.
        """
        metadata: dict[str, Any] = {}

        pii_counts: dict[str, int] = {}
        redacted_fields = []
        for text in (original_claim, corrected_claim, reason):
            redacted, counts = redact_pii(text)
            redacted_fields.append(redacted)
            for category, n in counts.items():
                pii_counts[category] = pii_counts.get(category, 0) + n
        original_claim, corrected_claim, reason = redacted_fields
        if pii_counts:
            metadata["pii_redacted"] = pii_counts
            logger.info(f"[Corrections] Redacted PII in proposal: {pii_counts}")

        if self._content_policy is not None:
            outcome, reasons = self._content_policy.screen_knowledge_write(
                f"{original_claim}\n{corrected_claim}\n{reason}",
                tenant_id=tenant_id,
            )
            if outcome == "rejected":
                raise ValueError(
                    f"Correction rejected by content policy: {reasons}"
                )
            if outcome == "flagged":
                metadata["content_policy"] = {
                    "outcome": outcome,
                    "reasons": reasons,
                }

        correction = Correction(
            agent_id=agent_id,
            original_claim=original_claim,
            corrected_claim=corrected_claim,
            reason=reason,
            evidence_level=evidence_level,
            tenant_id=tenant_id,
            session_id=session_id,
            created_by=created_by,
            metadata=metadata,
        )
        self._store.insert("corrections", self._to_row(correction))
        logger.info(f"[Corrections] Proposed {correction.id} for agent '{agent_id}'")

        if self._checkin_manager is not None:
            try:
                self._checkin_manager.create(
                    checkin_type="correction",
                    prompt=(
                        f"Approve correction for agent '{agent_id}'?\n"
                        f"Instead of: {original_claim}\n"
                        f"Use: {corrected_claim}\n"
                        f"Reason: {reason}"
                    ),
                    suggested_action=f"Approve correction {correction.id}",
                    project_id=tenant_id,
                    context={"correction_id": correction.id},
                )
            except Exception as exc:
                logger.warning(f"[Corrections] Check-in creation failed: {exc}")

        return correction

    def approve(self, correction_id: str, approved_by: str) -> Correction:
        """
        Approve a PROPOSED correction.

        Raises ValueError if the correction is missing, not in PROPOSED
        status, or if the four-eyes rule is violated (approver == proposer).
        """
        correction = self._get_or_raise(correction_id)
        if correction.status != STATUS_PROPOSED:
            raise ValueError(
                f"Correction {correction_id} is '{correction.status}', "
                f"only '{STATUS_PROPOSED}' corrections can be approved"
            )
        if (
            self._require_four_eyes
            and correction.created_by
            and approved_by == correction.created_by
        ):
            raise ValueError(
                f"Four-eyes rule: correction {correction_id} was proposed by "
                f"'{correction.created_by}' and cannot be approved by the same "
                "user. Pass require_four_eyes=False for single-operator "
                "deployments."
            )

        correction.status = STATUS_APPROVED
        correction.approved_by = approved_by
        correction.updated_at = datetime.now().isoformat()
        self._store.update(
            "corrections",
            correction_id,
            {
                "status": STATUS_APPROVED,
                "approved_by": approved_by,
                "updated_at": correction.updated_at,
            },
        )
        logger.info(f"[Corrections] Approved {correction_id} by '{approved_by}'")

        if self._on_approve is not None:
            try:
                self._on_approve(correction)
            except Exception as exc:
                logger.warning(f"[Corrections] on_approve callback failed: {exc}")

        return correction

    def reject(
        self, correction_id: str, rejected_by: str, reason: str = ""
    ) -> Correction:
        """Reject a PROPOSED correction. Terminal state."""
        correction = self._get_or_raise(correction_id)
        if correction.status != STATUS_PROPOSED:
            raise ValueError(
                f"Correction {correction_id} is '{correction.status}', "
                f"only '{STATUS_PROPOSED}' corrections can be rejected"
            )
        correction.status = STATUS_REJECTED
        correction.updated_at = datetime.now().isoformat()
        correction.metadata.update({"rejected_by": rejected_by, "reject_reason": reason})
        self._store.update(
            "corrections",
            correction_id,
            {
                "status": STATUS_REJECTED,
                "updated_at": correction.updated_at,
                "metadata_json": json.dumps(correction.metadata, default=str),
            },
        )
        logger.info(f"[Corrections] Rejected {correction_id} by '{rejected_by}'")
        return correction

    def retire(self, correction_id: str) -> Correction:
        """Retire an APPROVED correction (no longer injected into context)."""
        correction = self._get_or_raise(correction_id)
        if correction.status != STATUS_APPROVED:
            raise ValueError(
                f"Correction {correction_id} is '{correction.status}', "
                f"only '{STATUS_APPROVED}' corrections can be retired"
            )
        correction.status = STATUS_RETIRED
        correction.updated_at = datetime.now().isoformat()
        self._store.update(
            "corrections",
            correction_id,
            {"status": STATUS_RETIRED, "updated_at": correction.updated_at},
        )
        logger.info(f"[Corrections] Retired {correction_id}")
        return correction

    def get(self, correction_id: str) -> Correction | None:
        """Fetch a correction by id, or None."""
        rows = self._store.query("corrections", {"id": correction_id}, limit=1)
        return self._from_row(rows[0]) if rows else None

    def get_approved_for_context(
        self,
        tenant_id: str = "default",
        agent_id: str = "",
        budget_chars: int = DEFAULT_CONTEXT_BUDGET_CHARS,
    ) -> str:
        """
        Render approved corrections (most recent first) as a prompt block.

        Stops adding entries BEFORE the block would exceed budget_chars.
        Env override: CORRECTION_CONTEXT_BUDGET. Returns "" when there is
        nothing to inject. All stored text is sanitized before rendering.
        """
        env_budget = os.environ.get("CORRECTION_CONTEXT_BUDGET", "")
        if env_budget.strip().isdigit():
            budget_chars = int(env_budget.strip())

        filters: dict[str, Any] = {"tenant_id": tenant_id, "status": STATUS_APPROVED}
        if agent_id:
            filters["agent_id"] = agent_id
        rows = self._store.query("corrections", filters, order_by="created_at DESC")
        if not rows:
            return ""

        header = "## Approved corrections (these override earlier claims)\n"
        block = header
        added = 0
        for row in rows:
            original = sanitize_for_prompt(
                row.get("original_claim", ""), max_length=MAX_FIELD_RENDER_CHARS
            )
            corrected = sanitize_for_prompt(
                row.get("corrected_claim", ""), max_length=MAX_FIELD_RENDER_CHARS
            )
            reason = sanitize_for_prompt(
                row.get("reason", ""), max_length=MAX_FIELD_RENDER_CHARS
            )
            entry = f"- Instead of: {original}\n  Use: {corrected}\n"
            if reason:
                entry += f"  Why: {reason}\n"
            if len(block) + len(entry) > budget_chars:
                break
            block += entry
            added += 1

        return block.rstrip() if added else ""

    def _get_or_raise(self, correction_id: str) -> Correction:
        correction = self.get(correction_id)
        if correction is None:
            raise ValueError(f"Correction {correction_id} not found")
        return correction

    @staticmethod
    def _to_row(correction: Correction) -> dict:
        """Serialize a Correction to a corrections-table row dict."""
        return {
            "id": correction.id,
            "tenant_id": correction.tenant_id,
            "agent_id": correction.agent_id,
            "session_id": correction.session_id,
            "original_claim": correction.original_claim,
            "corrected_claim": correction.corrected_claim,
            "reason": correction.reason,
            "evidence_level": correction.evidence_level,
            "status": correction.status,
            "created_by": correction.created_by,
            "approved_by": correction.approved_by,
            "created_at": correction.created_at,
            "updated_at": correction.updated_at,
            "metadata_json": json.dumps(correction.metadata, default=str),
        }

    @staticmethod
    def _from_row(row: dict) -> Correction:
        """Deserialize a corrections-table row dict to a Correction."""
        try:
            metadata = json.loads(row.get("metadata_json") or "{}")
        except json.JSONDecodeError:
            metadata = {}
        return Correction(
            id=row["id"],
            tenant_id=row.get("tenant_id", "default"),
            agent_id=row.get("agent_id", ""),
            session_id=row.get("session_id", ""),
            original_claim=row.get("original_claim", ""),
            corrected_claim=row.get("corrected_claim", ""),
            reason=row.get("reason", ""),
            evidence_level=row.get("evidence_level", ""),
            status=row.get("status", STATUS_PROPOSED),
            created_by=row.get("created_by", ""),
            approved_by=row.get("approved_by", ""),
            metadata=metadata,
            created_at=row.get("created_at", ""),
            updated_at=row.get("updated_at", ""),
        )
