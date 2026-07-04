"""
Contradiction detector -- finds approved corrections that disagree.

Two approved corrections about the same topic that point in opposite
directions ("use X" vs "stop using X") silently poison agent context:
whichever renders last wins. This module scans a tenant's approved
corrections pairwise and flags likely contradictions to integrity_flags
for human review -- it never auto-retires anything.

Heuristic, deterministic, $0 ongoing cost:
  1. Group corrections by agent_id (contradictions across different
     agents' guidance are usually fine).
  2. Within a group, compute TF-IDF cosine similarity over the correction
     text (pure-Python bag-of-words -- the scaffold has no numpy and
     keeps it that way).
  3. A pair is flagged only when it is BOTH similar (same topic) AND at
     least one side contains a negation/reversal marker.

Keep this file under 250 lines.
"""

import json
import logging
import math
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from .corrections import STATUS_APPROVED
from .store import LearningStore

logger = logging.getLogger(__name__)

FLAG_TYPE_CONTRADICTION = "correction_contradiction"
SIMILARITY_THRESHOLD = 0.5
GROUP_CAP = 50

_TOKENIZE_RE = re.compile(r"[a-z0-9]+")

NEGATION_PATTERNS: tuple[str, ...] = (
    "don't",
    "do not",
    "never",
    "avoid",
    "instead of",
    "no longer",
    "removed",
    "deprecated",
    "replaced by",
    "not recommended",
    "should not",
    "must not",
    "disable",
    "stop using",
    "switched from",
)


@dataclass
class ContradictionFinding:
    """A flagged pair of likely-contradicting corrections."""

    correction_id_a: str
    correction_id_b: str
    agent_id: str
    similarity: float
    negation_marker: str


def _tokenize(text: str) -> list[str]:
    return _TOKENIZE_RE.findall(text.lower())


def _tfidf_vectors(texts: list[str]) -> list[dict[str, float]]:
    """L2-normalized TF-IDF vectors as sparse {token: weight} dicts."""
    tokenized = [_tokenize(t) for t in texts]
    n_docs = len(texts)

    df: Counter = Counter()
    for tokens in tokenized:
        df.update(set(tokens))

    vectors: list[dict[str, float]] = []
    for tokens in tokenized:
        if not tokens:
            vectors.append({})
            continue
        counts = Counter(tokens)
        vec = {
            tok: (count / len(tokens)) * (math.log((n_docs + 1) / (df[tok] + 1)) + 1)
            for tok, count in counts.items()
        }
        norm = math.sqrt(sum(w * w for w in vec.values())) or 1.0
        vectors.append({tok: w / norm for tok, w in vec.items()})
    return vectors


def _cosine(vec_a: dict[str, float], vec_b: dict[str, float]) -> float:
    if len(vec_b) < len(vec_a):
        vec_a, vec_b = vec_b, vec_a
    return sum(w * vec_b.get(tok, 0.0) for tok, w in vec_a.items())


def _negation_marker(text_a: str, text_b: str) -> str:
    """First negation/reversal marker found in either text, or ""."""
    combined = f"{text_a} {text_b}".lower()
    for pattern in NEGATION_PATTERNS:
        if pattern in combined:
            return pattern
    return ""


def _correction_text(row: dict) -> str:
    return " ".join(
        str(row.get(col) or "")
        for col in ("original_claim", "corrected_claim", "reason")
    )


def scan_corrections(
    store: LearningStore,
    tenant_id: str = "default",
    similarity_threshold: float = SIMILARITY_THRESHOLD,
    group_cap: int = GROUP_CAP,
) -> list[ContradictionFinding]:
    """Scan a tenant's approved corrections for likely contradictions.

    Findings are returned AND written to integrity_flags (best-effort,
    deduplicated against unresolved flags for the same pair). Groups
    larger than group_cap are truncated to the most recent entries to
    bound the O(n^2) pair comparison.
    """
    rows = store.query(
        "corrections",
        {"tenant_id": tenant_id, "status": STATUS_APPROVED},
        order_by="created_at DESC",
    )

    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row.get("agent_id") or "", []).append(row)

    findings: list[ContradictionFinding] = []
    for agent_id, members in groups.items():
        if len(members) < 2:
            continue
        members = members[:group_cap]
        texts = [_correction_text(m) for m in members]
        vectors = _tfidf_vectors(texts)

        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                similarity = _cosine(vectors[i], vectors[j])
                if similarity < similarity_threshold:
                    continue
                marker = _negation_marker(texts[i], texts[j])
                if not marker:
                    continue
                finding = ContradictionFinding(
                    correction_id_a=members[i].get("id", ""),
                    correction_id_b=members[j].get("id", ""),
                    agent_id=agent_id,
                    similarity=round(similarity, 4),
                    negation_marker=marker,
                )
                findings.append(finding)
                _flag(store, finding, tenant_id)

    if findings:
        logger.warning(
            "[Contradiction] %d likely contradiction(s) among tenant '%s' corrections",
            len(findings), tenant_id,
        )
    return findings


def _pair_key(finding: ContradictionFinding) -> str:
    return ":".join(sorted([finding.correction_id_a, finding.correction_id_b]))


def _flag(store: LearningStore, finding: ContradictionFinding, tenant_id: str) -> None:
    """Persist a finding as an integrity flag (best-effort, deduplicated)."""
    try:
        existing = store.query(
            "integrity_flags",
            {
                "flag_type": FLAG_TYPE_CONTRADICTION,
                "subject_id": _pair_key(finding),
                "tenant_id": tenant_id,
                "resolved": 0,
            },
            limit=1,
        )
        if existing:
            return
        store.insert(
            "integrity_flags",
            {
                "id": str(uuid.uuid4())[:12],
                "flag_type": FLAG_TYPE_CONTRADICTION,
                "subject_id": _pair_key(finding),
                "tenant_id": tenant_id,
                "severity": "warning",
                "detail_json": json.dumps(
                    {
                        "correction_id_a": finding.correction_id_a,
                        "correction_id_b": finding.correction_id_b,
                        "agent_id": finding.agent_id,
                        "similarity": finding.similarity,
                        "negation_marker": finding.negation_marker,
                    }
                ),
                "created_at": datetime.now().isoformat(),
                "resolved": 0,
            },
        )
    except Exception as exc:
        logger.warning("[Contradiction] Failed to persist flag: %s", exc)
