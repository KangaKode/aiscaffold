"""
LLM client factory -- provider auto-detection, including the offline mock.

Kept separate from client.py so the transport code stays under its size
cap. Import via the package: `from <project>.llm import create_client`.

Keep this file under 100 lines.
"""

import logging
import os

from .client import LLMClient

logger = logging.getLogger(__name__)


def create_client(
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    **kwargs,
) -> LLMClient:
    """
    Create an LLM client, auto-detecting provider from environment if not specified.

    Detection order:
      1. Explicit provider argument
      2. LLM_PROVIDER env var ("mock" returns the offline MockLLMClient --
         deterministic, zero-cost; used by demos and the load harness)
      3. ANTHROPIC_API_KEY set -> anthropic
      4. OPENAI_API_KEY set -> openai
      5. GOOGLE_API_KEY set -> google
      6. Default: anthropic

    The mock client is duck-typed against LLMClient (call / total_usage /
    provider / model), so callers need no branching.
    """
    if provider is None:
        provider = os.environ.get("LLM_PROVIDER", "").strip().lower() or None

    if provider == "mock":
        from .mock import MockLLMClient

        logger.info("[LLM] LLM_PROVIDER=mock -- using offline MockLLMClient")
        return MockLLMClient()

    if provider is None:
        if os.environ.get("ANTHROPIC_API_KEY"):
            provider = "anthropic"
        elif os.environ.get("OPENAI_API_KEY"):
            provider = "openai"
        elif os.environ.get("GOOGLE_API_KEY"):
            provider = "google"
        else:
            provider = "anthropic"
            logger.warning("[LLM] No API key found. Defaulting to anthropic.")

    return LLMClient(provider=provider, model=model, api_key=api_key, **kwargs)
