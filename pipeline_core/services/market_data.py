"""
Market data service — wraps yfinance for Indian stocks (NSE/BSE).
Handles .NS suffix, caching, async access, and batched ingestion.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import diskcache
import pandas as pd
import yfinance as yf

from pipeline_core.config import CACHE_DIR, settings
from pipeline_core.markets import display_ticker, get_market, normalize_ticker
from pipeline_core.utils import deep_sanitize, round_price

logger = logging.getLogger(__name__)
cache = diskcache.Cache(str(CACHE_DIR / "market_data"))


# ── Index Constituents (fallback lists; dynamic fetch lives in nse_data) ──

NIFTY_50_TICKERS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR",
    "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK", "LT", "AXISBANK",
    "BAJFINANCE", "ASIANPAINT", "MARUTI", "HCLTECH", "WIPRO", "SUNPHARMA",
    "TATAMOTORS", "TITAN", "ULTRACEMCO", "NESTLEIND", "POWERGRID",
    "NTPC", "BAJAJFINSV", "TECHM", "TATASTEEL", "M&M", "ONGC",
    "ADANIPORTS", "JSWSTEEL", "DIVISLAB", "DRREDDY", "HINDALCO",
    "COALINDIA", "GRASIM", "CIPLA", "EICHERMOT", "BPCL", "APOLLOHOSP",
    "SBILIFE", "TRENT", "HEROMOTOCO", "BRITANNIA", "BAJAJ-AUTO",
    "INDUSINDBK", "HDFCLIFE", "ADANIENT", "SHRIRAMFIN", "BEL",
]


def _ns_ticker(symbol: str, market: str = "IN") -> str:
    """Back-compat shim → markets.normalize_ticker. Defaults to the IN (.NS) behavior."""
    return normalize_ticker(symbol, market)


# yfinance period string → an approximate day count for the Binance OHLCV fetch.
_PERIOD_DAYS = {
    "1d": 2, "5d": 7, "1mo": 31, "3mo": 93, "6mo": 186, "ytd": 366,
    "1y": 366, "2y": 731, "5y": 1827, "10y": 3660, "max": 3660,
}


def _period_to_days(period: str) -> int:
    return _PERIOD_DAYS.get(period, 366)


def _crypto_info(symbol: str, m) -> Optional[dict]:
    """Build a crypto profile dict from the CoinGecko snapshot (None if the coin
    isn't in it — caller falls back to yfinance). Fundamentals are absent by design."""
    from pipeline_core.services import crypto_data

    prof = crypto_data.get_profile(symbol)
    if prof is None:
        return None
    raw_mcap = prof.get("market_cap")
    result = {
        "ticker": display_ticker(symbol),
        "market": m.key,
        "name": prof.get("name") or display_ticker(symbol),
        "sector": "Cryptocurrency",
        "industry": None,
        "exchange": "Crypto",
        "market_cap": raw_mcap,
        "market_cap_cr": round(raw_mcap / m.mcap_divisor, 2) if raw_mcap and m.mcap_divisor else None,
        "price": prof.get("price"),
        "previous_close": None,
        "change_pct": prof.get("change_pct"),
        "day_high": prof.get("day_high"),
        "day_low": prof.get("day_low"),
        "week_52_high": None,   # the technical stage derives 52w range from price history
        "week_52_low": None,
        "volume": prof.get("volume"),
        "avg_volume": None,
    }
    return deep_sanitize(result)


# ── Synchronous fetchers (cached) ──────────────────────────────────


def get_stock_info(symbol: str, market: str = "IN") -> dict:
    """Basic stock info (name, sector, market cap, ratios…). Cached 1 day.

    Market cap is stored under the legacy `market_cap_cr` key but scaled to the
    market's display unit (INR crore for IN, USD billions for US/crypto, absent
    for metals). The frontend labels the unit per market.
    """
    m = get_market(market)
    cache_key = f"info:{m.key}:{symbol}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    # Crypto profiles come from CoinGecko (name, live price, market cap) — richer and
    # more reliable than yfinance for coins. Falls through to yfinance if not found.
    if m.key == "CRYPTO":
        crypto_result = _crypto_info(symbol, m)
        if crypto_result is not None:
            cache.set(cache_key, crypto_result, expire=settings.cache_ttl_price)
            return crypto_result

    try:
        ticker = yf.Ticker(normalize_ticker(symbol, m.key))
        info = ticker.info
        raw_mcap = info.get("marketCap")
        mcap_display = (
            round(raw_mcap / m.mcap_divisor, 2)
            if raw_mcap and m.mcap_divisor else None
        )
        result = {
            "ticker": display_ticker(symbol),
            "market": m.key,
            "name": info.get("longName") or info.get("shortName", symbol),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "exchange": info.get("exchange"),
            "market_cap": raw_mcap,
            "market_cap_cr": mcap_display,
            "shares_outstanding": info.get("sharesOutstanding"),
            "price": info.get("currentPrice") or info.get("regularMarketPrice"),
            "previous_close": info.get("previousClose"),
            "change_pct": info.get("regularMarketChangePercent"),
            "day_high": info.get("dayHigh"),
            "day_low": info.get("dayLow"),
            "week_52_high": info.get("fiftyTwoWeekHigh"),
            "week_52_low": info.get("fiftyTwoWeekLow"),
            "volume": info.get("volume"),
            "avg_volume": info.get("averageVolume"),
            "pe_ratio": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "pb_ratio": info.get("priceToBook"),
            "ev_ebitda": info.get("enterpriseToEbitda"),
            "peg_ratio": info.get("trailingPegRatio"),
            "dividend_yield": info.get("dividendYield"),
            "eps": info.get("trailingEps"),
            "book_value": info.get("bookValue"),
            "roe": info.get("returnOnEquity"),
            "debt_to_equity": info.get("debtToEquity"),
            "free_cash_flow": info.get("freeCashflow"),
            "revenue_growth": info.get("revenueGrowth"),
            "earnings_growth": info.get("earningsGrowth"),
            "profit_margins": info.get("profitMargins"),
            "operating_margins": info.get("operatingMargins"),
            "gross_margins": info.get("grossMargins"),
            "ebitda": info.get("ebitda"),
            "total_debt": info.get("totalDebt"),
            "total_cash": info.get("totalCash"),
            "interest_coverage": None,  # derived in fundamental agent from statements
            # Ownership snapshot — point-in-time only (yfinance has no history), so it's a
            # live confirmation overlay, never a backtest input (see ENHANCEMENTS.md §2.1).
            "held_pct_institutions": info.get("heldPercentInstitutions"),
            "held_pct_insiders": info.get("heldPercentInsiders"),
        }
        # yfinance leaves NaN cells for missing ratios — strip them before
        # they can poison filters, scoring, or JSON responses.
        result = deep_sanitize(result)
        cache.set(cache_key, result, expire=settings.cache_ttl_fundamentals)
        return result
    except Exception as e:
        logger.error(f"Failed to fetch info for {symbol}: {e}")
        return {"ticker": symbol, "name": symbol, "error": str(e)}


def _as_pct(fraction) -> Optional[float]:
    """yfinance ownership comes as a fraction (0.72). Return a display percentage,
    or None if absent/non-numeric. Never raises."""
    try:
        return round(float(fraction) * 100, 1) if fraction is not None else None
    except (TypeError, ValueError):
        return None


def _edgar_ownership(symbol: str, info: dict) -> Optional[dict]:
    """Latest-quarter SEC 13F reading for a US ticker from the cached institutional panel,
    or None if the panel isn't built / the ticker isn't held by any tracked manager. Unlike
    the yfinance snapshot this carries a real quarter-over-quarter change. Never raises."""
    try:
        from pipeline_core.services import edgar_13f
        series = edgar_13f.get_institutional_series(symbol)
    except Exception as e:
        logger.warning("EDGAR 13F lookup failed for %s: %s", symbol, e)
        return None
    if series is None or series.empty:
        return None
    last = series.iloc[-1]
    shares_out = info.get("shares_outstanding")
    tracked_pct = round(float(last["agg_shares"]) / shares_out * 100, 1) if shares_out else None
    qoq = last["qoq_change_pct"]
    return {
        "ticker": display_ticker(symbol),
        "source": "edgar-13f",
        "held_pct_institutions": tracked_pct,        # tracked managers' % of shares out (may be None)
        "held_pct_insiders": _as_pct(info.get("held_pct_insiders")),
        "qoq_change_pct": round(float(qoq), 1) if pd.notna(qoq) else None,
        "n_managers": int(last["n_managers"]),
        "quarter": str(last.get("quarter") or series.index[-1].date()),
        "available": True,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


def get_institutional_ownership(symbol: str, market: str = "IN") -> dict:
    """Institutional & insider ownership for a symbol.

    US: prefers real **historical SEC 13F** data (via `edgar_13f`) — a quarterly series that
    carries a genuine quarter-over-quarter change — and falls back to the yfinance snapshot
    when the 13F panel isn't built or the name isn't held by a tracked manager. Other markets
    use the yfinance snapshot. Either way it's a live *confirmation* overlay; the 13F series is
    also what makes the backtestable `institutional-13f-accumulation` strategy possible. Reuses
    the 1-day `get_stock_info` cache. Unsupported markets / missing fields → `available: False`;
    never raises."""
    info = get_stock_info(symbol, market) or {}
    if (market or "IN").upper() == "US":
        edgar = _edgar_ownership(symbol, info)
        if edgar is not None:
            return edgar
    inst = _as_pct(info.get("held_pct_institutions"))
    insiders = _as_pct(info.get("held_pct_insiders"))
    return {
        "ticker": display_ticker(symbol),
        "source": "yfinance",
        "held_pct_institutions": inst,
        "held_pct_insiders": insiders,
        "qoq_change_pct": None,
        "n_managers": None,
        "available": inst is not None,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


def get_price_history(symbol: str, period: str = "1y", interval: str = "1d", market: str = "IN",
                      force_refresh: bool = False) -> pd.DataFrame:
    """OHLCV price history. Cached 15 minutes. Pass `force_refresh=True` to skip the cache and
    re-fetch (used by the live mark-to-market quote so gains/losses reflect the latest price)."""
    m = get_market(market)
    cache_key = f"price:{m.key}:{symbol}:{period}:{interval}"
    if not force_refresh:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    # Crypto OHLCV comes from Binance (full O/H/L/C/V, broad coverage); fall through
    # to yfinance for coins Binance doesn't list. Intraday intervals still use yfinance.
    if m.key == "CRYPTO" and interval == "1d":
        from pipeline_core.services import crypto_data

        cdf = crypto_data.get_ohlcv(symbol, _period_to_days(period))
        if not cdf.empty:
            cache.set(cache_key, cdf, expire=settings.cache_ttl_price)
            return cdf

    try:
        ticker = yf.Ticker(normalize_ticker(symbol, m.key))
        df = ticker.history(period=period, interval=interval)
        if df.empty:
            logger.warning(f"No price data for {symbol}")
            return pd.DataFrame()
        # yfinance can append a trailing row for the incomplete current
        # session with NaN Close — NaN prices silently break every
        # downstream comparison (x > NaN is always False).
        df = df.dropna(subset=["Close"])
        if df.empty:
            logger.warning(f"Only NaN price rows for {symbol}")
            return pd.DataFrame()
        cache.set(cache_key, df, expire=settings.cache_ttl_price)
        return df
    except Exception as e:
        logger.error(f"Failed to fetch price history for {symbol}: {e}")
        return pd.DataFrame()


def get_quote(symbol: str, market: str = "IN", max_age: Optional[int] = None) -> Optional[float]:
    """Freshest last-traded price for mark-to-market P&L.

    Bypasses the 15-minute history cache and re-fetches, then micro-caches just the scalar
    for `max_age` seconds (default `cache_ttl_quote`, ~90s) so open gains/losses reflect a
    (near-)live price without hammering the source. Returns None on failure.

    Crypto is genuinely live (Binance last bar); equities carry the data provider's inherent
    ~15-minute delay — this removes *our* staleness, not the feed's.
    """
    m = get_market(market)
    max_age = settings.cache_ttl_quote if max_age is None else max_age
    quote_key = f"quote:{m.key}:{symbol}"
    cached = cache.get(quote_key)
    if cached is not None:
        return cached
    try:
        df = get_price_history(symbol, period="5d", interval="1d", market=market, force_refresh=True)
        price = round_price(float(df["Close"].iloc[-1])) if df is not None and len(df) else None
    except Exception as e:
        logger.warning(f"Live quote fetch failed for {symbol} ({market}): {e}")
        price = None
    if price is not None:
        cache.set(quote_key, price, expire=max_age)
    return price


async def aget_quote(symbol: str, market: str = "IN", max_age: Optional[int] = None) -> Optional[float]:
    return await asyncio.to_thread(get_quote, symbol, market, max_age)


def get_financials(symbol: str, market: str = "IN") -> dict:
    """Income statement, balance sheet, cash flow. Cached 1 day."""
    m = get_market(market)
    cache_key = f"financials:{m.key}:{symbol}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        ticker = yf.Ticker(normalize_ticker(symbol, m.key))
        result = {
            "income_stmt": ticker.financials.to_dict() if not ticker.financials.empty else {},
            "quarterly_income": ticker.quarterly_financials.to_dict() if not ticker.quarterly_financials.empty else {},
            "balance_sheet": ticker.balance_sheet.to_dict() if not ticker.balance_sheet.empty else {},
            "cashflow": ticker.cashflow.to_dict() if not ticker.cashflow.empty else {},
        }
        result = deep_sanitize(result)
        cache.set(cache_key, result, expire=settings.cache_ttl_fundamentals)
        return result
    except Exception as e:
        logger.error(f"Failed to fetch financials for {symbol}: {e}")
        return {}


def get_index_tickers(index_name: str = "NIFTY 50", market: str = "IN") -> list[str]:
    """
    Constituent tickers for a universe in the given market.
    Dispatches through the market registry (NSE CSV for IN, S&P/NASDAQ for US,
    curated list for metals, CoinGecko for crypto), each with its own graceful
    fallback so a blocked source never returns the wrong universe silently.
    """
    from pipeline_core.markets import get_universe_tickers

    return get_universe_tickers(market, index_name)


def search_stocks(query: str, limit: int = 10, market: str = "IN") -> list[dict]:
    """Search for stocks by name or ticker (direct match + universe scan)."""
    query = query.upper().strip()
    results = []

    info = get_stock_info(query, market)
    if "error" not in info:
        results.append(info)

    # Scan the market's default universe for substring matches.
    try:
        universe = get_index_tickers(get_market(market).default_universe, market)
    except Exception:
        universe = NIFTY_50_TICKERS
    for ticker in universe:
        if query in ticker and ticker != query:
            info = get_stock_info(ticker, market)
            if "error" not in info:
                results.append(info)
            if len(results) >= limit:
                break

    return results[:limit]


# ── Async wrappers (yfinance is blocking → offload to threads) ─────


async def aget_stock_info(symbol: str, market: str = "IN") -> dict:
    return await asyncio.to_thread(get_stock_info, symbol, market)


async def aget_price_history(symbol: str, period: str = "1y", interval: str = "1d", market: str = "IN") -> pd.DataFrame:
    return await asyncio.to_thread(get_price_history, symbol, period, interval, market)


async def aget_financials(symbol: str, market: str = "IN") -> dict:
    return await asyncio.to_thread(get_financials, symbol, market)


async def fetch_batch_info(
    symbols: list[str],
    concurrency: Optional[int] = None,
    progress_callback=None,
    market: str = "IN",
) -> list[dict]:
    """
    Fetch stock info for many symbols with bounded concurrency.
    Per-symbol failures are logged and skipped, never fatal.
    progress_callback(done, total) is invoked after each completion.
    """
    limit = concurrency or settings.ingestion_concurrency
    semaphore = asyncio.Semaphore(limit)
    total = len(symbols)
    done = 0

    async def _one(symbol: str) -> Optional[dict]:
        nonlocal done
        async with semaphore:
            try:
                info = await aget_stock_info(symbol, market)
                return None if "error" in info else info
            except Exception as e:
                logger.error(f"Batch fetch failed for {symbol}: {e}")
                return None
            finally:
                done += 1
                if progress_callback:
                    progress_callback(done, total)

    results = await asyncio.gather(*(_one(s) for s in symbols))
    ok = [r for r in results if r is not None]
    logger.info(f"Batch fetch: {len(ok)}/{total} succeeded (concurrency={limit})")
    return ok
