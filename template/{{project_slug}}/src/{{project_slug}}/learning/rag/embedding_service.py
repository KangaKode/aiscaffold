"""
EmbeddingService -- Multi-provider text embeddings with caching.

Default provider:
  1. Deterministic fallback -- hash-based, works without any dependencies

Opt-in providers:
  - Set `EMBEDDING_PROVIDER=local` for sentence-transformers
  - Set `EMBEDDING_PROVIDER=openai` for text-embedding-3-small
  - Set `EMBEDDING_PROVIDER=auto` to try OpenAI, then local, then fallback

(The legacy `ROUNDTABLE_EMBEDDING_PROVIDER` name is honored for one release
with a DeprecationWarning.)

HONEST LIMITATION: the hash fallback is deterministic but NOT semantic --
two texts about the same topic get unrelated vectors, so cosine
similarity over fallback vectors is meaningless noise (measured: it can
rank an irrelevant document above a keyword-matching one). Retrieval
callers check `is_semantic` and skip embedding-based ranking when it is
False AND the vector store can rank lexically instead: with the default
in-memory VectorStore, PreferenceRetriever and TranscriptIndexer rank
with BM25 (see learning/rag/lexical.py); with a real provider the store
fuses BM25 + cosine via RRF. The Chroma backend has no lexical path,
so there the hash vectors are still stored and queried (dimension-
consistent with existing indexes, ranking stays hash noise) -- Chroma
deployments should set a real provider.

Caching: embeddings are cached in-memory (LRU) to avoid recomputing.
All providers produce normalized vectors suitable for cosine similarity.

Keep this file under 250 lines.
"""

import hashlib
import logging
import os
import warnings
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

MAX_CACHE_SIZE = 5000
MAX_TEXT_LENGTH = 8000


@dataclass
class EmbeddingResult:
    """Result from embedding generation."""

    embedding: list[float]
    dimensions: int
    provider: str
    cached: bool = False


class EmbeddingService:
    """
    Multi-provider embedding service with automatic fallback.

    Usage:
        service = EmbeddingService()
        result = service.embed("User prefers concise responses")
        vector = result.embedding  # list[float]

        # Batch
        results = service.embed_batch(["text1", "text2", "text3"])
    """

    def __init__(self, preferred_provider: str | None = None):
        self._provider: str = "fallback"
        self._model: Any = None
        self._openai_client: Any = None
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._dimensions: int = 0

        self._init_provider(preferred_provider)

    def _init_provider(self, preferred: str | None) -> None:
        """Initialize the best available provider."""
        if not preferred:
            preferred = os.environ.get("EMBEDDING_PROVIDER")
        if not preferred:
            legacy = os.environ.get("ROUNDTABLE_EMBEDDING_PROVIDER")
            if legacy is not None:
                warnings.warn(
                    "ROUNDTABLE_EMBEDDING_PROVIDER is deprecated and will be "
                    "ignored in a future release; set EMBEDDING_PROVIDER instead.",
                    DeprecationWarning,
                    stacklevel=2,
                )
                preferred = legacy
        preferred = preferred or "fallback"

        if preferred in {"openai", "auto"}:
            if self._try_openai():
                return

        if preferred in {"local", "auto"}:
            if self._try_local():
                return

        self._provider = "fallback"
        self._dimensions = 128
        logger.info(
            "[Embeddings] Using deterministic fallback (hash-based). "
            "Install sentence-transformers or set OPENAI_API_KEY for real embeddings."
        )

    def _try_local(self) -> bool:
        """Try to initialize local sentence-transformers."""
        try:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer("all-MiniLM-L6-v2")
            self._provider = "local"
            self._dimensions = 384
            logger.info("[Embeddings] Using local sentence-transformers (384d)")
            return True
        except ImportError:
            return False

    def _try_openai(self) -> bool:
        """Try to initialize OpenAI embeddings."""
        try:
            import openai

            api_key = os.environ.get("OPENAI_API_KEY", "")
            if not api_key:
                return False
            self._openai_client = openai.OpenAI(api_key=api_key)
            self._provider = "openai"
            self._dimensions = 1536
            logger.info("[Embeddings] Using OpenAI text-embedding-3-small (1536d)")
            return True
        except ImportError:
            return False

    def embed(self, text: str) -> EmbeddingResult:
        """Generate an embedding for a single text."""
        text = text[:MAX_TEXT_LENGTH].strip()
        if not text:
            return EmbeddingResult(
                embedding=[0.0] * self._dimensions,
                dimensions=self._dimensions,
                provider=self._provider,
            )

        cache_key = self._cache_key(text)
        if cache_key in self._cache:
            self._cache.move_to_end(cache_key)
            return EmbeddingResult(
                embedding=self._cache[cache_key],
                dimensions=self._dimensions,
                provider=self._provider,
                cached=True,
            )

        if self._provider == "local":
            vector = self._embed_local(text)
        elif self._provider == "openai":
            vector = self._embed_openai(text)
        else:
            vector = self._embed_fallback(text)

        self._cache_put(cache_key, vector)

        return EmbeddingResult(
            embedding=vector,
            dimensions=self._dimensions,
            provider=self._provider,
        )

    def embed_batch(self, texts: list[str]) -> list[EmbeddingResult]:
        """Generate embeddings for multiple texts."""
        return [self.embed(t) for t in texts]

    def _embed_local(self, text: str) -> list[float]:
        """Generate embedding using local sentence-transformers."""
        try:
            vector = self._model.encode(text, normalize_embeddings=True)
            return vector.tolist()
        except Exception as e:
            logger.warning(f"[Embeddings] Local embedding failed: {e}")
            return self._embed_fallback(text)

    def _embed_openai(self, text: str) -> list[float]:
        """Generate embedding using OpenAI API."""
        try:
            response = self._openai_client.embeddings.create(
                input=text,
                model="text-embedding-3-small",
            )
            return response.data[0].embedding
        except Exception as e:
            logger.warning(f"[Embeddings] OpenAI embedding failed: {e}")
            return self._embed_fallback(text)

    def _embed_fallback(self, text: str) -> list[float]:
        """Deterministic hash-based embedding (always available)."""
        h = hashlib.sha256(text.encode()).hexdigest()
        vector = []
        for i in range(0, min(len(h), self._dimensions * 2), 2):
            val = int(h[i : i + 2], 16) / 255.0 - 0.5
            vector.append(val)
        while len(vector) < self._dimensions:
            ext = hashlib.sha256(f"{text}_{len(vector)}".encode()).hexdigest()
            for i in range(0, min(len(ext), (self._dimensions - len(vector)) * 2), 2):
                vector.append(int(ext[i : i + 2], 16) / 255.0 - 0.5)

        norm = sum(v * v for v in vector) ** 0.5
        if norm > 0:
            vector = [v / norm for v in vector[: self._dimensions]]
        return vector

    def _cache_key(self, text: str) -> str:
        """Generate cache key from text content (not used for security)."""
        return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()

    def _cache_put(self, key: str, vector: list[float]) -> None:
        """Store in LRU cache with eviction."""
        self._cache[key] = vector
        while len(self._cache) > MAX_CACHE_SIZE:
            self._cache.popitem(last=False)

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def is_semantic(self) -> bool:
        """True when vectors carry meaning (cosine similarity is valid).

        The hash fallback is deterministic filler for pipelines that
        require a vector -- similarity over it is noise, so retrieval
        paths prefer lexical (BM25) ranking when this is False and the
        store supports it (VectorStore.supports_keyword_search).
        """
        return self._provider != "fallback"

    @property
    def dimensions(self) -> int:
        return self._dimensions
