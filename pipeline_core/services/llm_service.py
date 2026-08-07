"""
LLM abstraction service — supports OpenAI, Anthropic, Gemini, and Ollama.

Gemini rides the OpenAI-compatible endpoint (reusing the openai SDK with a
Google base_url), so it needs no extra dependency.

Every LLM call site has a rules-only fallback: if no provider is configured
(or the provider errors), the pipeline keeps working deterministically.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

from pipeline_core.config import settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMUnavailableError(RuntimeError):
    """Raised when no usable LLM provider is configured."""


# ── Provider circuit breaker ─────────────────────────────────────────
# When the configured provider starts failing — most importantly a quota / rate-limit (HTTP 429)
# that won't clear for a while — every subsequent call would otherwise repeat the same slow, doomed
# round-trip (the SDK retries 429s with backoff before finally raising). Under a per-item fan-out
# (e.g. per-coin idea sentiment) that turns one dead provider into many seconds of latency for zero
# value. The breaker trips after a quota error (or `llm_breaker_threshold` consecutive failures) and
# short-circuits calls for `llm_breaker_cooldown_s`, so callers reach their deterministic fallback
# immediately instead of once per item. Process-local and self-healing.
_breaker = {"open_until": 0.0, "fails": 0}


def _breaker_open() -> bool:
    return time.monotonic() < _breaker["open_until"]


def reset_llm_breaker() -> None:
    """Clear the breaker — used on a healthy response and as a test hook."""
    _breaker["open_until"] = 0.0
    _breaker["fails"] = 0


def _is_quota_error(err: Exception) -> bool:
    """A rate-limit / quota-exhausted provider error — one that won't clear on immediate retry."""
    s = str(err).lower()
    return any(k in s for k in ("429", "quota", "resource_exhausted", "rate limit", "rate_limit"))


def _breaker_record(ok: bool, err: Optional[Exception] = None) -> None:
    if ok:
        reset_llm_breaker()
        return
    _breaker["fails"] += 1
    if (err is not None and _is_quota_error(err)) or _breaker["fails"] >= settings.llm_breaker_threshold:
        _breaker["open_until"] = time.monotonic() + settings.llm_breaker_cooldown_s
        logger.warning(
            f"LLM circuit breaker OPEN for {settings.llm_breaker_cooldown_s}s after "
            f"{_breaker['fails']} failure(s); short-circuiting to rules-only."
        )


def llm_available() -> bool:
    """True if a provider with credentials (or local Ollama) is configured."""
    provider = settings.llm_provider.lower()
    if provider == "openai":
        return bool(settings.openai_api_key)
    if provider == "anthropic":
        return bool(settings.anthropic_api_key)
    if provider == "gemini":
        return bool(settings.gemini_api_key)
    if provider == "ollama":
        return True  # assume local daemon; errors degrade gracefully
    return False


def llm_mode() -> str:
    return settings.llm_provider.lower() if llm_available() else "rules-only"


async def llm_complete(
    prompt: str,
    system_prompt: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: int = 2000,
) -> str:
    """
    Send a completion request to the configured LLM provider.
    Raises LLMUnavailableError when no provider is configured — callers
    decide their fallback. Provider errors also raise (no silent
    '[LLM Error]' strings leaking into reports).
    """
    if not llm_available():
        raise LLMUnavailableError(
            f"LLM provider '{settings.llm_provider}' is not configured "
            "(missing credentials or provider 'none')."
        )
    if _breaker_open():
        raise LLMUnavailableError(
            "LLM temporarily disabled by the circuit breaker after repeated provider failures."
        )

    try:
        result = await _dispatch(prompt, system_prompt, temperature, max_tokens)
    except LLMUnavailableError:
        raise                            # misconfiguration, not a provider failure — don't count it
    except Exception as e:               # provider / network / quota error — record, maybe trip
        _breaker_record(False, e)
        raise
    _breaker_record(True)
    return result


async def _dispatch(prompt, system_prompt, temperature, max_tokens) -> str:
    """Route to the configured provider (the raw call the circuit breaker wraps)."""
    provider = settings.llm_provider.lower()
    if provider == "openai":
        return await _openai_complete(prompt, system_prompt, temperature, max_tokens)
    if provider == "anthropic":
        return await _anthropic_complete(prompt, system_prompt, temperature, max_tokens)
    if provider == "gemini":
        return await _gemini_complete(prompt, system_prompt, temperature, max_tokens)
    if provider == "ollama":
        return await _ollama_complete(prompt, system_prompt, temperature, max_tokens)
    raise LLMUnavailableError(f"Unknown LLM provider: {provider}")


async def llm_structured(
    prompt: str,
    schema: Type[T],
    system_prompt: Optional[str] = None,
    temperature: float = 0.1,
    max_tokens: int = 1000,
    retries: int = 1,
) -> Optional[T]:
    """
    Ask the LLM for JSON and validate it against a Pydantic schema.
    Retries (with the validation error fed back) up to `retries` times.
    Returns None if the LLM is unavailable or validation keeps failing.
    """
    if not llm_available():
        return None

    schema_hint = json.dumps(
        {k: str(v.get("type", "any")) for k, v in schema.model_json_schema().get("properties", {}).items()},
        indent=2,
    )
    base_prompt = (
        f"{prompt}\n\n"
        f"Respond with ONLY a JSON object matching these fields (omit fields not mentioned):\n{schema_hint}"
    )

    last_error: Optional[str] = None
    for attempt in range(retries + 1):
        attempt_prompt = base_prompt
        if last_error:
            attempt_prompt += (
                f"\n\nYour previous response failed validation: {last_error}. "
                "Return corrected JSON only."
            )
        try:
            raw = await llm_complete(attempt_prompt, system_prompt, temperature, max_tokens)
        except Exception as e:  # provider/network failure — no point retrying validation
            logger.error(f"LLM structured call failed: {e}")
            return None

        data = extract_json(raw)
        if data is None:
            last_error = "response was not valid JSON"
            continue
        try:
            return schema.model_validate(data)
        except ValidationError as e:
            last_error = str(e)[:500]
            logger.warning(f"LLM JSON failed schema validation (attempt {attempt + 1}): {e}")

    logger.error(f"LLM structured output failed after {retries + 1} attempts")
    return None


def extract_json(text: str) -> Optional[dict]:
    """
    Robustly extract the first JSON object from an LLM response.
    Handles markdown fences, leading prose, and trailing commentary.
    """
    if not text:
        return None
    cleaned = text.strip()

    # Strip a single markdown fence wrapper if present
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)

    try:
        result = json.loads(cleaned)
        return result if isinstance(result, dict) else None
    except json.JSONDecodeError:
        pass

    # Fall back to the first {...} block in the text
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group(0))
            return result if isinstance(result, dict) else None
        except json.JSONDecodeError:
            return None
    return None


async def _openai_complete(
    prompt, system_prompt, temperature, max_tokens,
    *, base_url=None, api_key=None, model=None, extra_params=None,
) -> str:
    """OpenAI chat-completions call. Also serves any OpenAI-compatible endpoint
    (e.g. Gemini) via base_url/api_key/model/extra_params overrides."""
    from openai import AsyncOpenAI

    # max_retries=0: fail fast on a 429/5xx so our own circuit breaker trips immediately and the
    # caller falls back to rules-only — instead of the SDK burning seconds of internal backoff per
    # call (the dominant cost when a provider is rate-limited under a per-item fan-out).
    client = AsyncOpenAI(api_key=api_key or settings.openai_api_key, base_url=base_url,
                         max_retries=settings.llm_max_retries)
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    response = await client.chat.completions.create(
        model=model or settings.llm_model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        **(extra_params or {}),
    )
    return response.choices[0].message.content or ""


async def _gemini_complete(prompt, system_prompt, temperature, max_tokens) -> str:
    """Gemini via its OpenAI-compatible endpoint (reuses the openai SDK)."""
    # Default llm_model is an OpenAI name; fall back to a Gemini model unless overridden.
    model = settings.llm_model if "gemini" in settings.llm_model.lower() else "gemini-2.5-flash"
    extra: dict = {}
    # Gemini 2.5 Flash "thinks" by default, which can burn the entire token budget
    # before any answer is produced — fatal for our short, bounded calls (esp. the
    # structured-JSON parser). Turn thinking off for deterministic, complete output.
    # (2.5 Pro cannot fully disable thinking, so it's left alone.)
    if "2.5-flash" in model.lower():
        extra["reasoning_effort"] = "none"
    return await _openai_complete(
        prompt, system_prompt, temperature, max_tokens,
        base_url=settings.gemini_base_url, api_key=settings.gemini_api_key,
        model=model, extra_params=extra,
    )


async def _anthropic_complete(prompt, system_prompt, temperature, max_tokens) -> str:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    response = await client.messages.create(
        model=settings.llm_model if "gpt" not in settings.llm_model else "claude-sonnet-4-5",
        max_tokens=max_tokens,
        system=system_prompt or "You are a financial analyst.",
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
    )
    return response.content[0].text


async def _ollama_complete(prompt, system_prompt, temperature, max_tokens) -> str:
    import httpx

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    model = settings.llm_model if "gpt" not in settings.llm_model else "llama3"
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{settings.ollama_base_url}/api/chat",
            json={
                "model": model,
                "messages": messages,
                "stream": False,
                "options": {"temperature": temperature, "num_predict": max_tokens},
            },
            timeout=120,
        )
        data = response.json()
        return data.get("message", {}).get("content", "")


_INTENT_COMMON = """Given a natural language query, extract structured filters as JSON.

Guidance:
- Percentages as decimals unless the field is an RSI level or a pct distance (0.15 = 15%).
- Return ONLY valid JSON. Omit fields not mentioned."""

# Per-market guidance appended to the common rules above.
_INTENT_MARKET_HINTS = {
    "IN": """You are a filter parser for Indian stocks (NSE/BSE).
- promoter_holding_min=0.5 means "promoters own at least 50%".
- market caps (market_cap_min_cr/max_cr) are in INR crores (1000 Cr+ is large-cap).
- "cheap/undervalued" → pe_ratio_max, pb_ratio_max. "quality" → roe_min/roce_min, low debt.""",
    "US": """You are a filter parser for US stocks (NYSE/NASDAQ).
- market caps (market_cap_min_cr/max_cr) are in USD billions (10+ is large-cap; there is no promoter/pledge/delivery data).
- "cheap/undervalued" → pe_ratio_max, pb_ratio_max. "quality" → roe_min/roce_min, low debt.""",
    "METALS": """You are a filter parser for metals (gold, silver, platinum, copper — futures/ETFs).
- These are price-only instruments: ONLY technical filters apply (rsi_min/max, price_above_50dma/200dma,
  macd_bullish, near_52w_high_pct/near_52w_low_pct, volume_spike_min). There are NO fundamentals
  (no P/E, ROE, market cap, promoter data) — never emit those fields.""",
    "CRYPTO": """You are a filter parser for cryptocurrencies (BTC, ETH, …).
- Mostly price-only: technical filters apply (rsi_min/max, price_above_50dma/200dma, macd_bullish,
  near_52w_high_pct/near_52w_low_pct, volume_spike_min) plus market_cap_min_cr/max_cr in USD billions.
- There are NO equity fundamentals (no P/E, ROE, promoter data) — never emit those fields.""",
}


def _intent_system_prompt(market: str) -> str:
    hint = _INTENT_MARKET_HINTS.get((market or "IN").upper(), _INTENT_MARKET_HINTS["IN"])
    return f"{hint}\n\n{_INTENT_COMMON}"


# Back-compat: the IN prompt some callers/tests may import directly.
INTENT_PARSE_SYSTEM_PROMPT = _intent_system_prompt("IN")


async def parse_natural_language_query(query: str, market: str = "IN") -> dict:
    """
    Parse a natural-language screening query into a ScreenFilters-validated dict.
    The prompt is market-aware (units + which filters exist). Returns {} when the
    LLM is unavailable or parsing fails — callers merge this over explicit
    filters, so {} is always safe.
    """
    from pipeline_core.models.schemas import ScreenFilters

    parsed = await llm_structured(
        prompt=f'Parse this {(market or "IN").upper()} market screening query into filters:\n\n"{query}"',
        schema=ScreenFilters,
        system_prompt=_intent_system_prompt(market),
        temperature=0.1,
        max_tokens=600,
        retries=1,
    )
    if parsed is None:
        return {}
    return parsed.model_dump(exclude_unset=True)
