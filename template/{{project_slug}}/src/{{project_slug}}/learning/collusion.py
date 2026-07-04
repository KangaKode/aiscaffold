"""
CollusionDetector -- statistical checks for agents that stop checking
each other.

A round table only works while agents genuinely disagree when the
evidence warrants it. Three failure modes this module watches for:

  1. Vote lockstep -- a pair of agents that (almost) always votes the
     same way across many rounds (analyze_votes).
  2. Challenge softness -- a pair that sits in many rounds together but
     never challenges each other in either direction
     (analyze_challenge_softness).
  3. Correction drift / slow poisoning -- the stream of approved
     corrections gradually tilts toward "softening" language ("benign",
     "false positive", "reduce severity"), which can indicate someone is
     steadily lowering the system's guard (analyze_correction_drift).

Design choice: raw votes are NOT persisted here. Vote history lives with
the orchestrator (round-table artifacts) and is passed into the analysis
functions; only FINDINGS are persisted, as integrity_flags rows. This
keeps the store schema small and avoids duplicating orchestration data.

Findings never auto-suspend anyone. flag() opens a check-in whose
suggested action names registry.suspend() as the human-approved remedy.

All detectors are heuristic v1 -- thresholds are conservative and the
pattern lists are extension points.

Keep this file under 300 lines.
"""

import json
import logging
import re
import uuid
from datetime import datetime
from itertools import combinations

logger = logging.getLogger(__name__)

FLAG_TYPE_COLLUSION = "collusion_suspected"
FLAG_TYPE_DRIFT = "correction_drift"

# "Softening" language in approved corrections -- phrases that reduce
# vigilance. Heuristic v1: extend per deployment.
SOFTENING_PATTERNS = [
    r"not\s+suspicious",
    r"\bbenign\b",
    r"reduce\s+(the\s+)?severity",
    r"lower\s+(the\s+)?severity",
    r"false\s+positive",
    r"no\s+need\s+to\s+(check|verify|review|escalate)",
    r"safe\s+to\s+(ignore|skip)",
    r"nothing\s+to\s+worry",
]

DRIFT_SHARE_DELTA = 0.25  # recent share must exceed baseline share by this
DRIFT_MIN_WINDOW = 10  # below this many recent samples, no verdict


def _pair_key(a: str, b: str) -> tuple[str, str]:
    """Canonical (sorted) key for an unordered agent pair."""
    return (a, b) if a <= b else (b, a)


class CollusionDetector:
    """
    Analyzes voting/challenge behavior for suspicious agreement.

    Usage:
        detector = CollusionDetector(store, checkin_manager=CheckInManager())
        detector.record_votes(task_id, result.votes)   # per round table run
        # ... or run the analyses directly over collected history:
        findings = detector.analyze_votes(vote_history)
        detector.flag(findings)
    """

    # Cap on in-memory vote history (rounds). Oldest rounds are dropped.
    MAX_HISTORY_ROUNDS = 500

    def __init__(self, store, checkin_manager=None):
        self._store = store
        self._checkin_manager = checkin_manager
        # In-memory vote history: list of rounds, each a list of
        # (agent_name, approve) tuples. Persisting raw votes is the
        # orchestrator's job (artifacts); we only keep a working window.
        self._vote_history: list[list[tuple[str, bool]]] = []
        # Pairs already flagged this process lifetime (avoid re-flagging
        # the same pair on every subsequent round).
        self._flagged_pairs: set[tuple[str, str]] = set()

    # ------------------------------------------------------------------
    # Recording (in-memory accumulation + incremental analysis)
    # ------------------------------------------------------------------

    def record_votes(self, task_id: str, votes, tenant_id: str = "default") -> list[dict]:
        """
        Record one round of votes and incrementally analyze the history.

        votes: list of AgentVote-like objects (agent_name/approve attrs)
        or (agent_name, approve) tuples. New findings (pairs not yet
        flagged by this instance) are persisted via flag() and returned.
        """
        round_votes: list[tuple[str, bool]] = []
        for vote in votes:
            if isinstance(vote, tuple):
                name, approve = vote[0], bool(vote[1])
            else:
                name = getattr(vote, "agent_name", "")
                approve = bool(getattr(vote, "approve", False))
            if name:
                round_votes.append((name, approve))
        if len(round_votes) < 2:
            return []

        self._vote_history.append(round_votes)
        if len(self._vote_history) > self.MAX_HISTORY_ROUNDS:
            self._vote_history = self._vote_history[-self.MAX_HISTORY_ROUNDS:]

        findings = self.analyze_votes(self._vote_history)
        new = [
            f for f in findings
            if _pair_key(*f["pair"]) not in self._flagged_pairs
        ]
        if new:
            for finding in new:
                finding["task_id"] = task_id
                self._flagged_pairs.add(_pair_key(*finding["pair"]))
            self.flag(new, tenant_id=tenant_id)
        return new

    # ------------------------------------------------------------------
    # Pure analyses (no persistence)
    # ------------------------------------------------------------------

    @staticmethod
    def analyze_votes(
        vote_history: list[list[tuple[str, bool]]],
        threshold: float = 0.9,
        min_rounds: int = 5,
    ) -> list[dict]:
        """
        Pairwise vote-agreement analysis.

        vote_history: one entry per round; each entry is a list of
        (agent_name, approve) tuples. For every agent pair, agreement
        rate is computed over the rounds BOTH participated in. Pairs with
        >= min_rounds shared rounds and agreement rate >= threshold are
        returned as findings:
            {"pair": [a, b], "agreement_rate": float, "rounds": int}
        """
        shared: dict[tuple[str, str], int] = {}
        agreed: dict[tuple[str, str], int] = {}
        for round_votes in vote_history:
            by_agent = dict(round_votes)
            for a, b in combinations(sorted(by_agent), 2):
                key = (a, b)
                shared[key] = shared.get(key, 0) + 1
                if by_agent[a] == by_agent[b]:
                    agreed[key] = agreed.get(key, 0) + 1

        findings = []
        for key, rounds in shared.items():
            if rounds < min_rounds:
                continue
            rate = agreed.get(key, 0) / rounds
            if rate >= threshold:
                findings.append(
                    {
                        "kind": "vote_lockstep",
                        "pair": list(key),
                        "agreement_rate": round(rate, 4),
                        "rounds": rounds,
                    }
                )
        return findings

    @staticmethod
    def analyze_challenge_softness(
        challenge_counts: dict[tuple[str, str], int],
        rounds_together: dict[tuple[str, str], int],
        min_rounds: int = 5,
    ) -> list[dict]:
        """
        Find reciprocal non-challenging pairs.

        challenge_counts: {(challenger, target): count} (ordered keys).
        rounds_together: {pair: count} of rounds both agents attended
        (unordered pairs; either key order is accepted).

        A pair is flagged when they shared >= min_rounds rounds and
        NEITHER ever challenged the other:
            {"pair": [a, b], "rounds": int, "challenges": 0}
        """
        # Normalize to canonical sorted keys (accept either key order;
        # if both orders are present, keep the larger count).
        normalized: dict[tuple[str, str], int] = {}
        for (a, b), rounds in rounds_together.items():
            key = _pair_key(a, b)
            normalized[key] = max(normalized.get(key, 0), rounds)

        findings = []
        for (a, b), rounds in normalized.items():
            if rounds < min_rounds:
                continue
            forward = challenge_counts.get((a, b), 0)
            backward = challenge_counts.get((b, a), 0)
            if forward == 0 and backward == 0:
                findings.append(
                    {
                        "kind": "challenge_softness",
                        "pair": [a, b],
                        "rounds": rounds,
                        "challenges": 0,
                    }
                )
        return findings

    # ------------------------------------------------------------------
    # Persistence of findings
    # ------------------------------------------------------------------

    def flag(self, findings: list[dict], tenant_id: str = "default") -> None:
        """
        Persist findings as integrity_flags (flag_type="collusion_suspected",
        severity="warning") and, when a checkin_manager is configured, open
        a check-in per finding. Suspension is NEVER automatic -- the
        suggested action points the human at registry.suspend().
        """
        for finding in findings:
            pair = finding.get("pair", [])
            try:
                self._store.insert(
                    "integrity_flags",
                    {
                        "id": str(uuid.uuid4())[:12],
                        "flag_type": FLAG_TYPE_COLLUSION,
                        "subject_id": ",".join(pair),
                        "tenant_id": tenant_id,
                        "severity": "warning",
                        "detail_json": json.dumps(finding, default=str),
                        "created_at": datetime.now().isoformat(),
                        "resolved": 0,
                    },
                )
            except Exception as exc:
                logger.error(f"[Collusion] Failed to persist flag: {exc}")
                continue

            logger.warning(f"[Collusion] Flagged pair {pair}: {finding}")

            if self._checkin_manager is None:
                continue
            try:
                self._checkin_manager.create(
                    checkin_type="collusion_review",
                    prompt=(
                        f"Agents {pair} show suspicious agreement "
                        f"({finding.get('kind', 'collusion')}): {finding}. "
                        "Review their recent round-table activity."
                    ),
                    suggested_action=(
                        f"If collusion is confirmed, suspend one agent via "
                        f"registry.suspend({pair[0]!r}) (or the "
                        f"POST /api/v1/agents/{{id}}/suspend endpoint) "
                        "pending investigation."
                    ),
                    project_id=tenant_id,
                    context=finding,
                )
            except Exception as exc:
                logger.warning(f"[Collusion] Check-in creation failed: {exc}")


def analyze_correction_drift(
    store,
    tenant_id: str = "default",
    window: int = 20,
    baseline: int = 100,
) -> dict | None:
    """
    Slow-poisoning heuristic (v1) over approved corrections.

    Compares the share of the most recent `window` approved corrections
    whose reason/corrected_claim matches a "softening" pattern against
    the share in the prior `baseline` corrections. When the recent share
    exceeds the baseline share by more than 0.25 (and at least
    DRIFT_MIN_WINDOW recent samples exist), returns a finding dict and
    persists an integrity flag (flag_type="correction_drift").

    With no prior history the baseline share is 0.0 -- i.e. a brand-new
    deployment whose first corrections are mostly softening WILL flag,
    which is the conservative choice. Extend SOFTENING_PATTERNS and the
    deltas per deployment; this is deliberately simple and extensible.

    Returns None when there is no drift (or not enough data).
    """
    rows = store.query(
        "corrections",
        {"tenant_id": tenant_id, "status": "approved"},
        order_by="created_at DESC",
        limit=window + baseline,
    )
    recent, prior = rows[:window], rows[window:]
    if len(recent) < DRIFT_MIN_WINDOW:
        return None

    def _softening_share(batch: list[dict]) -> float:
        if not batch:
            return 0.0
        hits = 0
        for row in batch:
            text = f"{row.get('corrected_claim', '')} {row.get('reason', '')}".lower()
            if any(re.search(p, text) for p in SOFTENING_PATTERNS):
                hits += 1
        return hits / len(batch)

    recent_share = _softening_share(recent)
    baseline_share = _softening_share(prior)
    if recent_share - baseline_share <= DRIFT_SHARE_DELTA:
        return None

    finding = {
        "kind": "correction_drift",
        "recent_share": round(recent_share, 4),
        "baseline_share": round(baseline_share, 4),
        "window": len(recent),
        "baseline_samples": len(prior),
    }
    logger.warning(f"[Collusion] Correction drift detected: {finding}")
    try:
        store.insert(
            "integrity_flags",
            {
                "id": str(uuid.uuid4())[:12],
                "flag_type": FLAG_TYPE_DRIFT,
                "subject_id": "corrections",
                "tenant_id": tenant_id,
                "severity": "warning",
                "detail_json": json.dumps(finding, default=str),
                "created_at": datetime.now().isoformat(),
                "resolved": 0,
            },
        )
    except Exception as exc:
        logger.error(f"[Collusion] Failed to persist drift flag: {exc}")
    return finding
