"""
Curated screening strategies — battle-tested filter combinations for
Indian equities, exposed to both the dashboard and MCP surfaces.

Each strategy is a named ScreenFilters bundle with a rationale, so users
can fire a full pipeline run in one click or load it as a starting point
for customization. All thresholds are decimals unless noted (same units
as ScreenFilters).
"""

from __future__ import annotations

from typing import Any

# Percent-style UI labels for filter fields — used by the dashboard to
# render human-readable chips. Kept here so both surfaces agree on units.
PCT_FIELDS = {
    "dividend_yield_min", "revenue_growth_min", "profit_growth_min",
    "roe_min", "roce_min", "opm_min", "npm_min",
    "promoter_holding_min", "promoter_pledge_max", "delivery_pct_min",
    "fcf_yield_min", "eps_growth_3y_min", "sales_growth_3y_min",
}

STRATEGIES: list[dict[str, Any]] = [
    {
        "id": "quality-compounders",
        "name": "Quality Compounders",
        "category": "Quality",
        "description": "High-return, financially sound businesses with proven quality scores.",
        "rationale": (
            "ROE/ROCE ≥ 15% with low leverage and a Piotroski F-score of 6+ filters "
            "for companies that compound capital efficiently — the classic "
            "quality-at-fair-price approach for long-term holding."
        ),
        "filters": {
            "index": "NIFTY 200",
            "roe_min": 0.15,
            "roce_min": 0.15,
            "debt_to_equity_max": 0.5,
            "piotroski_score_min": 6,
        },
        "defaults": {"max_candidates": 8, "analysis_depth": "standard"},
    },
    {
        "id": "value-hunters",
        "name": "Value Hunters",
        "category": "Value",
        "description": "Statistically cheap stocks: low P/E, low P/B, with a dividend cushion.",
        "rationale": (
            "Ben Graham-style deep value on the NIFTY 500: P/E ≤ 20, P/B ≤ 3, "
            "positive dividend yield, and manageable debt to avoid value traps."
        ),
        "filters": {
            "index": "NIFTY 500",
            "pe_ratio_max": 20,
            "pb_ratio_max": 3,
            "dividend_yield_min": 0.01,
            "debt_to_equity_max": 1.0,
        },
        "defaults": {"max_candidates": 10, "analysis_depth": "standard"},
    },
    {
        "id": "garp",
        "name": "Growth at a Reasonable Price",
        "category": "Growth",
        "description": "Double-digit growers that aren't priced for perfection (PEG ≤ 1.5).",
        "rationale": (
            "Peter Lynch's GARP: revenue and profit growth above 12% with a PEG "
            "ratio under 1.5 and ROE ≥ 15% — growth you don't overpay for."
        ),
        "filters": {
            "index": "NIFTY 500",
            "peg_ratio_max": 1.5,
            "revenue_growth_min": 0.12,
            "profit_growth_min": 0.12,
            "roe_min": 0.15,
        },
        "defaults": {"max_candidates": 10, "analysis_depth": "standard"},
    },
    {
        "id": "momentum-breakout",
        "name": "Momentum Breakout",
        "category": "Momentum",
        "description": "Uptrending stocks near 52-week highs with volume confirmation.",
        "rationale": (
            "Trend-following: above the 200-DMA, bullish MACD, within 10% of the "
            "52-week high, and ≥1.5× average volume — breakouts with participation."
        ),
        "filters": {
            "index": "NIFTY 500",
            "price_above_200dma": True,
            "macd_bullish": True,
            "near_52w_high_pct": 10,
            "volume_spike_min": 1.5,
        },
        "defaults": {"max_candidates": 10, "analysis_depth": "quick"},
    },
    {
        "id": "oversold-quality",
        "name": "Oversold Quality Reversal",
        "category": "Contrarian",
        "description": "Quality names in long-term uptrends that are short-term oversold.",
        "rationale": (
            "Buy-the-dip with a safety net: RSI ≤ 35 (washed out) but still above "
            "the 200-DMA (trend intact), with ROE ≥ 15% and modest debt."
        ),
        "filters": {
            "index": "NIFTY 500",
            "rsi_max": 35,
            "price_above_200dma": True,
            "roe_min": 0.15,
            "debt_to_equity_max": 0.8,
        },
        "defaults": {"max_candidates": 10, "analysis_depth": "standard"},
    },
    {
        "id": "promoter-confidence",
        "name": "Promoter Confidence",
        "category": "India-specific",
        "description": "High promoter skin-in-the-game with negligible pledging.",
        "rationale": (
            "India-specific governance screen: promoters holding ≥ 50% with ≤ 1% "
            "pledged and healthy delivery percentages — aligned owners, no "
            "margin-call overhang."
        ),
        "filters": {
            "index": "NIFTY 500",
            "promoter_holding_min": 0.5,
            "promoter_pledge_max": 0.01,
            "delivery_pct_min": 0.4,
            "debt_to_equity_max": 0.7,
        },
        "defaults": {"max_candidates": 10, "analysis_depth": "standard"},
    },
    {
        "id": "dividend-stalwarts",
        "name": "Dividend Stalwarts",
        "category": "Income",
        "description": "Reliable payers: 2%+ yield from large, financially steady companies.",
        "rationale": (
            "Income focus: dividend yield ≥ 2%, Piotroski ≥ 5 for stability, "
            "market cap ≥ ₹20,000 Cr, and debt under control — payouts that "
            "are likely to keep arriving."
        ),
        "filters": {
            "index": "NIFTY 500",
            "dividend_yield_min": 0.02,
            "piotroski_score_min": 5,
            "market_cap_min_cr": 20000,
            "debt_to_equity_max": 0.8,
        },
        "defaults": {"max_candidates": 10, "analysis_depth": "standard"},
    },
    {
        "id": "debt-free-cash-machines",
        "name": "Debt-Free Cash Machines",
        "category": "Quality",
        "description": "Near-zero-debt companies generating serious free cash flow.",
        "rationale": (
            "D/E ≤ 0.1 with FCF yield ≥ 4% and ROE ≥ 15% — businesses that fund "
            "themselves and return cash, resilient through rate cycles."
        ),
        "filters": {
            "index": "NIFTY 500",
            "debt_to_equity_max": 0.1,
            "fcf_yield_min": 0.04,
            "roe_min": 0.15,
        },
        "defaults": {"max_candidates": 10, "analysis_depth": "standard"},
    },
    {
        "id": "large-cap-safety",
        "name": "Large-Cap Safety",
        "category": "Defensive",
        "description": "Blue-chip only: NIFTY 50 names with clean balance sheets.",
        "rationale": (
            "Defensive core: NIFTY 50 universe, ₹1 lakh Cr+ market cap, D/E ≤ 0.5, "
            "ROE ≥ 12% and Piotroski ≥ 5 — sleep-well-at-night territory."
        ),
        "filters": {
            "index": "NIFTY 50",
            "market_cap_min_cr": 100000,
            "debt_to_equity_max": 0.5,
            "roe_min": 0.12,
            "piotroski_score_min": 5,
        },
        "defaults": {"max_candidates": 8, "analysis_depth": "standard"},
    },
    {
        "id": "turnaround-watchlist",
        "name": "Turnaround Watchlist",
        "category": "Contrarian",
        "description": "Beaten-down but returning to profit growth — higher risk, higher reward.",
        "rationale": (
            "Speculative: RSI ≤ 40 and P/B ≤ 2 (cheap, unloved) but profit growth "
            "already back above 10% — early signs the ship is turning."
        ),
        "filters": {
            "index": "NIFTY 500",
            "rsi_max": 40,
            "pb_ratio_max": 2,
            "profit_growth_min": 0.10,
        },
        "defaults": {"max_candidates": 10, "analysis_depth": "deep"},
    },
]

# ── US equities (USD; market caps in $B via market_cap_min_cr; no promoter data) ──

US_STRATEGIES: list[dict[str, Any]] = [
    {
        "id": "quality-compounders", "name": "Quality Compounders", "category": "Quality",
        "description": "High-return, financially sound businesses with proven quality scores.",
        "rationale": "ROE/ROCE ≥ 15% with low leverage and a Piotroski F-score of 6+ — capital compounders for long-term holding.",
        "filters": {"index": "NASDAQ-100", "roe_min": 0.15, "roce_min": 0.15, "debt_to_equity_max": 0.5, "piotroski_score_min": 6},
        "defaults": {"max_candidates": 8, "analysis_depth": "standard"},
    },
    {
        "id": "value-hunters", "name": "Value Hunters", "category": "Value",
        "description": "Statistically cheap stocks: low P/E, low P/B, with a dividend cushion.",
        "rationale": "Graham-style value on the S&P 500: P/E ≤ 18, P/B ≤ 3, positive yield, manageable debt to avoid value traps.",
        "filters": {"index": "S&P 500", "pe_ratio_max": 18, "pb_ratio_max": 3, "dividend_yield_min": 0.01, "debt_to_equity_max": 1.0},
        "defaults": {"max_candidates": 10, "analysis_depth": "standard"},
    },
    {
        "id": "garp", "name": "Growth at a Reasonable Price", "category": "Growth",
        "description": "Double-digit growers that aren't priced for perfection (PEG ≤ 1.5).",
        "rationale": "Lynch's GARP: revenue and profit growth above 12% with PEG under 1.5 and ROE ≥ 15% — growth you don't overpay for.",
        "filters": {"index": "S&P 500", "peg_ratio_max": 1.5, "revenue_growth_min": 0.12, "profit_growth_min": 0.12, "roe_min": 0.15},
        "defaults": {"max_candidates": 10, "analysis_depth": "standard"},
    },
    {
        "id": "momentum-breakout", "name": "Momentum Breakout", "category": "Momentum",
        "description": "Uptrending stocks near 52-week highs with volume confirmation.",
        "rationale": "Trend-following: above the 200-DMA, bullish MACD, within 10% of the 52-week high, ≥1.5× average volume.",
        "filters": {"index": "NASDAQ-100", "price_above_200dma": True, "macd_bullish": True, "near_52w_high_pct": 10, "volume_spike_min": 1.5},
        "defaults": {"max_candidates": 10, "analysis_depth": "quick"},
    },
    {
        "id": "oversold-quality", "name": "Oversold Quality Reversal", "category": "Contrarian",
        "description": "Quality names in long-term uptrends that are short-term oversold.",
        "rationale": "Buy-the-dip with a net: RSI ≤ 35 but still above the 200-DMA, ROE ≥ 15% and modest debt.",
        "filters": {"index": "S&P 500", "rsi_max": 35, "price_above_200dma": True, "roe_min": 0.15, "debt_to_equity_max": 0.8},
        "defaults": {"max_candidates": 10, "analysis_depth": "standard"},
    },
    {
        "id": "dividend-stalwarts", "name": "Dividend Stalwarts", "category": "Income",
        "description": "Reliable payers: 2.5%+ yield from large, financially steady companies.",
        "rationale": "Income focus: yield ≥ 2.5%, Piotroski ≥ 5, market cap ≥ $20B, debt under control — durable payouts.",
        "filters": {"index": "S&P 500", "dividend_yield_min": 0.025, "piotroski_score_min": 5, "market_cap_min_cr": 20, "debt_to_equity_max": 0.8},
        "defaults": {"max_candidates": 10, "analysis_depth": "standard"},
    },
    {
        "id": "debt-free-cash-machines", "name": "Debt-Free Cash Machines", "category": "Quality",
        "description": "Near-zero-debt companies generating serious free cash flow.",
        "rationale": "D/E ≤ 0.1 with FCF yield ≥ 4% and ROE ≥ 15% — self-funding businesses resilient through rate cycles.",
        "filters": {"index": "S&P 500", "debt_to_equity_max": 0.1, "fcf_yield_min": 0.04, "roe_min": 0.15},
        "defaults": {"max_candidates": 10, "analysis_depth": "standard"},
    },
    {
        "id": "mega-cap-safety", "name": "Mega-Cap Safety", "category": "Defensive",
        "description": "Blue-chip only: Dow 30 names with clean balance sheets.",
        "rationale": "Defensive core: Dow 30 universe, $100B+ market cap, D/E ≤ 0.5, ROE ≥ 12%, Piotroski ≥ 5.",
        "filters": {"index": "Dow 30", "market_cap_min_cr": 100, "debt_to_equity_max": 0.5, "roe_min": 0.12, "piotroski_score_min": 5},
        "defaults": {"max_candidates": 8, "analysis_depth": "standard"},
    },
]

# ── Price-only markets (metals, crypto) — technical filters only, no fundamentals.
#    No `index` key → the market's default universe is used. ──

PRICE_STRATEGIES: list[dict[str, Any]] = [
    {
        "id": "momentum-breakout", "name": "Momentum Breakout", "category": "Momentum",
        "description": "Uptrending assets near 52-week highs with volume confirmation.",
        "rationale": "Above the 200-DMA, bullish MACD, within 10% of the 52-week high, ≥1.5× average volume.",
        "filters": {"price_above_200dma": True, "macd_bullish": True, "near_52w_high_pct": 10, "volume_spike_min": 1.5},
        "defaults": {"max_candidates": 8, "analysis_depth": "quick"},
    },
    {
        "id": "trend-following", "name": "Trend Following", "category": "Trend",
        "description": "Established uptrends: above both the 50- and 200-day averages with bullish MACD.",
        "rationale": "Classic dual-MA trend filter — ride sustained moves, sit out chop.",
        "filters": {"price_above_50dma": True, "price_above_200dma": True, "macd_bullish": True},
        "defaults": {"max_candidates": 8, "analysis_depth": "quick"},
    },
    {
        "id": "oversold-bounce", "name": "Oversold Bounce", "category": "Contrarian",
        "description": "Short-term oversold assets still in a longer-term uptrend.",
        "rationale": "RSI ≤ 35 (washed out) but price above the 200-DMA (trend intact) — mean-reversion with a filter.",
        "filters": {"rsi_max": 35, "price_above_200dma": True},
        "defaults": {"max_candidates": 8, "analysis_depth": "standard"},
    },
    {
        "id": "high-momentum", "name": "High Momentum", "category": "Momentum",
        "description": "Assets pressing new highs on rising volume.",
        "rationale": "Within 5% of the 52-week high with above-average volume — strongest relative strength.",
        "filters": {"near_52w_high_pct": 5, "volume_spike_min": 1.2},
        "defaults": {"max_candidates": 8, "analysis_depth": "quick"},
    },
]

# STRATEGIES stays the IN list for back-compat (portfolio construct + existing imports).
STRATEGIES_BY_MARKET: dict[str, list[dict[str, Any]]] = {
    "IN": STRATEGIES,
    "US": US_STRATEGIES,
    "METALS": PRICE_STRATEGIES,
    "CRYPTO": PRICE_STRATEGIES,
}


def _market_strategies(market: str) -> list[dict[str, Any]]:
    return STRATEGIES_BY_MARKET.get((market or "IN").upper(), STRATEGIES)


def list_strategies(market: str = "IN") -> list[dict[str, Any]]:
    """Curated strategies for a market (definitions are immutable; callers get copies)."""
    return [dict(s, filters=dict(s["filters"]), defaults=dict(s["defaults"])) for s in _market_strategies(market)]


def get_strategy(strategy_id_or_name: str, market: str = "IN") -> dict[str, Any] | None:
    """Look up one strategy by id or exact name (case-insensitive) within a market.
    Falls back to scanning every market so a bare id still resolves."""
    key = strategy_id_or_name.strip().lower()
    pools = [_market_strategies(market)] + list(STRATEGIES_BY_MARKET.values())
    for pool in pools:
        for s in pool:
            if s["id"] == key or s["name"].lower() == key:
                return dict(s, filters=dict(s["filters"]), defaults=dict(s["defaults"]))
    return None
