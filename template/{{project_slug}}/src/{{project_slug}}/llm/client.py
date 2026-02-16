"""
Provider-agnostic LLM client with prompt caching and token tracking.

Supports Anthropic (Claude), OpenAI (GPT), Google (Gemini).
Uses CacheablePrompt(system, context, user_message) for automatic caching.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from ..security.prompt_guard import sanitize_for_prompt

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120
DEFAULT_MAX_RETRIES = 2
DEFAULT_MAX_PROMPT_LENGTH = 200_000
RETRY_BASE_DELAY = 1.0
RETRY_MAX_DELAY = 30.0

# Cost per 1K tokens (approximate, varies by model -- override via config)
COST_RATES = {
    "anthropic": {"input": 0.003, "cached": 0.0003, "output": 0.015},
    "openai": {"input": 0.005, "cached": 0.0025, "output": 0.015},
    "google": {"input": 0.0, "cached": 0.0, "output": 0.0},
}


@dataclass
class CacheablePrompt:
    """
    Separates prompt into cacheable (stable) and dynamic parts.

    The LLM client marks stable parts for provider-level caching:
      - system: System instructions (cached -- never changes)
      - context: Agent descriptions, user preferences (cached -- changes per session)
      - user_message: The actual request (never cached -- changes every call)

    This structure enables 85-90% token savings on the stable prefix.
    """

    system: str = ""
    context: str = ""
    user_message: str = ""

    def to_flat_prompt(self) -> str:
        """Flatten to a single string (for providers that don't support caching)."""
        parts = []
        if self.system:
            parts.append(self.system)
        if self.context:
            parts.append(self.context)
        if self.user_message:
            parts.append(self.user_message)
        return "\n\n".join(parts)

    @property
    def total_length(self) -> int:
        return len(self.system) + len(self.context) + len(self.user_message)


@dataclass
class TokenUsage:
    """Token usage tracking for a single LLM call."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    cache_hit: bool = False

    def __post_init__(self):
        self.total_tokens = self.input_tokens + self.output_tokens


@dataclass
class LLMResponse:
    """Response from an LLM call -- drop-in compatible with existing code."""

    content: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    model: str = ""
    provider: str = ""
    latency_ms: float = 0.0
    cached: bool = False


# =============================================================================
# LLM CLIENT
# =============================================================================


class LLMClient:
    """
    Provider-agnostic LLM client with prompt caching and token tracking.

    Usage:
        client = LLMClient(provider="anthropic")
        response = await client.call(prompt="Analyze this", role="analyst")

    Or with caching:
        prompt = CacheablePrompt(
            system="You are an expert analyst...",
            context="Available tools: ...",
            user_message="Analyze: ...",
        )
        response = await client.call(prompt=prompt, role="analyst", temperature=0.3)
    """

    def __init__(
        self,
        provider: str = "anthropic",
        model: str | None = None,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_prompt_length: int = DEFAULT_MAX_PROMPT_LENGTH,
        max_cost_usd: float | None = None,
    ):
        self._provider = provider.lower()
        self._model = model or self._default_model()
        self._api_key = api_key or self._load_api_key()
        self._timeout = timeout
        self._max_retries = max_retries
        self._max_prompt_length = max_prompt_length
        self._max_cost_usd = max_cost_usd
        self._client: Any = None
        self._total_usage = TokenUsage()

        self._init_client()
        logger.info(
            f"[LLM] Initialized {self._provider} client "
            f"(model={self._model}, timeout={self._timeout}s)"
        )

    def _default_model(self) -> str:
        defaults = {
            "anthropic": "claude-sonnet-4-20250514",
            "openai": "gpt-4o",
            "google": "gemini-2.0-flash",
        }
        return defaults.get(self._provider, "claude-sonnet-4-20250514")

    def _load_api_key(self) -> str:
        key_map = {
            "anthropic": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
            "google": "GOOGLE_API_KEY",
        }
        env_var = key_map.get(self._provider, "ANTHROPIC_API_KEY")
        key = os.environ.get(env_var, "")
        if not key:
            logger.warning(f"[LLM] {env_var} not set -- calls will fail")
        return key

    def _init_client(self) -> None:
        """Initialize the provider-specific SDK client."""
        try:
            if self._provider == "anthropic":
                import anthropic

                self._client = anthropic.AsyncAnthropic(
                    api_key=self._api_key, timeout=self._timeout
                )
            elif self._provider == "openai":
                import openai

                self._client = openai.AsyncOpenAI(
                    api_key=self._api_key, timeout=self._timeout
                )
            elif self._provider == "google":
                import google.generativeai as genai

                genai.configure(api_key=self._api_key)
                self._client = genai.GenerativeModel(self._model)
            else:
                raise ValueError(f"Unsupported provider: {self._provider}")
        except ImportError:
            logger.error(
                f"[LLM] {self._provider} SDK not installed. "
                f"Add it to requirements.txt."
            )
            self._client = None

    async def call(
        self,
        prompt: str | CacheablePrompt,
        role: str = "assistant",
        temperature: float = 0.5,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """Make an LLM call with prompt caching, retries, and budget enforcement."""
        if isinstance(prompt, str):
            prompt = CacheablePrompt(user_message=prompt)

        prompt = self._sanitize_prompt(prompt)

        if self._max_cost_usd and self._total_usage.estimated_cost_usd >= self._max_cost_usd:
            logger.error(
                f"[LLM] Budget exhausted: ${self._total_usage.estimated_cost_usd:.4f} "
                f">= ${self._max_cost_usd:.4f}. Call blocked."
            )
            return LLMResponse(
                content=f"[Budget exhausted: ${self._max_cost_usd} limit reached]",
                provider=self._provider,
                model=self._model,
            )

        if self._client is None:
            return LLMResponse(
                content="[LLM client not initialized -- check API key and dependencies]",
                provider=self._provider,
                model=self._model,
            )

        start = time.time()
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._call_provider(
                    prompt, temperature, max_tokens
                )
                response.latency_ms = (time.time() - start) * 1000

                self._track_usage(response.usage)

                logger.debug(
                    f"[LLM] {self._provider}/{role}: "
                    f"{response.usage.input_tokens}in "
                    f"({response.usage.cached_input_tokens} cached) + "
                    f"{response.usage.output_tokens}out = "
                    f"{response.usage.total_tokens}tok "
                    f"${response.usage.estimated_cost_usd:.4f} "
                    f"({response.latency_ms:.0f}ms)"
                )
                return response

            except Exception as e:
                last_error = e
                if self._is_retryable(e) and attempt < self._max_retries:
                    delay = min(
                        RETRY_BASE_DELAY * (2**attempt), RETRY_MAX_DELAY
                    )
                    logger.warning(
                        f"[LLM] Retryable error (attempt {attempt + 1}): "
                        f"{type(e).__name__}. Retrying in {delay:.1f}s"
                    )
                    await asyncio.sleep(delay)
                else:
                    break

        logger.error(f"[LLM] Call failed after {self._max_retries + 1} attempts: {last_error}")
        return LLMResponse(
            content=f"[LLM call failed: {type(last_error).__name__}]",
            provider=self._provider,
            model=self._model,
        )

    def _sanitize_prompt(self, prompt: CacheablePrompt) -> CacheablePrompt:
        """Enforce size limits and sanitize prompt content."""
        return CacheablePrompt(
            system=sanitize_for_prompt(
                prompt.system, max_length=self._max_prompt_length // 3
            ),
            context=sanitize_for_prompt(
                prompt.context, max_length=self._max_prompt_length // 3
            ),
            user_message=sanitize_for_prompt(
                prompt.user_message, max_length=self._max_prompt_length // 3
            ),
        )

    async def _call_provider(
        self,
        prompt: CacheablePrompt,
        temperature: float,
        max_tokens: int,
    ) -> LLMResponse:
        """Dispatch to provider-specific implementation."""
        if self._provider == "anthropic":
            return await self._call_anthropic(prompt, temperature, max_tokens)
        elif self._provider == "openai":
            return await self._call_openai(prompt, temperature, max_tokens)
        elif self._provider == "google":
            return await self._call_google(prompt, temperature, max_tokens)
        else:
            raise ValueError(f"Unsupported provider: {self._provider}")

    async def _call_anthropic(
        self, prompt: CacheablePrompt, temperature: float, max_tokens: int
    ) -> LLMResponse:
        """Anthropic Claude with explicit prompt caching (cache_control)."""
        system_blocks = []
        if prompt.system:
            system_blocks.append({
                "type": "text",
                "text": prompt.system,
                "cache_control": {"type": "ephemeral"},
            })
        if prompt.context:
            system_blocks.append({
                "type": "text",
                "text": prompt.context,
                "cache_control": {"type": "ephemeral"},
            })

        messages = [{"role": "user", "content": prompt.user_message}]

        response = await self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_blocks if system_blocks else None,
            messages=messages,
        )

        usage_data = response.usage
        cached = getattr(usage_data, "cache_read_input_tokens", 0)
        input_tok = getattr(usage_data, "input_tokens", 0)
        output_tok = getattr(usage_data, "output_tokens", 0)

        rates = COST_RATES.get("anthropic", {})
        cost = (
            (input_tok - cached) * rates.get("input", 0) / 1000
            + cached * rates.get("cached", 0) / 1000
            + output_tok * rates.get("output", 0) / 1000
        )

        return LLMResponse(
            content=response.content[0].text,
            usage=TokenUsage(
                input_tokens=input_tok,
                output_tokens=output_tok,
                cached_input_tokens=cached,
                estimated_cost_usd=round(cost, 6),
                cache_hit=cached > 0,
            ),
            model=self._model,
            provider="anthropic",
            cached=cached > 0,
        )

    async def _call_openai(
        self, prompt: CacheablePrompt, temperature: float, max_tokens: int
    ) -> LLMResponse:
        """OpenAI with automatic prefix caching."""
        messages = []
        if prompt.system:
            messages.append({"role": "system", "content": prompt.system})
        if prompt.context:
            messages.append({"role": "system", "content": prompt.context})
        messages.append({"role": "user", "content": prompt.user_message})

        response = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        usage_data = response.usage
        input_tok = usage_data.prompt_tokens if usage_data else 0
        output_tok = usage_data.completion_tokens if usage_data else 0
        cached = getattr(usage_data, "prompt_tokens_details", None)
        cached_tok = getattr(cached, "cached_tokens", 0) if cached else 0

        rates = COST_RATES.get("openai", {})
        cost = (
            (input_tok - cached_tok) * rates.get("input", 0) / 1000
            + cached_tok * rates.get("cached", 0) / 1000
            + output_tok * rates.get("output", 0) / 1000
        )

        return LLMResponse(
            content=response.choices[0].message.content or "",
            usage=TokenUsage(
                input_tokens=input_tok,
                output_tokens=output_tok,
                cached_input_tokens=cached_tok,
                estimated_cost_usd=round(cost, 6),
                cache_hit=cached_tok > 0,
            ),
            model=self._model,
            provider="openai",
            cached=cached_tok > 0,
        )

    async def _call_google(
        self, prompt: CacheablePrompt, temperature: float, max_tokens: int
    ) -> LLMResponse:
        """Google Gemini (no explicit caching API in current SDK)."""
        full_prompt = prompt.to_flat_prompt()

        response = await asyncio.to_thread(
            self._client.generate_content,
            full_prompt,
            generation_config={
                "temperature": temperature,
                "max_output_tokens": max_tokens,
            },
        )

        input_tok = 0
        output_tok = 0
        if hasattr(response, "usage_metadata"):
            input_tok = getattr(response.usage_metadata, "prompt_token_count", 0)
            output_tok = getattr(response.usage_metadata, "candidates_token_count", 0)

        return LLMResponse(
            content=response.text,
            usage=TokenUsage(
                input_tokens=input_tok,
                output_tokens=output_tok,
            ),
            model=self._model,
            provider="google",
        )

    def _is_retryable(self, error: Exception) -> bool:
        """Check if an error is transient and worth retrying."""
        error_type = type(error).__name__
        retryable_types = {
            "RateLimitError",
            "APITimeoutError",
            "InternalServerError",
            "ServiceUnavailableError",
            "APIConnectionError",
            "Timeout",
            "ConnectError",
        }
        return error_type in retryable_types

    def _track_usage(self, usage: TokenUsage) -> None:
        """Accumulate usage stats across calls."""
        self._total_usage.input_tokens += usage.input_tokens
        self._total_usage.output_tokens += usage.output_tokens
        self._total_usage.cached_input_tokens += usage.cached_input_tokens
        self._total_usage.total_tokens += usage.total_tokens
        self._total_usage.estimated_cost_usd += usage.estimated_cost_usd

    @property
    def total_usage(self) -> TokenUsage:
        """Cumulative token usage across all calls in this client's lifetime."""
        return self._total_usage

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def model(self) -> str:
        return self._model


# =============================================================================
# FACTORY
# =============================================================================


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
      2. ANTHROPIC_API_KEY set -> anthropic
      3. OPENAI_API_KEY set -> openai
      4. GOOGLE_API_KEY set -> google
      5. Default: anthropic
    """
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
