"""
Smart-money / coattail layer — a market-agnostic "follow the smart money" overlay.

StockSense's SEC-13F work (services/edgar_13f.py) proved one instance of a general idea:
find who the proven, informed money is in a market and treat their positioning as an extra,
INDEPENDENT signal family. But *who* the smart money is — and how you observe them — is
completely different per market, so each needs its own source module behind one interface:

  US      large concentrated 13F managers + famed gurus (SEC filings)   → edgar_13f          (here)
  METALS  the commercials (producers/hedgers) in CFTC COT               → services/cot        (Phase 1)
  CRYPTO  exchange netflows + top perp traders (Hyperliquid)            → crypto_smartmoney   (Phase 2)
  IN      named bulk/block-deal buyers + FII/DII flows                  → in_smartmoney       (Phase 3)

This module is the registry/dispatch that hides those differences behind ONE normalized
signal, mirroring pipeline_core/markets.py. Three seams, matching what edgar_13f already
feeds today:
  • `get_signal(market, symbol)`   — the live overlay for the ideas board / badge / pillar.
  • `get_series(market, symbol)`   — a backtestable historical series (disclosure-dated).
  • `enrich_frames(market, frames)`— merge the series onto daily bars for the "follow-the-
                                     smart-money" backtest strategies (no-op where no source).

Each native source module exposes the same tiny contract — `live_signal(symbol)`,
`series(symbol)`, `enrich_frames(df_by_ticker)` — so a market lights up the moment its module
lands. Missing sources degrade to None / no-op; nothing here ever raises.

Anti-lookahead: every historical series is indexed by DISCLOSURE date and forward-filled,
exactly as the 13F merge does — a backtest can never see a filing/report before it was public.
"""

from __future__ import annotations

import importlib
import logging
from types import ModuleType
from typing import Optional

import pandas as pd

from pipeline_core.config import settings
from pipeline_core.markets import get_market

logger = logging.getLogger(__name__)

# Price-only markets get their smart-money family from a dedicated source module (equity
# markets use the ownership snapshot / 13F baseline instead). market key → (module, flag).
_NATIVE_SOURCES: dict[str, tuple[str, str]] = {
    "METALS": ("cot", "sm_metals_cot_enabled"),
    "CRYPTO": ("crypto_smartmoney", "sm_crypto_flows_enabled"),
    "IN": ("in_smartmoney", "sm_in_bulk_deals_enabled"),
}


# ── Normalized signal schema ────────────────────────────────────────
# A strict SUPERSET of the old `inst_ownership` dict, so the existing 🏦 badge and its tests
# keep working while new markets fill the market-agnostic fields (direction/conviction/actors).
#   direction     "accumulate" | "distribute" | None (can't judge — e.g. a level-only snapshot)
#   net_delta_pct signed change in the smart money's positioning (QoQ 13F delta, COT net %, …)
#   conviction    "high" | "medium" | "low" | None  — a coarse strength read
#   actors        names of the specific investors/managers behind it (when known)
#   n_actors      how many independent actors confirm it (13F manager count, COT trader count)
#   label/detail  short chip text + a one-line human explanation for the UI


def _conviction(direction: Optional[str], net_delta_pct: Optional[float],
                n_actors: Optional[int]) -> Optional[str]:
    """Coarse high/medium/low from delta magnitude + breadth. None unless accumulating."""
    if direction != "accumulate":
        return None
    mag = abs(net_delta_pct) if net_delta_pct is not None else 0.0
    breadth = n_actors or 0
    if mag >= 10 or breadth >= 5:
        return "high"
    if mag >= 3 or breadth >= 2:
        return "medium"
    return "low"


def normalize_ownership(own: Optional[dict]) -> Optional[dict]:
    """Wrap an `inst_ownership`-shaped dict (edgar-13f or yfinance snapshot, from
    market_data.get_institutional_ownership) into the normalized smart-money signal.
    None when ownership is unavailable. Preserves every original key (superset) so the
    existing 🏦 badge renders unchanged; adds the market-agnostic fields on top."""
    if not own or not own.get("available"):
        return None
    qoq = own.get("qoq_change_pct")
    direction = None
    if qoq is not None:
        direction = "accumulate" if qoq > 0 else "distribute" if qoq < 0 else None
    n_actors = own.get("n_managers")
    is_13f = own.get("source") == "edgar-13f"
    sig = dict(own)  # keep the original fields (available/source/held_pct_*/qoq/n_managers)
    sig.update({
        "family": "ownership",
        "direction": direction,
        "net_delta_pct": qoq,
        "n_actors": n_actors,
        "actors": own.get("actors") or [],
        "conviction": _conviction(direction, qoq, n_actors),
        "label": "13F" if is_13f else "ownership",
        "detail": (f"{n_actors} managers · {qoq:+.1f}% QoQ"
                   if is_13f and qoq is not None and n_actors else None),
    })
    return sig


# ── Per-market source dispatch (lazy — a market lights up as its module lands) ──


def _native_module(key: str) -> Optional[ModuleType]:
    """Import a price-only market's native smart-money source module, or None when the
    source is switched off / not built yet (an earlier phase). Never raises."""
    spec = _NATIVE_SOURCES.get(key)
    if not spec:
        return None
    modname, flag = spec
    if not getattr(settings, flag, False):
        return None
    try:
        return importlib.import_module(f"pipeline_core.services.{modname}")
    except ImportError:
        return None
    except Exception as e:      # pragma: no cover — defensive
        logger.warning("smart_money: failed importing %s: %s", modname, e)
        return None


def _ownership_signal(key: str, symbol: str) -> Optional[dict]:
    """The 13F/ownership baseline (equity markets; None for price-only)."""
    try:
        from pipeline_core.services.market_data import get_institutional_ownership
        return normalize_ownership(get_institutional_ownership(symbol, key))
    except Exception as e:
        logger.warning("smart_money ownership lookup failed (%s %s): %s", key, symbol, e)
        return None


def get_signal(market: Optional[str], symbol: str) -> Optional[dict]:
    """The live smart-money overlay for one symbol, normalized across markets. Equity markets
    start from the ownership/13F baseline; a market-native source (COT/flows/deals) augments
    or overrides it when present. None when nothing is available. Never raises."""
    if not settings.smart_money_enabled:
        return None
    key = get_market(market).key
    sig = _ownership_signal(key, symbol)
    mod = _native_module(key)
    if mod is not None and hasattr(mod, "live_signal"):
        try:
            native = mod.live_signal(symbol)
        except Exception as e:
            logger.warning("smart_money native live (%s %s): %s", key, symbol, e)
            native = None
        if native:
            sig = {**(sig or {}), **native}     # native wins on shared keys
    return sig


def get_series(market: Optional[str], symbol: str) -> Optional[pd.DataFrame]:
    """The backtestable historical positioning series for one symbol (disclosure-dated),
    or None. Generalizes edgar_13f.get_institutional_series across markets."""
    if not settings.smart_money_enabled:
        return None
    key = get_market(market).key
    try:
        if key == "US":
            from pipeline_core.services import edgar_13f
            return edgar_13f.get_institutional_series(symbol)
        mod = _native_module(key)
        if mod is not None and hasattr(mod, "series"):
            return mod.series(symbol)
    except Exception as e:
        logger.warning("smart_money series lookup failed (%s %s): %s", key, symbol, e)
    return None


def enrich_frames(market: Optional[str], df_by_ticker: dict) -> None:
    """Merge the market's smart-money series onto each daily frame so the follow-the-smart-
    money backtest strategies have data. Mutates in place; no-op where no source. Never raises.
    (Generalizes runner._enrich_with_13f — US delegates to edgar_13f.enrich_frames.)"""
    if not settings.smart_money_enabled:
        return
    key = get_market(market).key
    try:
        if key == "US":
            from pipeline_core.services import edgar_13f
            edgar_13f.enrich_frames(df_by_ticker)
            return
        mod = _native_module(key)
        if mod is not None and hasattr(mod, "enrich_frames"):
            mod.enrich_frames(df_by_ticker)
    except Exception as e:
        logger.warning("smart_money frame enrichment skipped (%s): %s", key, e)


def has_family(market: Optional[str]) -> bool:
    """Whether this market has a smart-money signal family available to confirm ideas — drives
    the confluence denominator in runner.enrich_ideas_live. Equity markets always do (ownership
    snapshot / 13F); price-only markets do once their native source module is switched on and built."""
    if not settings.smart_money_enabled:
        return False
    m = get_market(market)
    if m.has_fundamentals:
        return True
    return _native_module(m.key) is not None
