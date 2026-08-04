"""
NSE-specific data service — index constituents, promoter holdings,
FII/DII data, delivery percentages, bulk/block deals.

Uses publicly available NSE / Nifty Indices endpoints. Everything degrades
gracefully to None / fallback lists when NSE blocks requests (common).
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Optional

import diskcache
import requests

from pipeline_core.config import CACHE_DIR, settings

logger = logging.getLogger(__name__)
cache = diskcache.Cache(str(CACHE_DIR / "nse_data"))

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

NSE_BASE = "https://www.nseindia.com"

# Nifty Indices publishes constituent CSVs at predictable URLs.
INDEX_CSV_URLS = {
    "NIFTY 50": "https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv",
    "NIFTY 100": "https://www.niftyindices.com/IndexConstituent/ind_nifty100list.csv",
    "NIFTY 200": "https://www.niftyindices.com/IndexConstituent/ind_nifty200list.csv",
    "NIFTY 500": "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
    "NIFTY NEXT 50": "https://www.niftyindices.com/IndexConstituent/ind_niftynext50list.csv",
    "NIFTY MIDCAP 150": "https://www.niftyindices.com/IndexConstituent/ind_niftymidcap150list.csv",
    "NIFTY SMALLCAP 250": "https://www.niftyindices.com/IndexConstituent/ind_niftysmallcap250list.csv",
    "NIFTY BANK": "https://www.niftyindices.com/IndexConstituent/ind_niftybanklist.csv",
    "NIFTY IT": "https://www.niftyindices.com/IndexConstituent/ind_niftyitlist.csv",
}

# Canonical aliases → key in INDEX_CSV_URLS
_INDEX_ALIASES = {
    "NIFTY50": "NIFTY 50",
    "NIFTY100": "NIFTY 100",
    "NIFTY200": "NIFTY 200",
    "NIFTY500": "NIFTY 500",
    "NIFTYNEXT50": "NIFTY NEXT 50",
    "NIFTYBANK": "NIFTY BANK",
    "BANKNIFTY": "NIFTY BANK",
    "NIFTYIT": "NIFTY IT",
}


def _nse_session() -> requests.Session:
    """Create a session primed with NSE cookies."""
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    try:
        session.get(NSE_BASE, timeout=10)
    except Exception:
        pass
    return session


# ── Index constituents ─────────────────────────────────────────────


def normalize_index_name(index_name: str) -> str:
    """Map user/index aliases to canonical names ('nifty500' → 'NIFTY 500')."""
    name = (index_name or "").upper().strip()
    if name in INDEX_CSV_URLS:
        return name
    return _INDEX_ALIASES.get(name.replace(" ", ""), name)


def get_index_constituents(index_name: str = "NIFTY 50") -> list[str]:
    """
    Live constituent list from the Nifty Indices CSV, cached 1 day.
    Falls back to the hardcoded NIFTY 50 list with a loud warning when
    the CSV can't be fetched or the index is unknown.
    """
    from pipeline_core.services.market_data import NIFTY_50_TICKERS

    canonical = normalize_index_name(index_name)
    url = INDEX_CSV_URLS.get(canonical)
    if url is None:
        logger.warning(
            f"Unknown index '{index_name}' — falling back to hardcoded NIFTY 50 universe. "
            f"Known indices: {sorted(INDEX_CSV_URLS)}"
        )
        return NIFTY_50_TICKERS.copy()

    cache_key = f"index:{canonical}"
    cached = cache.get(cache_key)
    if cached is not None:
        return list(cached)

    try:
        resp = requests.get(url, headers=NSE_HEADERS, timeout=20)
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        tickers = [
            row["Symbol"].strip().upper()
            for row in reader
            if row.get("Symbol", "").strip()
        ]
        if not tickers:
            raise ValueError("constituent CSV parsed to zero symbols")
        cache.set(cache_key, tickers, expire=settings.cache_ttl_holdings)
        logger.info(f"Fetched {len(tickers)} constituents for {canonical}")
        return tickers
    except Exception as e:
        logger.error(
            f"Failed to fetch constituents for {canonical} ({e}) — "
            "falling back to hardcoded NIFTY 50 universe"
        )
        return NIFTY_50_TICKERS.copy()


# ── NSE per-stock enrichment ───────────────────────────────────────


def get_promoter_holding(symbol: str) -> Optional[dict]:
    """Promoter / FII / DII / pledge holding data from NSE."""
    cache_key = f"promoter:{symbol}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        session = _nse_session()
        url = f"{NSE_BASE}/api/corporate-shareholding?symbol={symbol.upper()}"
        resp = session.get(url, timeout=15)

        if resp.status_code != 200:
            logger.warning(f"NSE returned {resp.status_code} for promoter data: {symbol}")
            return None

        data = resp.json()
        shareholding = data.get("data", [])

        result = {
            "promoter_holding_pct": None,
            "pledge_pct": None,
            "fii_holding_pct": None,
            "dii_holding_pct": None,
            "public_holding_pct": None,
        }

        for entry in shareholding:
            category = entry.get("category", "").lower()
            pct = entry.get("percentage")
            if pct is not None:
                pct = float(pct)
            if "promoter" in category and "group" in category:
                result["promoter_holding_pct"] = pct
            elif "foreign" in category or "fii" in category:
                result["fii_holding_pct"] = pct
            elif "mutual" in category or "dii" in category or "domestic" in category:
                result["dii_holding_pct"] = pct
            elif "public" in category:
                result["public_holding_pct"] = pct

        pledge_data = data.get("pledgeData", {})
        if pledge_data:
            result["pledge_pct"] = pledge_data.get("pledgePercentage")

        cache.set(cache_key, result, expire=settings.cache_ttl_holdings)
        return result

    except Exception as e:
        logger.error(f"Failed to fetch promoter holding for {symbol}: {e}")
        return None


def get_delivery_data(symbol: str) -> Optional[dict]:
    """Delivery percentage data from NSE."""
    cache_key = f"delivery:{symbol}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        session = _nse_session()
        url = f"{NSE_BASE}/api/quote-equity?symbol={symbol.upper()}&section=trade_info"
        resp = session.get(url, timeout=15)

        if resp.status_code != 200:
            return None

        data = resp.json()
        trade_info = data.get("marketDeptOrderBook", {}).get("tradeInfo", {})

        result = {
            "delivery_quantity": trade_info.get("deliveryQuantity"),
            "delivery_pct": trade_info.get("deliveryToTradedQuantity"),
            "total_traded_volume": trade_info.get("totalTradedVolume"),
        }

        cache.set(cache_key, result, expire=settings.cache_ttl_price)
        return result

    except Exception as e:
        logger.error(f"Failed to fetch delivery data for {symbol}: {e}")
        return None


def get_bulk_deals(symbol: str) -> list[dict]:
    """Recent bulk/block deals for a symbol from NSE."""
    cache_key = f"bulk_deals:{symbol}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        session = _nse_session()
        url = f"{NSE_BASE}/api/historical/bulk-deals?symbol={symbol.upper()}"
        resp = session.get(url, timeout=15)

        if resp.status_code != 200:
            return []

        deals = resp.json().get("data", [])
        result = [
            {
                "date": deal.get("date"),
                "client": deal.get("clientName"),
                "deal_type": deal.get("dealType"),
                "quantity": deal.get("quantity"),
                "price": deal.get("price"),
            }
            for deal in deals[:20]
        ]

        cache.set(cache_key, result, expire=settings.cache_ttl_holdings)
        return result

    except Exception as e:
        logger.error(f"Failed to fetch bulk deals for {symbol}: {e}")
        return []


def get_stock_quote(symbol: str) -> Optional[dict]:
    """Real-time quote from NSE (5-minute cache)."""
    cache_key = f"quote:{symbol}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        session = _nse_session()
        url = f"{NSE_BASE}/api/quote-equity?symbol={symbol.upper()}"
        resp = session.get(url, timeout=15)

        if resp.status_code != 200:
            return None

        data = resp.json()
        price_info = data.get("priceInfo", {})
        industry_info = data.get("industryInfo", {})

        result = {
            "symbol": symbol.upper(),
            "name": data.get("info", {}).get("companyName"),
            "price": price_info.get("lastPrice"),
            "change": price_info.get("change"),
            "change_pct": price_info.get("pChange"),
            "open": price_info.get("open"),
            "close": price_info.get("close"),
            "high": price_info.get("intraDayHighLow", {}).get("max"),
            "low": price_info.get("intraDayHighLow", {}).get("min"),
            "week_52_high": price_info.get("weekHighLow", {}).get("max"),
            "week_52_low": price_info.get("weekHighLow", {}).get("min"),
            "sector": industry_info.get("sector"),
            "industry": industry_info.get("industry"),
        }

        cache.set(cache_key, result, expire=300)
        return result

    except Exception as e:
        logger.error(f"Failed to fetch NSE quote for {symbol}: {e}")
        return None


def enrich_with_nse_data(stock_info: dict) -> dict:
    """
    Enrich a stock info dict with NSE-specific data
    (promoter holding, pledge, FII/DII, delivery %).
    Missing data leaves fields absent — never raises.
    """
    symbol = stock_info.get("ticker", "")
    if not symbol or "error" in stock_info:
        return stock_info

    holding = get_promoter_holding(symbol)
    if holding:
        stock_info["promoter_holding"] = holding.get("promoter_holding_pct")
        stock_info["promoter_pledge"] = holding.get("pledge_pct")
        stock_info["fii_holding"] = holding.get("fii_holding_pct")
        stock_info["dii_holding"] = holding.get("dii_holding_pct")

    delivery = get_delivery_data(symbol)
    if delivery:
        stock_info["delivery_pct"] = delivery.get("delivery_pct")

    return stock_info


async def aenrich_with_nse_data(stock_info: dict) -> dict:
    """Async wrapper — NSE calls are blocking requests."""
    import asyncio

    return await asyncio.to_thread(enrich_with_nse_data, stock_info)
