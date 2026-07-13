"""
Lexical ranking primitives: BM25 (Okapi) scoring and Reciprocal Rank Fusion.

Pure functions over token lists -- no I/O, no state, no persisted index.
Used by the in-memory VectorStore fallback path to replace the naive
binary keyword-presence scorer. The Chroma adapter path is unaffected.

Security invariant (see docs/GOVERNANCE.md): callers MUST pass only the
`where`-filtered candidate set to `bm25_scores`. All corpus statistics
(document frequency, average length) are computed over exactly the token
lists given, so cross-scope documents can never influence in-scope IDF.

Tokenization is language-naive: NFKC fold + lowercase + `\\w+` word
characters. Whole-token matching means no substring or prefix recall
("deploy" does not match "deployment") -- this trade-off is measured in
tests/test_retrieval_ranking.py, not hidden. The NFKC fold here is a
RELEVANCE normalization, deliberately divergent from the security
normalization stack (`security/injection_defense.py`): it provides no
homoglyph defense -- a homoglyph-stuffed document simply loses rank
because its tokens match nothing, which is the correct retrieval
outcome, not a detection.

Prior art: `learning/contradiction.py` carries its own private
tokenizer + TF-IDF for contradiction similarity. Consolidating it onto
this module is a sanctioned future cleanup, not done here (different
tuning, different false-positive costs).

Keep this file under 150 lines.
"""

import math
import os
import re
import unicodedata
from collections import Counter

LEXICAL_RANKING_ENV = "LEXICAL_RANKING_ENABLED"


def lexical_ranking_enabled() -> bool:
    """Default ON; only an explicit falsy value disables (rollback lever).

    ACTIVITY_TRACKING_ENABLED parse precedent: garbage preserves the
    default, the safe failure mode for a default-on kill switch. Read per
    search so rollback needs no restart of long-lived stores.
    """
    return os.environ.get(LEXICAL_RANKING_ENV, "true").strip().lower() not in (
        "false", "0", "no",
    )

# Standard Okapi BM25 constants; not env-tunable by design (two more knobs
# nobody will measure). Change requires re-pinning the measured-gain test.
BM25_K1 = 1.5
BM25_B = 0.75

# Standard RRF discount constant from the original Cormack et al. paper.
RRF_K = 60

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """NFKC-fold, lowercase, and split into word-character tokens."""
    return _TOKEN_RE.findall(unicodedata.normalize("NFKC", text).lower())


def bm25_scores(
    query_tokens: list[str],
    docs_tokens: list[list[str]],
) -> list[float]:
    """Score every candidate document against the query with Okapi BM25.

    `docs_tokens` must be the tokenized `where`-filtered candidate set and
    nothing else (scoped-IDF invariant). Returns one score per document in
    input order; documents sharing no query term score 0.0.
    """
    n = len(docs_tokens)
    if n == 0 or not query_tokens:
        return [0.0] * n

    doc_lengths = [len(d) for d in docs_tokens]
    avgdl = sum(doc_lengths) / n
    doc_token_sets = [set(d) for d in docs_tokens]

    # unique query terms, first-occurrence order (repetition in the query
    # itself must not multiply a term's contribution)
    terms = list(dict.fromkeys(query_tokens))
    idf = {}
    for term in terms:
        df = sum(1 for s in doc_token_sets if term in s)
        idf[term] = math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    scores = []
    for tokens, length in zip(docs_tokens, doc_lengths):
        counts = Counter(tokens)
        score = 0.0
        for term in terms:
            tf = counts.get(term, 0)
            if tf == 0:
                continue
            if avgdl > 0:
                norm = 1.0 - BM25_B + BM25_B * length / avgdl
            else:
                norm = 1.0
            score += idf[term] * tf * (BM25_K1 + 1.0) / (tf + BM25_K1 * norm)
        scores.append(score)
    return scores


def rrf_fuse(*rankings: list[str], k: int = RRF_K) -> dict[str, float]:
    """Fuse ranked id lists with Reciprocal Rank Fusion.

    Each list contributes 1 / (k + rank) per id (rank is 1-based). Only ids
    present in at least one input list appear in the output -- membership
    eligibility (which ids are allowed into a ranking at all) is the
    caller's responsibility.
    """
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + rank)
    return fused
