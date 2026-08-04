"""
Metals universe service — a curated, fixed set of precious/industrial metals
instruments (Yahoo Finance futures + liquid ETFs). No network fetch: the list is
small and stable, so it's hardcoded with display names.

Price-only market: these instruments have OHLCV history (for screening/backtest)
but no fundamentals. markets.normalize_ticker passes symbols through unchanged
(GC=F, GLD, …).
"""

from __future__ import annotations

# symbol → human-readable name. Futures give the purest exposure; ETFs add
# tradable, long-history proxies (and broaden the universe for backtests).
METALS: dict[str, str] = {
    # Continuous futures
    "GC=F": "Gold Futures",
    "SI=F": "Silver Futures",
    "PL=F": "Platinum Futures",
    "PA=F": "Palladium Futures",
    "HG=F": "Copper Futures",
    # ETFs (long, liquid history)
    "GLD": "SPDR Gold Shares",
    "IAU": "iShares Gold Trust",
    "SLV": "iShares Silver Trust",
    "PPLT": "abrdn Platinum ETF",
    "PALL": "abrdn Palladium ETF",
    "CPER": "US Copper Index Fund",
    "GDX": "VanEck Gold Miners ETF",
}

# Only one universe today, but the shape matches the other services.
UNIVERSES = {"Metals": list(METALS.keys())}


def get_universe(name: str = "Metals") -> list[str]:
    """Constituent symbols for a metals universe (only 'Metals' today)."""
    return list(UNIVERSES.get(name, list(METALS.keys())))


def display_name(symbol: str) -> str:
    """Human-readable name for a metals symbol (falls back to the symbol)."""
    return METALS.get(symbol.strip().upper(), symbol)
