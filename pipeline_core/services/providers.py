"""
Data-provider abstraction — an ordered fallback chain with retries/backoff and
per-provider health tracking.

The platform runs on free/public sources with no SLA (yfinance, Binance, CoinGecko,
NSE, …), so any single one can rate-limit or break. `fetch_with_fallback` tries a
list of providers in order, returns the first valid result, retries transient
failures with exponential backoff, and records each provider's success/failure so a
degrading source is visible (`/api/v1/health/providers`).

Providers are plain callables — this layer is source-agnostic. Its first use is
crypto OHLCV (Binance → Kraken; yfinance remains the final fallback at the
market-data layer), and it's designed to wrap equity price/fundamental sources next.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# In-memory per-provider health (process-local; fine for a single-user app).
_HEALTH: dict[str, dict] = {}


def _record(name: str, ok: bool, err: Optional[Exception] = None, elapsed: Optional[float] = None) -> None:
    h = _HEALTH.setdefault(
        name, {"calls": 0, "ok": 0, "fail": 0, "last_error": None, "last_ok_at": None, "last_latency_ms": None},
    )
    h["calls"] += 1
    if ok:
        h["ok"] += 1
        h["last_ok_at"] = datetime.now(timezone.utc).isoformat()
        h["last_error"] = None
    else:
        h["fail"] += 1
        if err is not None:
            h["last_error"] = f"{type(err).__name__}: {err}"[:200]
    if elapsed is not None:
        h["last_latency_ms"] = round(elapsed * 1000)


def provider_health() -> list[dict]:
    """Per-provider stats for the health surface (success rate, last error, latency)."""
    out = []
    for name, h in sorted(_HEALTH.items()):
        rate = round(h["ok"] / h["calls"] * 100, 1) if h["calls"] else None
        out.append({"provider": name, "success_rate_pct": rate, **h})
    return out


def reset_health() -> None:
    _HEALTH.clear()


def fetch_with_fallback(
    providers: list[tuple[str, Callable[[], object]]],
    *,
    validate: Optional[Callable[[object], bool]] = None,
    retries: int = 1,
    backoff: float = 0.4,
) -> tuple[Optional[object], Optional[str]]:
    """Try each `(name, fn)` in order; return `(result, provider_name)` for the first
    result that passes `validate` (default: non-None). Transient exceptions are retried
    per provider up to `retries` times with exponential backoff; an empty/invalid result
    (no exception) moves straight to the next provider. Returns `(None, None)` if all fail.
    """
    for name, fn in providers:
        for attempt in range(retries + 1):
            t0 = time.time()
            try:
                result = fn()
            except Exception as e:  # transient (network/rate-limit) — retry then fall through
                _record(name, False, err=e, elapsed=time.time() - t0)
                if attempt < retries:
                    time.sleep(backoff * (2 ** attempt))
                    continue
                break
            ok = result is not None and (validate is None or validate(result))
            _record(name, ok, elapsed=time.time() - t0)
            if ok:
                return result, name
            break  # reached the source but it had nothing usable → try the next provider
    return None, None
