"""
Corrections -- user-approved fixes to agent claims.

When an agent states something wrong, a user (or another agent) proposes a
correction. Corrections only influence behavior after human approval, then
get rendered into a prompt-injectable block so agents stop repeating the
original claim.

Lifecycle: PROPOSED -> APPROVED -> RETIRED (REJECTED is a terminal branch).
APPROVED corrections can be REVALIDATED in place (refresh last_validated_at/by
without changing status) or INVALIDATED by approval of a successor that
sets supersedes_id (ancestor keeps status=approved but invalid_at is set
and grounding excludes it -- see learning/supersession.py).

Four-eyes rule: the approver should differ from the proposer (policy in
learning/four_eyes.py): require_four_eyes=None (the default) defers to
CORRECTIONS_FOUR_EYES ("strict" rejects self-approval; "warn", the
default, allows it but logs loudly and flags once). True/False pin the
historical strict/off semantics. Supersession uses the same posture on
the successor's proposer/approver pair.

Security: PII is redacted before persistence (security.pii); rendered
text passes through sanitize_for_prompt so stored content cannot inject
into prompts, within a character budget (CORRECTION_CONTEXT_BUDGET env
override). An optional ContentPolicy can gate propose().

Concurrency: status transitions (approve/reject/retire) are conditional
writes (learning/lifecycle.py) that only land while the row still holds
the expected prior status; a lost race raises ValueError (409 at API).

Keep this file under 560 lines. (Raised from 500: validity fields on the
dataclass/serde plus supersession hooks in approve; heavy lifting lives
in learning/supersession.py.)
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
from .four_eyes import check_four_eyes
from .lifecycle import transition
from .store import LearningStore
from .supersession import (
    finalize_supersession_approve,
    require_update_if,
    validate_ancestor_for_supersession,
)

logger = logging.getLogger(__name__)

# Correction lifecycle statuses
STATUS_PROPOSED = "proposed"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_RETIRED = "retired"

DEFAULT_CONTEXT_BUDGET_CHARS = 4000
MAX_FIELD_RENDER_CHARS = 500
# Row-fetch cap for get_approved_for_context (newest first). The char
# budget stops rendering long before this in practice; the cap bounds
# the query itself so a huge knowledge base cannot make every prompt
# build fetch every approved row. Env override:
# CORRECTION_CONTEXT_FETCH_CAP. When a fetch saturates the cap, older
# approved corrections are silently absent from grounding -- a single
# warning per process says so.
DEFAULT_CONTEXT_FETCH_CAP = 500
_fetch_cap_warned = False


def _context_fetch_cap() -> int:
    raw = os.environ.get("CORRECTION_CONTEXT_FETCH_CAP", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return DEFAULT_CONTEXT_FETCH_CAP


def _warn_fetch_cap_once(cap: int) -> None:
    global _fetch_cap_warned
    if _fetch_cap_warned:
        return
    _fetch_cap_warned = True
    logger.warning(
        f"[Corrections] get_approved_for_context fetched {cap} rows (the "
        "fetch cap): older approved corrections may be missing from prompt "
        "grounding. Raise CORRECTION_CONTEXT_FETCH_CAP or retire stale "
        "knowledge."
    )


@dataclass
class Correction:
    """
    A correction to an agent claim -- mirrors the corrections table.

    evidence_level: How well-supported the correction is. Projects define
                    their own scale (e.g., "anecdotal", "documented",
                    "verified").
    status: One of the STATUS_* constants.
    created_by / approved_by: User identifiers for the four-eyes check.
    source_surface: Which surface the write came through -- "api" for the
                    corrections API route, "library" for direct manager
                    calls ("" on pre-migration rows).
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
    last_validated_at: str = ""
    last_validated_by: str = ""
    source_surface: str = ""
    valid_at: str = ""
    invalid_at: str = ""
    supersedes_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())


class CorrectionsManager:
    """
    Manages the correction lifecycle on top of a LearningStore.

    Usage:
        mgr = CorrectionsManager(get_learning_store(),
                                 checkin_manager=CheckInManager())
        c = mgr.propose(agent_id="analyst",
                        original_claim="The API rate limit is 100/min",
                        corrected_claim="The API rate limit is 60/min",
                        reason="Confirmed in vendor docs",
                        evidence_level="documented", created_by="alice")
        mgr.approve(c.id, approved_by="bob")  # four-eyes: bob != alice
        block = mgr.get_approved_for_context(agent_id="analyst")
        # -> prompt-injectable text, capped at the character budget

    on_approve: optional callback invoked (best-effort) with the approved
    Correction -- e.g., feed an AgentTrustManager by building a
    FeedbackSignal(SignalType.MODIFY) and calling update_from_signal.

    content_policy: optional ContentPolicy. When set, propose() screens
    the correction text: "rejected" raises ValueError, "flagged" is
    recorded in metadata but still routes to the normal check-in flow.
    """

    def __init__(
        self,
        store: LearningStore,
        checkin_manager=None,
        require_four_eyes: bool | None = None,
        on_approve: Callable[[Correction], None] | None = None,
        content_policy=None,
    ):
        self._store = store
        self._checkin_manager = checkin_manager
        # None = defer to CORRECTIONS_FOUR_EYES (strict|warn, default warn);
        # True = always strict; False = self-approval silently allowed.
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
        source_surface: str = "library",
        supersedes_id: str = "",
    ) -> Correction:
        """
        Persist a PROPOSED correction and (if configured) open a check-in.

        PII in the claim/reason text is redacted before persistence
        (counts land in metadata). A content policy may reject the text
        (ValueError) or flag it (recorded, still proposed). source_surface
        records provenance: "library" (default) or "api" (the API route).
        When supersedes_id is set, the ancestor must be currently-valid
        approved knowledge in the same tenant (see propose_supersession).
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

        if supersedes_id:
            validate_ancestor_for_supersession(
                self.get(supersedes_id), tenant_id
            )

        correction = Correction(
            agent_id=agent_id,
            original_claim=original_claim,
            corrected_claim=corrected_claim,
            reason=reason,
            evidence_level=evidence_level,
            tenant_id=tenant_id,
            session_id=session_id,
            created_by=created_by,
            source_surface=source_surface,
            supersedes_id=supersedes_id,
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

    def propose_supersession(self, ancestor_id: str, **kwargs) -> Correction:
        """Propose a successor that will invalidate ancestor_id on approve."""
        tenant_id = kwargs.get("tenant_id", "default")
        validate_ancestor_for_supersession(self.get(ancestor_id), tenant_id)
        kwargs["supersedes_id"] = ancestor_id
        return self.propose(**kwargs)

    def approve(self, correction_id: str, approved_by: str) -> Correction:
        """
        Approve a PROPOSED correction.

        Raises ValueError if the correction is missing, not in PROPOSED
        status, or if the four-eyes rule is violated (approver == proposer)
        while the effective mode is strict. In warn mode (the default) a
        self-approval succeeds but is logged loudly and recorded as a
        "four_eyes_unenforceable" integrity flag (see learning/four_eyes.py).
        """
        correction = self._get_or_raise(correction_id)
        if correction.status != STATUS_PROPOSED:
            raise ValueError(
                f"Correction {correction_id} is '{correction.status}', "
                f"only '{STATUS_PROPOSED}' corrections can be approved"
            )
        check_four_eyes(
            self._store, correction_id, correction.created_by, approved_by,
            tenant_id=correction.tenant_id, require=self._require_four_eyes,
        )

        now = datetime.now().isoformat()
        if correction.supersedes_id:
            # Fail closed before transition if store lacks update_if (T17).
            require_update_if(self._store)

        valid_at = correction.valid_at or now
        correction.status = STATUS_APPROVED
        correction.approved_by = approved_by
        correction.updated_at = now
        correction.valid_at = valid_at
        transition(self._store, correction_id, {
            "status": STATUS_APPROVED,
            "approved_by": approved_by,
            "updated_at": now,
            "valid_at": valid_at,
        }, expected_status=STATUS_PROPOSED)
        logger.info(f"[Corrections] Approved {correction_id} by '{approved_by}'")

        if correction.supersedes_id:
            finalize_supersession_approve(
                self._store,
                successor_id=correction_id,
                ancestor_id=correction.supersedes_id,
                tenant_id=correction.tenant_id,
                approved_by=approved_by,
                now=now,
            )

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
        transition(self._store, correction_id, {
            "status": STATUS_REJECTED,
            "updated_at": correction.updated_at,
            "metadata_json": json.dumps(correction.metadata, default=str),
        }, expected_status=STATUS_PROPOSED)
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
        transition(self._store, correction_id,
                   {"status": STATUS_RETIRED, "updated_at": correction.updated_at},
                   expected_status=STATUS_APPROVED)
        logger.info(f"[Corrections] Retired {correction_id}")
        return correction

    def revalidate(self, correction_id: str, validated_by: str) -> Correction:
        """
        Re-validate an APPROVED correction: a human confirms it is still
        true, refreshing its staleness clock (last_validated_at/by) while
        leaving status AND updated_at untouched -- updated_at must stay a
        pure lifecycle-change timestamp or it would zero the governance
        report's fresh_only_via_revalidation metric and drag old approvals
        back into updated_at-windowed pattern checks (approval_patterns).

        Raises ValueError if the correction is missing or not APPROVED.
        """
        correction = self._get_or_raise(correction_id)
        if correction.status != STATUS_APPROVED:
            raise ValueError(
                f"Correction {correction_id} is '{correction.status}', "
                f"only '{STATUS_APPROVED}' corrections can be revalidated"
            )
        now = datetime.now().isoformat()
        correction.last_validated_at = now
        correction.last_validated_by = validated_by
        self._store.update(
            "corrections",
            correction_id,
            {
                "last_validated_at": now,
                "last_validated_by": validated_by,
            },
        )
        logger.info(
            f"[Corrections] Revalidated {correction_id} by '{validated_by}'"
        )
        return correction

    def get(self, correction_id: str) -> Correction | None:
        """Fetch a correction by id, or None."""
        rows = self._store.query("corrections", {"id": correction_id}, limit=1)
        return self._from_row(rows[0]) if rows else None

    @property
    def store(self) -> LearningStore:
        """The underlying LearningStore (used by erasure and admin APIs)."""
        return self._store

    @property
    def checkin_manager(self):
        """CheckInManager for approval check-ins (or None); erasure sweeps it."""
        return self._checkin_manager

    def list(
        self,
        tenant_id: str = "default",
        status: str = "",
        agent_id: str = "",
        limit: int = 100,
        order_by: str = "created_at DESC",
    ) -> list[Correction]:
        """List a tenant's corrections (newest first by default)."""
        filters: dict[str, Any] = {"tenant_id": tenant_id}
        if status:
            filters["status"] = status
        if agent_id:
            filters["agent_id"] = agent_id
        rows = self._store.query(
            "corrections", filters, order_by=order_by, limit=limit
        )
        return [self._from_row(row) for row in rows]

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

        filters: dict[str, Any] = {
            "tenant_id": tenant_id,
            "status": STATUS_APPROVED,
            "invalid_at": "",
        }
        if agent_id:
            filters["agent_id"] = agent_id
        fetch_cap = _context_fetch_cap()
        rows = self._store.query(
            "corrections",
            filters,
            order_by="created_at DESC",
            limit=fetch_cap,
        )
        if len(rows) >= fetch_cap:
            _warn_fetch_cap_once(fetch_cap)
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
            "last_validated_at": correction.last_validated_at,
            "last_validated_by": correction.last_validated_by,
            "source_surface": correction.source_surface,
            "valid_at": correction.valid_at,
            "invalid_at": correction.invalid_at,
            "supersedes_id": correction.supersedes_id,
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
            last_validated_at=row.get("last_validated_at") or "",
            last_validated_by=row.get("last_validated_by") or "",
            source_surface=row.get("source_surface") or "",
            valid_at=row.get("valid_at") or "",
            invalid_at=row.get("invalid_at") or "",
            supersedes_id=row.get("supersedes_id") or "",
            metadata=metadata,
            created_at=row.get("created_at", ""),
            updated_at=row.get("updated_at", ""),
        )
