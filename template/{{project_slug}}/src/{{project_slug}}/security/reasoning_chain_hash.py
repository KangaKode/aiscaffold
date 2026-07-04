"""
Reasoning chain hash -- tamper-evidence for a single deliberation run.

Computes an incremental SHA-256 over each phase's artifacts as a round
table runs, in order. The final digest is stored on the deliberation's
audit event. Recomputing the hash from the recorded artifacts and
comparing it to the stored digest tells you whether the phase record was
altered after the fact -- within one run.

Scope, honestly: this makes tampering *evident*, not *impossible*. The
digest lives in the same store as the artifacts, so anyone who can edit
the artifacts can also edit the digest. It defends against accidental
corruption and casual after-the-fact edits; for non-repudiation you still
need append-only storage and an external timestamp authority (see
GOVERNANCE.md).

Zero dependencies -- stdlib only.
"""

import hashlib
import json
from dataclasses import dataclass

HASH_VERSION = 1


@dataclass(frozen=True)
class ChainHashResult:
    """Outcome of finalizing a reasoning chain hash.

    hex_digest is None when the hash is degraded (a phase could not be
    serialized) or empty (no phases fed). hash_status is one of
    "complete" | "degraded" | "empty".
    """

    hex_digest: str | None
    hash_version: int
    phases_hashed: int
    degraded: bool
    hash_status: str


class ReasoningChainHasher:
    """Incremental SHA-256 over deliberation phase artifacts.

    Create one at the start of a run, feed each phase via add_phase() in
    order, then call finalize() once. Not thread-safe; single-use per run.
    """

    def __init__(self) -> None:
        self._hasher = hashlib.sha256()
        self._phases_hashed = 0
        self._degraded = False
        self._finalized = False
        self._result: ChainHashResult | None = None

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def phases_hashed(self) -> int:
        return self._phases_hashed

    def add_phase(self, phase_name: str, data: object) -> None:
        """Feed one phase's artifact into the running hash.

        data must be JSON-serializable (dataclasses should be converted
        with asdict() first). A serialization failure marks the whole
        chain degraded rather than raising -- a run must never crash
        because its integrity hash could not be computed.
        """
        if self._finalized:
            raise RuntimeError("Cannot add phases after finalize()")
        try:
            canonical = json.dumps(
                data, sort_keys=True, separators=(",", ":"), default=str
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError):
            self._degraded = True
            return
        self._hasher.update(phase_name.encode("utf-8") + b"\n" + canonical)
        self._phases_hashed += 1

    def mark_degraded(self) -> None:
        """Force the chain into a degraded state (e.g. redaction failed)."""
        self._degraded = True

    def finalize(self) -> ChainHashResult:
        """Produce the final result. Idempotent."""
        if self._result is not None:
            return self._result
        self._finalized = True
        if self._degraded or self._phases_hashed == 0:
            self._result = ChainHashResult(
                hex_digest=None,
                hash_version=HASH_VERSION,
                phases_hashed=self._phases_hashed,
                degraded=self._degraded,
                hash_status="degraded" if self._degraded else "empty",
            )
        else:
            self._result = ChainHashResult(
                hex_digest=self._hasher.hexdigest(),
                hash_version=HASH_VERSION,
                phases_hashed=self._phases_hashed,
                degraded=False,
                hash_status="complete",
            )
        return self._result


def compute_chain_hash(phases: list[tuple[str, object]]) -> ChainHashResult:
    """Convenience: hash an ordered list of (phase_name, data) pairs.

    Used both when recording a run and when verifying one later -- feed
    the same phases in the same order and compare hex_digest.
    """
    hasher = ReasoningChainHasher()
    for phase_name, data in phases:
        hasher.add_phase(phase_name, data)
    return hasher.finalize()
