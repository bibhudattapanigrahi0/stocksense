"""
Market registry — the single source of truth for asset-class differences.

StockSense started as an India-only (NSE/BSE) tool. This module generalizes it to
multiple markets (India equities, US equities, metals, crypto) by pushing every
asset-class-specific value — ticker suffix, benchmark, currency/units, universe
source, whether fundamentals exist — into one `Market` config object. Every other
module replaces a hardcoded India constant with a lookup on the selected market.

Two market *kinds*:
  - "equity"     — full fundamentals (P/E, ROE, statements) → 4-agent composite.
  - "price_only" — no fundamentals (metals, crypto) → technical/momentum composite.

The universe provider dispatch (`get_universe_tickers`) uses lazy imports, so a
market lights up as its data service module lands; only IN is required at import time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Market:
    """Everything that differs between asset classes."""

    key: str                       # "IN" | "US" | "METALS" | "CRYPTO"
    label: str                     # human name for the switcher
    kind: str                      # "equity" | "price_only"
    currency: str                  # "INR" | "USD"
    benchmark: str                 # yfinance benchmark symbol for alpha/beta
    benchmark_label: str           # UI label ("NIFTY 50", "S&P 500", "Gold", "Bitcoin")
    default_universe: str          # default universe name
    universes: list[str]           # selectable universe names (UI dropdown)
    cap_tiers: dict[str, str]      # portfolio risk appetite → universe name
    mcap_divisor: Optional[float] = None  # raw market cap → display unit (None = no mcap)
    mcap_unit: str = ""            # "Cr" | "B" | "" (empty ⇒ hide market cap)
    enrich: bool = False           # NSE promoter/pledge/delivery enrichment (IN only)
    vol_symbol: Optional[str] = None    # regime gauge: volatility index (^VIX, ^INDIAVIX); None = none
    macro_symbol: Optional[str] = None  # regime gauge: macro driver (DXY for metals, inverted); None = none
    # Composite weights for price_only markets: technical/momentum/risk/sentiment.
    # None for equity markets — they use the fundamental-weighted settings weights.
    price_weights: Optional[dict[str, float]] = None

    @property
    def has_fundamentals(self) -> bool:
        return self.kind == "equity"

    def to_public(self) -> dict:
        """JSON-safe view for the /api/v1/markets endpoint (drives the frontend)."""
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "currency": self.currency,
            "benchmark_label": self.benchmark_label,
            "default_universe": self.default_universe,
            "universes": list(self.universes),
            "mcap_unit": self.mcap_unit,
            "has_fundamentals": self.has_fundamentals,
            "enrich": self.enrich,
        }


# ── Registry ────────────────────────────────────────────────────────
# Data-only — safe to define for all markets even before their service modules
# exist; functionality lights up per phase via the lazy dispatch below.

MARKETS: dict[str, Market] = {
    "IN": Market(
        key="IN",
        label="India (NSE)",
        kind="equity",
        currency="INR",
        benchmark="^NSEI",
        benchmark_label="NIFTY 50",
        vol_symbol="^INDIAVIX",
        default_universe="NIFTY 50",
        universes=[
            "NIFTY 50", "NIFTY 100", "NIFTY 200", "NIFTY 500", "NIFTY NEXT 50",
            "NIFTY BANK", "NIFTY IT", "NIFTY MIDCAP 150", "NIFTY SMALLCAP 250",
        ],
        cap_tiers={"conservative": "NIFTY 100", "balanced": "NIFTY 200", "aggressive": "NIFTY 500"},
        mcap_divisor=1e7,   # rupees → crore
        mcap_unit="Cr",
        enrich=True,
    ),
    "US": Market(
        key="US",
        label="US",
        kind="equity",
        currency="USD",
        benchmark="^GSPC",
        benchmark_label="S&P 500",
        vol_symbol="^VIX",
        default_universe="S&P 500",
        universes=["S&P 500", "NASDAQ-100", "Dow 30"],
        cap_tiers={"conservative": "Dow 30", "balanced": "NASDAQ-100", "aggressive": "S&P 500"},
        mcap_divisor=1e9,   # dollars → billions
        mcap_unit="B",
        enrich=False,
    ),
    "METALS": Market(
        key="METALS",
        label="Metals",
        kind="price_only",
        currency="USD",
        benchmark="GC=F",
        benchmark_label="Gold",
        macro_symbol="DX-Y.NYB",   # US Dollar Index — up = headwind for metals (inverse)
        default_universe="Metals",
        universes=["Metals"],
        cap_tiers={"conservative": "Metals", "balanced": "Metals", "aggressive": "Metals"},
        mcap_divisor=None,  # futures/ETFs have no market cap
        mcap_unit="",
        enrich=False,
        price_weights={"technical": 0.45, "momentum": 0.30, "risk": 0.15, "sentiment": 0.10},
    ),
    "CRYPTO": Market(
        key="CRYPTO",
        label="Crypto",
        kind="price_only",
        currency="USD",
        benchmark="BTC-USD",
        benchmark_label="Bitcoin",
        default_universe="Top 100",
        universes=["Top 50", "Top 100"],
        cap_tiers={"conservative": "Top 50", "balanced": "Top 100", "aggressive": "Top 100"},
        mcap_divisor=1e9,   # dollars → billions (from CoinGecko)
        mcap_unit="B",
        enrich=False,
        price_weights={"technical": 0.40, "momentum": 0.35, "risk": 0.15, "sentiment": 0.10},
    ),
}

DEFAULT_MARKET = "IN"


def get_market(market: Optional[str]) -> Market:
    """Resolve a market key (case-insensitive) → Market, defaulting to IN."""
    key = (market or DEFAULT_MARKET).upper().strip()
    return MARKETS.get(key, MARKETS[DEFAULT_MARKET])


def list_markets() -> list[dict]:
    """Public registry view for the frontend switcher."""
    return [m.to_public() for m in MARKETS.values()]


# ── Ticker normalization (replaces market_data._ns_ticker) ──────────


def normalize_ticker(symbol: str, market: Optional[str] = None) -> str:
    """
    Convert a display symbol to the yfinance ticker for the given market.

      IN     RELIANCE → RELIANCE.NS   (^NSEI, *.BO pass through)
      US     AAPL → AAPL, BRK.B → BRK-B   (^GSPC passes through)
      METALS GC=F → GC=F, GLD → GLD   (pass through as-is)
      CRYPTO BTC → BTC-USD   (already-suffixed / ^ indices pass through)
    """
    m = get_market(market)
    sym = symbol.strip().upper()
    if sym.startswith("^"):
        return sym

    if m.key == "IN":
        if not sym.endswith(".NS") and not sym.endswith(".BO"):
            return f"{sym}.NS"
        return sym

    if m.key == "US":
        # yfinance uses a dash for class shares (BRK.B → BRK-B), never a dot.
        return sym.replace(".", "-")

    if m.key == "CRYPTO":
        if sym.endswith("-USD"):
            return sym
        return f"{sym}-USD"

    # METALS and any other pass-through market
    return sym


def display_ticker(symbol: str) -> str:
    """Strip market suffixes back to the display symbol (RELIANCE.NS → RELIANCE)."""
    sym = symbol.strip().upper()
    for suffix in (".NS", ".BO", "-USD"):
        if sym.endswith(suffix):
            return sym[: -len(suffix)]
    return sym


# ── Universe dispatch (lazy imports — one per data service) ─────────


def get_universe_tickers(market: Optional[str], name: Optional[str] = None) -> list[str]:
    """Constituent display-symbols for (market, universe name)."""
    m = get_market(market)
    universe = name or m.default_universe

    if m.key == "IN":
        from pipeline_core.services import nse_data
        return nse_data.get_index_constituents(universe)
    if m.key == "US":
        from pipeline_core.services import us_data
        return us_data.get_index_constituents(universe)
    if m.key == "METALS":
        from pipeline_core.services import metals_data
        return metals_data.get_universe(universe)
    if m.key == "CRYPTO":
        from pipeline_core.services import crypto_data
        return crypto_data.get_universe(universe)
    raise ValueError(f"Unknown market: {m.key}")
