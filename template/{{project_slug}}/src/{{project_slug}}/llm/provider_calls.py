"""
Provider-specific LLM call implementations (Anthropic, OpenAI, Google).

Split out of client.py to keep the client focused on the call lifecycle
(sanitization, budget checks, retries, routing, usage tracking); these
functions only translate a CacheablePrompt into one provider SDK call
and the SDK response into an LLMResponse. Each takes the SDK handle the
client constructed plus the model to call -- the model is per-call so
router selection and cascade can override the configured default.

Imports from client are deferred to function bodies: client.py imports
this module, so a top-level back-import would be a cycle.

Keep this file under 250 lines.
"""

import asyncio

# Cost per 1K tokens (approximate, varies by model -- override via config)
COST_RATES = {
    "anthropic": {"input": 0.003, "cached": 0.0003, "output": 0.015},
    "openai": {"input": 0.005, "cached": 0.0025, "output": 0.015},
    "google": {"input": 0.0, "cached": 0.0, "output": 0.0},
}


def _cost(provider: str, input_tok: int, cached: int, output_tok: int) -> float:
    rates = COST_RATES.get(provider, {})
    return (
        (input_tok - cached) * rates.get("input", 0) / 1000
        + cached * rates.get("cached", 0) / 1000
        + output_tok * rates.get("output", 0) / 1000
    )


async def call_anthropic(sdk, prompt, temperature: float, max_tokens: int, model: str):
    """Anthropic Claude with explicit prompt caching (cache_control)."""
    from .client import LLMResponse, TokenUsage

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

    response = await sdk.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_blocks if system_blocks else None,
        messages=messages,
    )

    usage_data = response.usage
    cached = getattr(usage_data, "cache_read_input_tokens", 0)
    input_tok = getattr(usage_data, "input_tokens", 0)
    output_tok = getattr(usage_data, "output_tokens", 0)
    cost = _cost("anthropic", input_tok, cached, output_tok)

    return LLMResponse(
        content=response.content[0].text,
        usage=TokenUsage(
            input_tokens=input_tok,
            output_tokens=output_tok,
            cached_input_tokens=cached,
            estimated_cost_usd=round(cost, 6),
            cache_hit=cached > 0,
        ),
        model=model,
        provider="anthropic",
        cached=cached > 0,
    )


async def call_openai(sdk, prompt, temperature: float, max_tokens: int, model: str):
    """OpenAI with automatic prefix caching."""
    from .client import LLMResponse, TokenUsage

    messages = []
    if prompt.system:
        messages.append({"role": "system", "content": prompt.system})
    if prompt.context:
        messages.append({"role": "system", "content": prompt.context})
    messages.append({"role": "user", "content": prompt.user_message})

    response = await sdk.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )

    usage_data = response.usage
    input_tok = usage_data.prompt_tokens if usage_data else 0
    output_tok = usage_data.completion_tokens if usage_data else 0
    cached = getattr(usage_data, "prompt_tokens_details", None)
    cached_tok = getattr(cached, "cached_tokens", 0) if cached else 0
    cost = _cost("openai", input_tok, cached_tok, output_tok)

    return LLMResponse(
        content=response.choices[0].message.content or "",
        usage=TokenUsage(
            input_tokens=input_tok,
            output_tokens=output_tok,
            cached_input_tokens=cached_tok,
            estimated_cost_usd=round(cost, 6),
            cache_hit=cached_tok > 0,
        ),
        model=model,
        provider="openai",
        cached=cached_tok > 0,
    )


async def call_google(
    sdk, configured_model: str, prompt, temperature: float, max_tokens: int, model: str
):
    """Google Gemini (no explicit caching API in current SDK)."""
    from .client import LLMResponse, TokenUsage

    full_prompt = prompt.to_flat_prompt()

    # The Gemini SDK binds the model at client construction; a routed
    # per-call model needs its own (cheap, local) model handle.
    client = sdk
    if model != configured_model:
        import google.generativeai as genai

        client = genai.GenerativeModel(model)

    response = await asyncio.to_thread(
        client.generate_content,
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
        model=model,
        provider="google",
    )
