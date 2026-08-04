"""
Bot framework — a tiny registry so trading bots share one interface.

A **signal** bot is a pure function `evaluate(df, ctx) -> signal` over one coin's OHLCV
on a chosen timeframe; it decides long/flat and gives entry / stop / target levels.
A **basket** bot (the daily momentum bot) ranks the whole universe instead.

The scanner ([`scan.py`](scan.py)) loads the data (and, for multi-timeframe bots, the
higher-timeframe + BTC frames) and runs `evaluate` across the universe.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

import pandas as pd

from pipeline_core.utils import round_price

# ctx passed to a signal bot's evaluate(): the coin's df plus optional context frames.
EvalFn = Callable[[pd.DataFrame, dict], dict]


@dataclass(frozen=True)
class BotDef:
    id: str
    name: str
    kind: str                       # "signal" | "basket"
    description: str
    default_timeframe: str
    timeframes: list[str]
    needs_htf: bool = False         # scanner loads htf_for(timeframe) per coin
    needs_btc: bool = False         # scanner loads BTC at the higher timeframe
    evaluate: Optional[EvalFn] = None

    def to_public(self) -> dict:
        return {
            "id": self.id, "name": self.name, "kind": self.kind,
            "description": self.description, "default_timeframe": self.default_timeframe,
            "timeframes": list(self.timeframes),
            "needs_htf": self.needs_htf, "needs_btc": self.needs_btc,
        }


BOTS: dict[str, BotDef] = {}
_LOADED = False


def register(bot: BotDef) -> None:
    BOTS[bot.id] = bot


def _ensure_loaded() -> None:
    """Import bot modules so they self-register (idempotent)."""
    global _LOADED
    if _LOADED:
        return
    from pipeline_core.bots import signals  # noqa: F401 — registers the signal bots
    # The daily momentum basket bot (implemented in crypto_momentum.py) — registered
    # here so that module stays focused on its own logic.
    register(BotDef(
        id="crypto-momentum", name="Crypto Momentum (daily basket)", kind="basket",
        description="BTC-regime-gated, inverse-vol-weighted top-N momentum basket (daily).",
        default_timeframe="1d", timeframes=["1d"],
    ))
    _LOADED = True


def get_bot(bot_id: str) -> Optional[BotDef]:
    _ensure_loaded()
    return BOTS.get(bot_id)


def list_bots() -> list[dict]:
    _ensure_loaded()
    # Signal bots first (the intraday family), then basket bots.
    order = {"signal": 0, "basket": 1}
    return [b.to_public() for b in sorted(BOTS.values(), key=lambda b: (order.get(b.kind, 9), b.name))]


# Lower timeframe → the higher timeframe its trend filter uses (MTF bots).
_HTF_MAP = {"5m": "1h", "10m": "1h", "15m": "1h", "30m": "4h", "1h": "4h", "4h": "1d", "1d": "1d"}


def htf_for(timeframe: str) -> str:
    return _HTF_MAP.get(timeframe, "4h")


def reward_risk(side: str, entry, stop, target) -> Optional[float]:
    """Reward:risk (R) multiple for a long or short setup, or None if the geometry is invalid.

    long  → stop below entry, target above  (risk = entry−stop, reward = target−entry)
    short → stop above entry, target below  (risk = stop−entry, reward = entry−target)
    """
    if not (entry and stop and target):
        return None
    if side == "long" and stop < entry:
        risk, reward = entry - stop, target - entry
    elif side == "short" and stop > entry:
        risk, reward = stop - entry, entry - target
    else:
        return None
    return round(reward / risk, 2) if risk > 0 and reward > 0 else None


def make_signal(
    state: str,
    entry: Optional[float] = None,
    stop: Optional[float] = None,
    target: Optional[float] = None,
    reason: str = "",
    indicators: Optional[dict] = None,
) -> dict:
    """Build a signal dict (long **or short**) with a computed reward:risk (R) multiple."""
    return {
        "state": state,
        "side": state if state in ("long", "short") else "flat",
        "entry": round_price(entry),
        "stop": round_price(stop),
        "target": round_price(target),
        "r_multiple": reward_risk(state, entry, stop, target),
        "reason": reason,
        "indicators": indicators or {},
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


def flat(reason: str = "no setup") -> dict:
    return make_signal("flat", reason=reason)
