"""
Crypto data service — CoinGecko for the universe + market caps, Binance public
market data for OHLCV history.

Why this split:
  - **CoinGecko** (`/coins/markets`) is the authority on which coins exist and their
    market-cap ranking + live market cap / name — data an exchange feed can't give.
  - **Binance** (`data-api.binance.vision`, the public market-data host — no API key,
    no geo-block) gives full daily OHLCV (open/high/low/close/volume) with broad
    coverage of the liquid coins that actually matter for screening/backtesting —
    far better than yfinance's spotty crypto series.

OHLCV falls back to yfinance for any coin Binance doesn't list (e.g. brand-new or
non-Binance tokens), so coverage never regresses. Stablecoins, wrapped/staked
derivatives, tokenized-treasury (RWA) funds, and non-Latin tickers are filtered
out of the universe (no meaningful tradable series).
"""

from __future__ import annotations

import logging
import time

import diskcache
import pandas as pd
import requests

from pipeline_core.config import CACHE_DIR, settings

logger = logging.getLogger(__name__)
cache = diskcache.Cache(str(CACHE_DIR / "crypto_data"))

COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
# Binance's dedicated public market-data host (no key, no geo restrictions).
BINANCE_KLINES_URL = "https://data-api.binance.vision/api/v3/klines"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# CoinGecko's top-by-market-cap list is polluted with instruments that have no
# tradable OHLCV series — stablecoins, wrapped/staked/liquid-staking derivatives,
# and tokenized treasuries / money-market (RWA) funds. Excluding them keeps the
# universe tradable.
_EXCLUDE = {
    # Stablecoins / fiat-pegged (most also caught by the "USD"/"EUR" substring rule)
    "DAI", "BUSD", "TUSD", "USDE", "FDUSD", "USDS", "PYUSD", "GUSD", "USDD", "FRAX",
    "LUSD", "USDP", "USDY", "USDB", "USDX", "USR", "RLUSD", "USDF", "USDG", "USD1",
    "MIM", "DOLA", "OUSD", "VAI", "GYEN", "XSGD", "AEUR", "EURC", "EURS", "EURT",
    # Gold-pegged (not a crypto-momentum instrument)
    "XAUT", "PAXG",
    # Wrapped / staked / liquid-staking derivatives
    "WBTC", "WETH", "STETH", "WSTETH", "WEETH", "WBETH", "CBBTC", "RETH", "WHYPE",
    "LBTC", "CLBTC", "SOLVBTC", "MSOL", "JITOSOL", "BSOL", "JUPSOL", "EZETH",
    "RSETH", "METH", "WEETHS", "OSETH", "ETHX", "SUSDE", "SUSDS", "SFRAX", "STUSDT",
    # Tokenized treasuries / money-market / RWA funds (no yfinance series)
    "BUIDL", "USYC", "USTB", "USTBL", "OUSG", "JTRSY", "JAAA", "EUTBL", "BCAP",
    "BENJI", "TBILL", "RBILL", "WTGXX", "TBIL", "VBILL",
    # Exchange tokens frequently missing from public feeds
    "WBT", "LEO",
}

# Coins whose Binance spot symbol differs from the CoinGecko ticker.
_BINANCE_ALIAS = {"IOTA": "IOTA", "NEAR": "NEAR"}  # placeholder for future overrides


def _is_tradable(sym: str) -> bool:
    """Keep only symbols that are plausibly a real, exchange-listed coin."""
    if not (sym and sym.isascii() and sym.isalnum()):
        return False                       # drops non-Latin symbols (e.g. 币安人生)
    if "USD" in sym or "EUR" in sym:       # stablecoins + tokenized-fiat funds
        return False
    return sym not in _EXCLUDE


# ── CoinGecko: universe + market-cap snapshot ───────────────────────


def _fetch_markets(count: int = 250) -> list[dict]:
    """Top coins by market cap from CoinGecko (cached 1 day). Full rows so we can
    reuse them for both the universe and per-coin profiles."""
    cache_key = f"cg_markets:{count}"
    cached = cache.get(cache_key)
    if cached is not None:
        return list(cached)
    try:
        resp = requests.get(
            COINGECKO_MARKETS_URL,
            params={"vs_currency": "usd", "order": "market_cap_desc",
                    "per_page": min(250, count), "page": 1},
            headers=_HEADERS, timeout=20,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not isinstance(rows, list) or not rows:
            raise ValueError("CoinGecko markets returned no rows")
        cache.set(cache_key, rows, expire=settings.cache_ttl_holdings)
        logger.info(f"Fetched {len(rows)} CoinGecko market rows")
        return rows
    except Exception as e:
        logger.error(f"CoinGecko markets fetch failed ({e}) — using majors fallback")
        return []


def _universe_size(name: str) -> int:
    """'Top 50' → 50, 'Top 100' → 100 (default 100)."""
    try:
        return int(name.strip().split()[-1])
    except (ValueError, IndexError, AttributeError):
        return 100


def get_universe(name: str = "Top 100") -> list[str]:
    """Constituent coin symbols (display form, e.g. 'BTC') for a crypto universe."""
    limit = _universe_size(name)
    rows = _fetch_markets(min(250, limit * 2 + 50))
    if not rows:
        return _FALLBACK[:limit]
    seen: set[str] = set()
    symbols: list[str] = []
    for row in rows:
        sym = str(row.get("symbol", "")).upper().strip()
        if _is_tradable(sym) and sym not in seen:
            seen.add(sym)
            symbols.append(sym)
    return (symbols or _FALLBACK)[:limit]


def get_profile(symbol: str) -> dict | None:
    """Live profile for one coin from the CoinGecko snapshot (name, price, market
    cap, 24h volume/high/low, change). None if the coin isn't in the snapshot."""
    sym = symbol.upper().strip()
    for row in _fetch_markets(250):
        if str(row.get("symbol", "")).upper() == sym:
            return {
                "name": row.get("name") or sym,
                "price": row.get("current_price"),
                "market_cap": row.get("market_cap"),
                "volume": row.get("total_volume"),
                "day_high": row.get("high_24h"),
                "day_low": row.get("low_24h"),
                "change_pct": row.get("price_change_percentage_24h"),
            }
    return None


# ── Binance: OHLCV history (daily + intraday timeframes) ────────────

# Milliseconds per Binance kline interval — used to page backwards from now.
_INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000,
}

# Display timeframe → (native Binance interval, pandas resample rule or None).
# 10m isn't a native Binance interval, so it's resampled from 5m.
TIMEFRAMES: dict[str, tuple[str, str | None]] = {
    "5m": ("5m", None),
    "10m": ("5m", "10min"),
    "15m": ("15m", None),
    "30m": ("30m", None),
    "1h": ("1h", None),
    "4h": ("4h", None),
    "1d": ("1d", None),
}


def _rows_to_df(rows: list[list]) -> pd.DataFrame:
    """Binance kline rows → yfinance-shaped OHLCV frame (naive UTC index)."""
    if not rows:
        return pd.DataFrame()
    idx = pd.to_datetime([int(x[0]) for x in rows], unit="ms")  # naive UTC
    df = pd.DataFrame(
        {
            "Open": [float(x[1]) for x in rows],
            "High": [float(x[2]) for x in rows],
            "Low": [float(x[3]) for x in rows],
            "Close": [float(x[4]) for x in rows],
            "Volume": [float(x[5]) for x in rows],
        },
        index=idx,
    )
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df.dropna(subset=["Close"])


def _binance_klines(symbol: str, interval: str, bars: int) -> pd.DataFrame:
    """Fetch ~`bars` klines of `interval` for SYM/USDT from Binance (paginated).
    Empty frame when the coin isn't listed — callers fall back to yfinance."""
    pair = f"{_BINANCE_ALIAS.get(symbol.upper(), symbol.upper())}USDT"
    step = _INTERVAL_MS.get(interval, 86_400_000)
    end_ms = int(time.time() * 1000)
    cursor = end_ms - int(bars * step)
    rows: list[list] = []
    for _ in range(30):  # safety cap (30 × 1000 bars ≫ any window we use)
        try:
            r = requests.get(
                BINANCE_KLINES_URL,
                params={"symbol": pair, "interval": interval, "startTime": cursor, "limit": 1000},
                headers=_HEADERS, timeout=15,
            )
        except Exception as e:
            logger.warning(f"Binance klines error for {pair} {interval}: {e}")
            break
        if r.status_code != 200:
            if not rows:  # 400 = not listed on Binance → let the caller fall back
                logger.info(f"Binance has no {pair} {interval} ({r.status_code}) — falling back")
            break
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 1000:
            break
        cursor = int(batch[-1][0]) + step
        if cursor >= end_ms:
            break
    return _rows_to_df(rows)


# ── Kraken (second OHLCV source, behind Binance) ────────────────────

KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"
# Binance interval → Kraken interval in minutes (Kraken has no 10m; get_klines fetches
# the native 5m and resamples upstream, so 10m never reaches here).
_KRAKEN_INTERVAL = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
_KRAKEN_ALIAS = {"BTC": "XBT", "DOGE": "XDG"}  # Kraken's legacy tickers


def _kraken_klines(symbol: str, interval: str, bars: int) -> pd.DataFrame:
    """OHLCV for one coin from Kraken (SYM/USD). Kraken caps at ~720 candles — plenty
    for an intraday scan, a fallback (not primary) for long daily history. Empty frame
    when the coin isn't on Kraken."""
    mins = _KRAKEN_INTERVAL.get(interval)
    if mins is None:
        return pd.DataFrame()
    base = _KRAKEN_ALIAS.get(symbol.upper(), symbol.upper())
    since = int(time.time()) - bars * mins * 60
    r = requests.get(KRAKEN_OHLC_URL, params={"pair": f"{base}USD", "interval": mins, "since": since},
                     headers=_HEADERS, timeout=15)
    if r.status_code != 200:
        return pd.DataFrame()
    payload = r.json()
    if payload.get("error"):  # e.g. "Unknown asset pair"
        return pd.DataFrame()
    rows = next((v for k, v in payload.get("result", {}).items() if k != "last"), [])
    if not rows:
        return pd.DataFrame()
    idx = pd.to_datetime([int(x[0]) for x in rows], unit="s")   # Kraken time is seconds
    df = pd.DataFrame({
        "Open": [float(x[1]) for x in rows], "High": [float(x[2]) for x in rows],
        "Low": [float(x[3]) for x in rows], "Close": [float(x[4]) for x in rows],
        "Volume": [float(x[6]) for x in rows],
    }, index=idx)
    return df[~df.index.duplicated(keep="last")].sort_index().dropna(subset=["Close"])


def _fetch_ohlcv(symbol: str, interval: str, bars: int) -> pd.DataFrame:
    """Crypto OHLCV via the provider fallback chain: Binance → Kraken. (yfinance stays
    the final fallback at the market-data layer for coins on neither.)"""
    from pipeline_core.services import providers

    df, _src = providers.fetch_with_fallback(
        [("binance", lambda: _binance_klines(symbol, interval, bars)),
         ("kraken", lambda: _kraken_klines(symbol, interval, bars))],
        validate=lambda d: d is not None and not d.empty,
        retries=1, backoff=0.4,
    )
    return df if df is not None else pd.DataFrame()


def get_ohlcv(symbol: str, days: int = 365) -> pd.DataFrame:
    """Daily OHLCV for one coin (Binance → Kraken fallback), most-recent `days` days.
    Empty frame when the coin is on neither — callers then fall back to yfinance."""
    return _fetch_ohlcv(symbol, "1d", days)


def get_klines(symbol: str, timeframe: str = "15m", bars: int = 300,
               closed_only: bool = True) -> pd.DataFrame:
    """Last `bars` OHLCV bars of an intraday/daily `timeframe` for one coin (Binance),
    yfinance-shaped. `10m` is resampled from `5m`. Cached ~2 min. Empty when unlisted.

    `closed_only` (default True) drops the current **in-progress** candle so signals are
    computed on completed bars only — the last kline Binance returns is the forming candle,
    and evaluating it makes intraday signals *repaint* (flip as the bar develops)."""
    tf = TIMEFRAMES.get(timeframe)
    if tf is None:
        raise ValueError(f"Unknown timeframe '{timeframe}'. Known: {sorted(TIMEFRAMES)}")
    interval, resample_rule = tf

    cache_key = f"klines:{symbol.upper()}:{timeframe}:{bars}:{int(closed_only)}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    # Over-fetch native bars when resampling (e.g. 2× 5m bars → `bars` 10m bars).
    fetch_bars = (bars * 2 if resample_rule else bars) + 5
    df = _fetch_ohlcv(symbol, interval, fetch_bars)   # Binance → Kraken fallback
    if df.empty:
        return df
    if closed_only and len(df) > 1:
        df = df.iloc[:-1]                  # drop the forming candle before resampling (no repaint)
    if resample_rule:
        df = df.resample(resample_rule).agg(
            {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
        ).dropna(subset=["Close"])
    df = df.tail(bars)
    cache.set(cache_key, df, expire=120)  # intraday — keep it fresh
    return df


# ── Fallback universe (CoinGecko unavailable) ───────────────────────

_FALLBACK = [
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "TRX", "LINK", "AVAX",
    "DOT", "MATIC", "LTC", "BCH", "SHIB", "XLM", "ATOM", "UNI", "ETC", "FIL",
    "APT", "ARB", "OP", "NEAR", "ICP", "AAVE", "ALGO", "VET", "INJ", "SUI",
    "RNDR", "IMX", "HBAR", "GRT", "STX", "TIA", "SEI", "MKR", "LDO", "QNT",
    "FTM", "RUNE", "AXS", "SAND", "MANA", "EGLD", "THETA", "FLOW", "XTZ", "CHZ",
    "CRV", "SNX", "GALA", "KAVA", "ZEC", "ENJ", "DYDX", "COMP", "1INCH", "BAT",
    "APE", "PEPE", "WLD", "JUP", "PYTH", "ONDO", "ENA", "BONK", "FET", "TAO",
    "KAS", "MNT", "CRO", "GT", "FLR", "AR", "ROSE", "CFX", "MINA", "NEO",
    "IOTA", "KSM", "ZIL", "GMX", "WOO", "BLUR", "CAKE", "TWT", "DASH", "WAVES",
    "QTUM", "ANKR", "SKL", "LRC", "CVX", "YFI", "BAL", "XMR", "XEC", "GLMR",
]
