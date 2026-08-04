"""
Screener Agent — applies multi-dimensional filters to Indian stocks.

Filters are declared in a staged registry instead of an ad-hoc lambda list:
  - "info":      cheap, evaluated on the profile dict (full universe)
  - "nse":       needs NSE API data (promoter/pledge/delivery) — survivors only
  - "technical": needs price history — survivors only
  - "quality":   needs financial statements — survivors only

Staging bounds expensive calls: NSE enrichment, price history, and statements
are fetched for at most max_candidates × 3 stocks, never the whole universe.

Missing data makes a filter "indeterminate" — excluded from the match count
rather than counted against the stock (unverifiable ≠ failed).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from pipeline_core.agents import indicators
from pipeline_core.agents.context import StockContext
from pipeline_core.config import settings
from pipeline_core.markets import get_market
from pipeline_core.models.schemas import FilterExplanation, ScreenFilters, StockScore
from pipeline_core.services import market_data, nse_data

logger = logging.getLogger(__name__)

# Returns True/False, or None when the required data is missing (indeterminate).
CheckFn = Callable[[dict, Any], Optional[bool]]
ActualFn = Callable[[dict], Any]


@dataclass(frozen=True)
class FilterSpec:
    """One declarative filter bound to a ScreenFilters field."""

    field: str
    stage: str  # "info" | "nse" | "technical" | "quality"
    description: str
    check: CheckFn          # (data_dict, threshold) -> True/False/None
    actual: ActualFn        # (data_dict) -> value used in the comparison


def _cmp(key: str, op: Callable[[float, float], bool], scale: float = 1.0) -> CheckFn:
    """Build a comparison check that is indeterminate when the key is missing."""
    def check(data: dict, threshold: Any) -> Optional[bool]:
        value = data.get(key)
        if value is None:
            return None
        try:
            return op(float(value), float(threshold) * scale)
        except (TypeError, ValueError):
            return None
    return check


def _flag(key: str) -> CheckFn:
    """Boolean check (e.g. price_above_200dma) — indeterminate when missing."""
    def check(data: dict, threshold: Any) -> Optional[bool]:
        value = data.get(key)
        if value is None:
            return None
        return bool(value) == bool(threshold)
    return check


def _pledge_check(data: dict, threshold: Any) -> Optional[bool]:
    """Promoter pledge: no disclosed pledge passes; a value must be ≤ max."""
    value = data.get("promoter_pledge")
    if value is None:
        return True  # NSE reports nothing → no pledge on record (agy semantics)
    return float(value) <= float(threshold) * 100


def _contains(key: str) -> CheckFn:
    def check(data: dict, threshold: Any) -> Optional[bool]:
        value = data.get(key)
        if value is None:
            return None
        return str(threshold).lower() in str(value).lower()
    return check


# ── Registry ───────────────────────────────────────────────────────
# Units: yfinance ratios (roe, margins, growth, dividend_yield) are decimals;
# NSE holding/delivery figures are percents (thresholds × 100);
# yfinance debt_to_equity is percent-of-equity (150 = 1.5x).
# ScreenFilters thresholds are decimals unless the name says pct/percent.

FILTER_REGISTRY: list[FilterSpec] = [
    # Valuation
    FilterSpec("pe_ratio_min", "info", "P/E ≥ min", _cmp("pe_ratio", lambda v, t: v >= t), lambda s: s.get("pe_ratio")),
    FilterSpec("pe_ratio_max", "info", "P/E ≤ max", _cmp("pe_ratio", lambda v, t: v <= t), lambda s: s.get("pe_ratio")),
    FilterSpec("pb_ratio_max", "info", "P/B ≤ max", _cmp("pb_ratio", lambda v, t: v <= t), lambda s: s.get("pb_ratio")),
    FilterSpec("ev_ebitda_max", "info", "EV/EBITDA ≤ max", _cmp("ev_ebitda", lambda v, t: v <= t), lambda s: s.get("ev_ebitda")),
    FilterSpec("peg_ratio_max", "info", "PEG ≤ max", _cmp("peg_ratio", lambda v, t: v <= t), lambda s: s.get("peg_ratio")),
    FilterSpec("dividend_yield_min", "info", "Dividend yield ≥ min", _cmp("dividend_yield", lambda v, t: v >= t), lambda s: s.get("dividend_yield")),

    # Growth
    FilterSpec("revenue_growth_min", "info", "Revenue growth ≥ min", _cmp("revenue_growth", lambda v, t: v >= t), lambda s: s.get("revenue_growth")),
    FilterSpec("profit_growth_min", "info", "Profit growth ≥ min", _cmp("earnings_growth", lambda v, t: v >= t), lambda s: s.get("earnings_growth")),

    # Profitability
    FilterSpec("roe_min", "info", "ROE ≥ min", _cmp("roe", lambda v, t: v >= t), lambda s: s.get("roe")),
    FilterSpec("roce_min", "quality", "ROCE ≥ min", _cmp("roce", lambda v, t: v >= t), lambda s: s.get("roce")),
    FilterSpec("opm_min", "info", "OPM ≥ min", _cmp("operating_margins", lambda v, t: v >= t), lambda s: s.get("operating_margins")),
    FilterSpec("npm_min", "info", "NPM ≥ min", _cmp("profit_margins", lambda v, t: v >= t), lambda s: s.get("profit_margins")),

    # India-specific — NSE stage (bounded to survivors; thresholds × 100)
    FilterSpec("promoter_holding_min", "nse", "Promoter holding ≥ min",
               _cmp("promoter_holding", lambda v, t: v >= t, scale=100), lambda s: s.get("promoter_holding")),
    FilterSpec("promoter_pledge_max", "nse", "Promoter pledge ≤ max", _pledge_check, lambda s: s.get("promoter_pledge")),
    FilterSpec("delivery_pct_min", "nse", "Delivery % ≥ min",
               _cmp("delivery_pct", lambda v, t: v >= t, scale=100), lambda s: s.get("delivery_pct")),

    # Financial health
    FilterSpec("debt_to_equity_max", "info", "D/E ≤ max",
               _cmp("debt_to_equity", lambda v, t: v / 100 <= t),
               lambda s: round(s["debt_to_equity"] / 100, 2) if s.get("debt_to_equity") is not None else None),
    FilterSpec("fcf_yield_min", "info", "FCF yield ≥ min", _cmp("fcf_yield", lambda v, t: v >= t), lambda s: s.get("fcf_yield")),

    # Market cap (INR crore)
    FilterSpec("market_cap_min_cr", "info", "Market cap ≥ min Cr", _cmp("market_cap_cr", lambda v, t: v >= t), lambda s: s.get("market_cap_cr")),
    FilterSpec("market_cap_max_cr", "info", "Market cap ≤ max Cr", _cmp("market_cap_cr", lambda v, t: v <= t), lambda s: s.get("market_cap_cr")),

    # Sector / industry
    FilterSpec("sector", "info", "Sector match", _contains("sector"), lambda s: s.get("sector")),
    FilterSpec("industry", "info", "Industry match", _contains("industry"), lambda s: s.get("industry")),

    # ── Technical stage (data dict from price history) ──
    FilterSpec("rsi_min", "technical", "RSI ≥ min", _cmp("rsi", lambda v, t: v >= t), lambda s: s.get("rsi")),
    FilterSpec("rsi_max", "technical", "RSI ≤ max", _cmp("rsi", lambda v, t: v <= t), lambda s: s.get("rsi")),
    FilterSpec("price_above_200dma", "technical", "Price above 200-DMA", _flag("above_200dma"), lambda s: s.get("above_200dma")),
    FilterSpec("price_above_50dma", "technical", "Price above 50-DMA", _flag("above_50dma"), lambda s: s.get("above_50dma")),
    FilterSpec("macd_bullish", "technical", "MACD bullish", _flag("macd_bullish"), lambda s: s.get("macd_bullish")),
    FilterSpec("near_52w_high_pct", "technical", "Within X% of 52w high",
               _cmp("pct_from_52w_high", lambda v, t: abs(v) <= t), lambda s: s.get("pct_from_52w_high")),
    FilterSpec("near_52w_low_pct", "technical", "Within X% of 52w low",
               _cmp("pct_from_52w_low", lambda v, t: abs(v) <= t), lambda s: s.get("pct_from_52w_low")),
    FilterSpec("volume_spike_min", "technical", "Volume spike ≥ min×", _cmp("volume_spike", lambda v, t: v >= t), lambda s: s.get("volume_spike")),

    # ── Quality stage (data dict from financial statements) ──
    FilterSpec("piotroski_score_min", "quality", "Piotroski F ≥ min", _cmp("piotroski_score", lambda v, t: v >= t), lambda s: s.get("piotroski_score")),
    FilterSpec("interest_coverage_min", "quality", "Interest coverage ≥ min", _cmp("interest_coverage", lambda v, t: v >= t), lambda s: s.get("interest_coverage")),
    FilterSpec("eps_growth_3y_min", "quality", "EPS 3y CAGR ≥ min", _cmp("eps_growth_3y", lambda v, t: v >= t), lambda s: s.get("eps_growth_3y")),
    FilterSpec("sales_growth_3y_min", "quality", "Sales 3y CAGR ≥ min", _cmp("sales_growth_3y", lambda v, t: v >= t), lambda s: s.get("sales_growth_3y")),
]

_BY_FIELD = {spec.field: spec for spec in FILTER_REGISTRY}

# Schema fields with no data source yet — logged once per screen, not silently dropped.
UNSUPPORTED_FIELDS = {"fii_holding_change_positive", "dii_holding_change_positive"}

# Fields that make sense for price-only markets (metals, crypto) — no fundamentals.
# Everything else (valuation/growth/profitability/quality/NSE) is dropped for them.
PRICE_ONLY_FIELDS = {
    "rsi_min", "rsi_max", "price_above_200dma", "price_above_50dma", "macd_bullish",
    "near_52w_high_pct", "near_52w_low_pct", "volume_spike_min",
    "market_cap_min_cr", "market_cap_max_cr",
}

_DEEP_STAGES = ("nse", "technical", "quality")


def with_derived_metrics(stock: dict) -> dict:
    """Add derived info-stage metrics (FCF yield) to a profile dict."""
    fcf, mcap = stock.get("free_cash_flow"), stock.get("market_cap")
    if fcf is not None and mcap:
        stock["fcf_yield"] = fcf / mcap
    return stock


class ScreenerAgent:
    """Applies structured filters and returns ranked, explained candidates."""

    def __init__(self):
        self.name = "Screener Agent"

    def active_specs(self, filters: ScreenFilters, market: str = "IN") -> list[FilterSpec]:
        m = get_market(market)
        set_fields = set(filters.model_dump(exclude_unset=True))
        for unsupported in UNSUPPORTED_FIELDS & set_fields:
            logger.warning(f"[{self.name}] Filter '{unsupported}' has no data source yet — skipping")
        specs = [
            _BY_FIELD[f]
            for f in set_fields
            if f in _BY_FIELD and getattr(filters, f) is not None
        ]
        # Price-only markets (metals/crypto): keep only technical/price filters.
        if not m.has_fundamentals:
            specs = [s for s in specs if s.field in PRICE_ONLY_FIELDS]
        # NSE promoter/pledge/delivery filters apply only to India.
        if not m.enrich:
            specs = [s for s in specs if s.stage != "nse"]
        return specs

    async def screen(
        self,
        stocks: list[dict],
        filters: ScreenFilters,
        max_candidates: int = 20,
        context_builder=None,
        market: str = "IN",
    ) -> list[StockScore]:
        """
        Screen stocks against all active filters, staged cheap→expensive.

        context_builder: optional async callable(ticker) -> StockContext used
        to source NSE data / price history / financials for the deep stages.
        When None, data is fetched through the services layer directly.
        market selects the asset class (gates NSE + fundamental filters, and
        scopes deep-stage price/statement fetches).
        """
        specs = self.active_specs(filters, market)
        info_specs = [s for s in specs if s.stage == "info"]
        deep_specs = [s for s in specs if s.stage in _DEEP_STAGES]

        logger.info(
            f"[{self.name}] Screening {len(stocks)} stocks "
            f"({len(info_specs)} info filters, {len(deep_specs)} deep filters)"
        )

        if not specs:
            logger.warning(f"[{self.name}] No active filters — returning first {max_candidates} stocks")
            return [self._stock_to_score(s, []) for s in stocks[:max_candidates]]

        # Stage 1: info filters across the whole universe
        survivors: list[tuple[dict, list[FilterExplanation]]] = []
        for stock in stocks:
            with_derived_metrics(stock)
            explanations = [self._evaluate(spec, stock, filters) for spec in info_specs]
            if self._keeps(explanations):
                survivors.append((stock, explanations))

        # Rank by info-stage match rate before paying for the deep stages
        survivors.sort(key=lambda item: self._match_pct(item[1]), reverse=True)
        survivors = survivors[: max_candidates * 3 if deep_specs else max_candidates]

        # Stages 2–4: NSE / technical / quality filters on survivors.
        # Deep-stage data (price history / statements / NSE) is fetched per survivor
        # with bounded concurrency — the sequential version dominated cold-cache latency.
        if not deep_specs:
            results = [self._stock_to_score(s, e) for s, e in survivors if self._keeps(e)]
        else:
            sem = asyncio.Semaphore(settings.analysis_concurrency)

            async def evaluate_deep(stock: dict, explanations: list[FilterExplanation]):
                async with sem:
                    deep_data = await self._deep_data(stock, deep_specs, context_builder, market)
                explanations = explanations + [
                    self._evaluate(spec, deep_data, filters) for spec in deep_specs
                ]
                # NSE enrichment may have populated display metrics
                stock.update({k: v for k, v in deep_data.items() if k in (
                    "promoter_holding", "promoter_pledge", "fii_holding", "dii_holding",
                    "delivery_pct", "roce", "rsi",
                )})
                return stock, explanations

            evaluated = await asyncio.gather(*(evaluate_deep(s, e) for s, e in survivors))
            results = [self._stock_to_score(s, e) for s, e in evaluated if self._keeps(e)]

        results.sort(key=lambda s: s.filter_match_pct, reverse=True)
        final = results[:max_candidates]
        logger.info(f"[{self.name}] {len(results)} matches → returning top {len(final)}")
        return final

    # ── Internals ──────────────────────────────────────────────────

    @staticmethod
    def _keeps(explanations: list[FilterExplanation]) -> bool:
        """Keep stocks that pass at least one determinate filter (or had none)."""
        determinate = [e for e in explanations if not e.indeterminate]
        if not determinate:
            return not explanations  # nothing evaluated → keep only when no filters at all
        return any(e.passed for e in determinate)

    def _evaluate(self, spec: FilterSpec, data: dict, filters: ScreenFilters) -> FilterExplanation:
        threshold = getattr(filters, spec.field)
        try:
            outcome = spec.check(data, threshold)
        except Exception:
            outcome = None
        return FilterExplanation(
            filter_name=spec.field,
            passed=bool(outcome),
            actual_value=self._safe_actual(spec, data),
            indeterminate=outcome is None,
        )

    @staticmethod
    def _safe_actual(spec: FilterSpec, data: dict):
        try:
            value = spec.actual(data)
            if isinstance(value, float):
                return round(value, 4)
            return value
        except Exception:
            return None

    @staticmethod
    def _match_pct(explanations: list[FilterExplanation]) -> float:
        determinate = [e for e in explanations if not e.indeterminate]
        if not determinate:
            return 0.0
        return sum(1 for e in determinate if e.passed) / len(determinate) * 100

    async def _deep_data(
        self,
        stock: dict,
        deep_specs: list[FilterSpec],
        context_builder,
        market: str = "IN",
    ) -> dict:
        """Assemble the merged NSE + technical + quality data dict for one stock."""
        ticker = stock["ticker"]
        data: dict = {}
        stages = {s.stage for s in deep_specs}

        ctx: Optional[StockContext] = None
        if context_builder is not None:
            try:
                ctx = await context_builder(ticker)
            except Exception as e:
                logger.error(f"[{self.name}] context build failed for {ticker}: {e}")

        if "nse" in stages:
            if ctx is not None:
                info = ctx.info
            else:
                info = dict(stock)
                info = await nse_data.aenrich_with_nse_data(info)
            data.update({k: info.get(k) for k in (
                "promoter_holding", "promoter_pledge", "fii_holding", "dii_holding", "delivery_pct",
            )})

        if "technical" in stages:
            df = ctx.price_history if ctx is not None else await market_data.aget_price_history(ticker, market=market)
            tech = self.compute_technicals(df)
            if tech:
                data.update(tech)

        if "quality" in stages:
            financials = ctx.financials if ctx is not None else await market_data.aget_financials(ticker, market=market)
            from pipeline_core.agents.fundamental import compute_quality_metrics

            data.update(compute_quality_metrics(financials))

        return data

    @staticmethod
    def compute_technicals(df) -> Optional[dict]:
        """Basic technical indicators for screening (RSI, DMAs, MACD, 52w, volume)."""
        if df is None or df.empty or len(df) < 20:
            return None

        close = df["Close"]
        current_price = float(close.iloc[-1])
        result: dict = {}

        rsi_series = indicators.rsi(close, length=14)
        last_rsi = rsi_series.iloc[-1]
        if last_rsi == last_rsi:  # not NaN
            result["rsi"] = round(float(last_rsi), 2)

        sma_50 = indicators.sma(close, 50)
        sma_200 = indicators.sma(close, 200)
        if sma_50.iloc[-1] == sma_50.iloc[-1]:
            result["sma_50"] = float(sma_50.iloc[-1])
            result["above_50dma"] = current_price > float(sma_50.iloc[-1])
        if sma_200.iloc[-1] == sma_200.iloc[-1]:
            result["sma_200"] = float(sma_200.iloc[-1])
            result["above_200dma"] = current_price > float(sma_200.iloc[-1])

        macd_df = indicators.macd(close)
        result["macd_bullish"] = bool(macd_df["macd"].iloc[-1] > macd_df["signal"].iloc[-1])

        window = df.tail(252) if len(df) >= 252 else df
        high_52w = float(window["High"].max())
        low_52w = float(window["Low"].min())
        result["pct_from_52w_high"] = round((current_price - high_52w) / high_52w * 100, 2)
        result["pct_from_52w_low"] = round((current_price - low_52w) / low_52w * 100, 2)

        avg_vol = float(df["Volume"].tail(20).mean())
        current_vol = float(df["Volume"].iloc[-1])
        result["volume_spike"] = round(current_vol / avg_vol, 2) if avg_vol > 0 else 1.0

        return result

    def _stock_to_score(self, stock: dict, explanations: list[FilterExplanation]) -> StockScore:
        determinate = [e for e in explanations if not e.indeterminate]
        matched = sum(1 for e in determinate if e.passed)
        total = len(determinate)
        return StockScore(
            ticker=stock.get("ticker", ""),
            name=stock.get("name", ""),
            sector=stock.get("sector"),
            industry=stock.get("industry"),
            market_cap_cr=stock.get("market_cap_cr"),
            price=stock.get("price"),
            change_pct=stock.get("change_pct"),
            filters_matched=matched,
            filters_total=total,
            filter_match_pct=round(matched / total * 100, 1) if total > 0 else 0,
            filter_explanations=explanations,
            pe_ratio=stock.get("pe_ratio"),
            pb_ratio=stock.get("pb_ratio"),
            roe=round(stock["roe"] * 100, 2) if stock.get("roe") is not None else None,
            # Real ROCE when the quality stage computed it; None otherwise (never a ROE copy)
            roce=round(stock["roce"] * 100, 2) if stock.get("roce") is not None else None,
            debt_to_equity=round(stock["debt_to_equity"] / 100, 2) if stock.get("debt_to_equity") is not None else None,
            promoter_holding=stock.get("promoter_holding"),
            promoter_pledge=stock.get("promoter_pledge"),
            dividend_yield=round(stock["dividend_yield"] * 100, 2) if stock.get("dividend_yield") is not None else None,
            rsi=stock.get("rsi"),
        )
