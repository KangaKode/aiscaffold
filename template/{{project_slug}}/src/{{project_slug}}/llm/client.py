"""
Provider-agnostic LLM client with prompt caching and token tracking.

Supports Anthropic (Claude), OpenAI (GPT), Google (Gemini) -- the
provider-specific call implementations live in provider_calls.py; this
module owns the call lifecycle (context-pressure detection, sanitization,
budget checks, retries, opt-in model routing, usage tracking).
Uses CacheablePrompt(system, context, user_message) for automatic caching.
"""

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..observability.metrics import record_llm_call
from ..security.prompt_guard import sanitize_for_prompt
from .budget_manager import enforce_budget, record_response_spend
from .context_pressure import check_context_pressure
from .model_router import cascade_for_call, route_for_call

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120
DEFAULT_MAX_RETRIES = 2
DEFAULT_MAX_PROMPT_LENGTH = 200_000
RETRY_BASE_DELAY = 1.0
RETRY_MAX_DELAY = 30.0
RETRYABLE_ERRORS = {
    "RateLimitError", "APITimeoutError", "InternalServerError",
    "ServiceUnavailableError", "APIConnectionError", "Timeout", "ConnectError",
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
    """Response from an LLM call -- drop-in compatible with existing code.

    is_error/error_type implement an explicit ok/error contract (pattern
    borrowed from a sibling platform's response contract): every error
    return from the client sets is_error=True with a machine-readable
    error_type, so callers can fail closed instead of parsing bracketed
    error strings out of ``content``.
    """

    content: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    model: str = ""
    provider: str = ""
    latency_ms: float = 0.0
    cached: bool = False
    is_error: bool = False
    error_type: str = ""


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
        budget_manager: Any = None,
    ):
        self._provider = provider.lower()
        self._model = model or self._default_model()
        self._api_key = api_key or self._load_api_key()
        self._timeout = timeout
        self._max_retries = max_retries
        # Single source of truth for the per-field truncation point --
        # shared by _sanitize_prompt and the context-pressure check so
        # the two can never desync. (Replaces the former
        # _max_prompt_length attribute, which nothing read directly.)
        self._per_field_cap = max_prompt_length // 3
        self._max_cost_usd = max_cost_usd
        # Optional BudgetManager; None = no checks (zero-config unchanged)
        self.budget_manager = budget_manager
        # Optional ModelRouter (MODEL_ROUTING_ENABLED); None = every call
        # uses the single configured model, byte-identical to pre-routing.
        self.model_router: Any = None
        self._client: Any = None
        self._total_usage = TokenUsage()
        # _track_usage runs in asyncio.to_thread workers, so concurrent
        # calls on one shared client (the round-table fan-out case) would
        # lose += updates without a lock -- undercounting the _max_cost_usd
        # circuit breaker.
        self._usage_lock = threading.Lock()

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

        # Detect-only, opt-in (default off), never raises. Measured on
        # what the caller ASSEMBLED, before _sanitize_prompt truncates.
        check_context_pressure(prompt, role, self._per_field_cap)

        prompt = self._sanitize_prompt(prompt)

        if self._max_cost_usd is not None and self._total_usage.estimated_cost_usd >= self._max_cost_usd:
            logger.error(
                f"[LLM] Budget exhausted: ${self._total_usage.estimated_cost_usd:.4f} "
                f">= ${self._max_cost_usd:.4f}. Call blocked."
            )
            return LLMResponse(
                content=f"[Budget exhausted: ${self._max_cost_usd} limit reached]",
                provider=self._provider,
                model=self._model,
                is_error=True,
                error_type="budget_exhausted",
            )

        # Budget checks read the spend ledger (blocking store I/O) -- run
        # off the loop. BudgetExceededError still propagates to the caller.
        await asyncio.to_thread(enforce_budget, self.budget_manager)

        if self._client is None:
            return LLMResponse(
                content="[LLM client not initialized -- check API key and dependencies]",
                provider=self._provider,
                model=self._model,
                is_error=True,
                error_type="client_not_initialized",
            )

        # Per-call model selection (opt-in): a router maps the call role to
        # a cost tier; without one, self._model -- behavior unchanged.
        model = self._model
        if self.model_router is not None:
            model = route_for_call(
                self.model_router, role, prompt.to_flat_prompt(), self._model
            )

        start = time.time()
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._call_provider(
                    prompt, temperature, max_tokens, model
                )
                response.latency_ms = (time.time() - start) * 1000

                # Spend recording writes to the store -- off the loop.
                await asyncio.to_thread(self._track_usage, response.usage, model)

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

        # Opt-in cascade: with a router attached, try ONE step up-tier
        # before returning the error (the failure also feeds the router's
        # circuit breaker). Without a router this block never runs.
        if self.model_router is not None:
            fallback = cascade_for_call(self.model_router, role, model)
            if fallback:
                try:
                    response = await self._call_provider(
                        prompt, temperature, max_tokens, fallback
                    )
                    response.latency_ms = (time.time() - start) * 1000
                    await asyncio.to_thread(
                        self._track_usage, response.usage, fallback
                    )
                    logger.info(
                        f"[LLM] Cascade to '{fallback}' succeeded after "
                        f"'{model}' failed"
                    )
                    return response
                except Exception as e:
                    logger.warning(
                        f"[LLM] Cascade attempt to '{fallback}' failed: "
                        f"{type(e).__name__}"
                    )

        return LLMResponse(
            content=f"[LLM call failed: {type(last_error).__name__}]",
            provider=self._provider,
            model=model,
            is_error=True,
            error_type="call_failed",
        )

    def _sanitize_prompt(self, prompt: CacheablePrompt) -> CacheablePrompt:
        """Enforce size limits and sanitize prompt content."""
        return CacheablePrompt(
            system=sanitize_for_prompt(prompt.system, max_length=self._per_field_cap),
            context=sanitize_for_prompt(
                prompt.context, max_length=self._per_field_cap
            ),
            user_message=sanitize_for_prompt(
                prompt.user_message, max_length=self._per_field_cap
            ),
        )

    async def _call_provider(
        self,
        prompt: CacheablePrompt,
        temperature: float,
        max_tokens: int,
        model: str | None = None,
    ) -> LLMResponse:
        """Dispatch to the provider implementation (llm/provider_calls.py).

        model overrides the configured default for this one call (router
        selection / cascade); None keeps the configured model. Deferred
        import: provider_calls imports LLMResponse back from this module.
        """
        from .provider_calls import call_anthropic, call_google, call_openai

        model = model or self._model
        if self._provider == "anthropic":
            return await call_anthropic(
                self._client, prompt, temperature, max_tokens, model
            )
        elif self._provider == "openai":
            return await call_openai(
                self._client, prompt, temperature, max_tokens, model
            )
        elif self._provider == "google":
            return await call_google(
                self._client, self._model, prompt, temperature, max_tokens, model
            )
        else:
            raise ValueError(f"Unsupported provider: {self._provider}")

    def _is_retryable(self, error: Exception) -> bool:
        """Check if an error is transient and worth retrying."""
        return type(error).__name__ in RETRYABLE_ERRORS

    def _track_usage(self, usage: TokenUsage, model: str | None = None) -> None:
        """Accumulate usage stats and record tenant spend when budgets are on.

        model is the model actually called (router selection / cascade);
        None keeps the configured default -- identical without a router.
        Thread-safe: called from asyncio.to_thread workers, so the shared
        accumulator is guarded (see _usage_lock).
        """
        model = model or self._model
        with self._usage_lock:
            self._total_usage.input_tokens += usage.input_tokens
            self._total_usage.output_tokens += usage.output_tokens
            self._total_usage.cached_input_tokens += usage.cached_input_tokens
            self._total_usage.total_tokens += usage.total_tokens
            self._total_usage.estimated_cost_usd += usage.estimated_cost_usd
        record_response_spend(
            self.budget_manager, usage.estimated_cost_usd, model
        )
        record_llm_call(
            self._provider,
            model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.estimated_cost_usd,
        )

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

