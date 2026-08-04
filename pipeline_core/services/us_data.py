"""
US equities universe service — S&P 500 / NASDAQ-100 / Dow 30 constituents.

Mirrors nse_data.get_index_constituents: the S&P 500 list is fetched live from a
stable public CSV (cached 1 day) with a hardcoded large-cap fallback; NASDAQ-100
and Dow 30 are curated constituent lists (they change rarely). Everything degrades
gracefully — a blocked request never returns the wrong universe silently.

Tickers are display symbols (e.g. BRK.B); markets.normalize_ticker maps them to
the yfinance form (BRK-B) at fetch time.
"""

from __future__ import annotations

import csv
import io
import logging

import diskcache
import requests

from pipeline_core.config import CACHE_DIR, settings

logger = logging.getLogger(__name__)
cache = diskcache.Cache(str(CACHE_DIR / "us_data"))

# datahub publishes a stable, well-formed S&P 500 constituents CSV (Symbol column).
SP500_CSV_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"

_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "text/csv, text/plain, */*"}

# Dow Jones Industrial Average — 30 names, stable enough to hardcode.
DOW_30 = [
    "AAPL", "AMGN", "AMZN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS",
    "GS", "HD", "HON", "IBM", "JNJ", "JPM", "KO", "MCD", "MMM", "MRK",
    "MSFT", "NKE", "NVDA", "PG", "SHW", "TRV", "UNH", "V", "VZ", "WMT",
]

# NASDAQ-100 — curated constituent list (primary source; refreshed occasionally).
NASDAQ_100 = [
    "AAPL", "ABNB", "ADBE", "ADI", "ADP", "ADSK", "AEP", "AMAT", "AMD", "AMGN",
    "AMZN", "ANSS", "APP", "ARM", "ASML", "AVGO", "AZN", "BIIB", "BKNG", "BKR",
    "CDNS", "CDW", "CEG", "CHTR", "CMCSA", "COST", "CPRT", "CRWD", "CSCO", "CSGP",
    "CSX", "CTAS", "CTSH", "DASH", "DDOG", "DXCM", "EA", "EXC", "FANG", "FAST",
    "FTNT", "GEHC", "GFS", "GILD", "GOOG", "GOOGL", "HON", "IDXX", "INTC", "INTU",
    "ISRG", "KDP", "KHC", "KLAC", "LIN", "LRCX", "LULU", "MAR", "MCHP", "MDLZ",
    "MELI", "META", "MNST", "MRVL", "MSFT", "MU", "NFLX", "NVDA", "NXPI", "ODFL",
    "ON", "ORLY", "PANW", "PAYX", "PCAR", "PDD", "PEP", "PLTR", "PYPL", "QCOM",
    "REGN", "ROP", "ROST", "SBUX", "SNPS", "TEAM", "TMUS", "TSLA", "TTD", "TTWO",
    "TXN", "VRSK", "VRTX", "WBD", "WDAY", "XEL", "ZS",
]

# Large-cap fallback for the S&P 500 when the CSV can't be fetched.
SP500_FALLBACK = sorted(set(NASDAQ_100) | set(DOW_30) | {
    "BRK.B", "LLY", "JPM", "XOM", "MA", "PG", "COST", "ORCL", "BAC", "ABBV",
    "WFC", "KO", "PEP", "TMO", "MCD", "ABT", "ACN", "DHR", "GE", "PM",
})

_ALIASES = {
    "SP500": "S&P 500", "S&P500": "S&P 500", "SPX": "S&P 500", "SANDP500": "S&P 500",
    "NASDAQ100": "NASDAQ-100", "NDX": "NASDAQ-100", "NASDAQ": "NASDAQ-100",
    "DOW": "Dow 30", "DOW30": "Dow 30", "DJIA": "Dow 30",
}


def normalize_index_name(name: str) -> str:
    """Map aliases to canonical US universe names ('sp500' → 'S&P 500')."""
    raw = (name or "").strip()
    canonical = {"S&P 500", "NASDAQ-100", "Dow 30"}
    if raw in canonical:
        return raw
    return _ALIASES.get(raw.upper().replace(" ", "").replace("-", ""), raw)


def _fetch_sp500() -> list[str]:
    """Live S&P 500 symbols from the datahub CSV (cached 1 day), fallback on failure."""
    cache_key = "us_index:S&P 500"
    cached = cache.get(cache_key)
    if cached is not None:
        return list(cached)
    try:
        resp = requests.get(SP500_CSV_URL, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        tickers = [row["Symbol"].strip().upper() for row in reader if row.get("Symbol", "").strip()]
        if not tickers:
            raise ValueError("S&P 500 CSV parsed to zero symbols")
        cache.set(cache_key, tickers, expire=settings.cache_ttl_holdings)
        logger.info(f"Fetched {len(tickers)} S&P 500 constituents")
        return tickers
    except Exception as e:
        logger.error(f"Failed to fetch S&P 500 constituents ({e}) — using large-cap fallback")
        return list(SP500_FALLBACK)


def get_index_constituents(index_name: str = "S&P 500") -> list[str]:
    """Constituent display-symbols for a US universe."""
    canonical = normalize_index_name(index_name)
    if canonical == "NASDAQ-100":
        return list(NASDAQ_100)
    if canonical == "Dow 30":
        return list(DOW_30)
    if canonical == "S&P 500":
        return _fetch_sp500()
    logger.warning(f"Unknown US universe '{index_name}' — falling back to S&P 500")
    return _fetch_sp500()
