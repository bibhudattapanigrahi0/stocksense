"""
Crypto "smart money" — the top traders on Hyperliquid, read live and on-chain.

Crypto has no 13F and no COT, but it has something no other market does: a fully on-chain perp
exchange (Hyperliquid) whose leaderboard AND every account's live positions are public and keyless.
That makes the literal "copy the better traders" signal possible — take the top performers by PnL,
read what they're actually positioned in right now, and aggregate their net directional bias per coin.

This is a LIVE overlay only: Hyperliquid exposes current positions, not a queryable history, so there
is no backtestable series here (`series`/`enrich_frames` are no-ops) — crypto gets a smart-money
pillar + badge + a leaders list, but not a backtested follow-strategy. Honest by construction.

Implements the smart_money source-module contract (live_signal / series / enrich_frames) plus a
`leaders()` view for the coattail endpoint. The whole aggregate "book" is built once and cached, so a
per-coin lookup is just a dict read. Degrades to None / no-op on any failure; never raises into callers.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

import diskcache
import requests

from pipeline_core.config import CACHE_DIR, settings

logger = logging.getLogger(__name__)
cache = diskcache.Cache(str(CACHE_DIR / "hl"))

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO_URL = "https://api.hyperliquid.xyz/info"
HL_TTL = 1800                 # 30 min — top-trader positioning is slow-moving relative to a build
_HTTP_TIMEOUT = 20

# Curated address → known label (flag-gated: sm_crypto_labels_enabled). Seeded empty on purpose — we
# never fabricate an identity; the scaffold falls back to the trader's self-set Hyperliquid displayName
# and then a shortened address. Paid label providers (Nansen/Arkham) could populate this later.
HL_LABELS: dict[str, str] = {}


def _label(address: str, display_name: Optional[str] = None) -> str:
    lab = HL_LABELS.get((address or "").lower())
    if lab:
        return lab
    if display_name and display_name.strip():
        return display_name.strip()
    return (address[:6] + "…" + address[-4:]) if address and len(address) > 12 else (address or "anon")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coin(symbol: str) -> str:
    """App crypto symbol → Hyperliquid coin (BTC-USD / BTCUSD / BTC → BTC)."""
    s = symbol.strip().upper()
    for suf in ("-USD", "USD", "-USDT", "USDT"):
        if s.endswith(suf) and len(s) > len(suf):
            s = s[: -len(suf)]
            break
    return s.strip("-")


# ── Network leaves (patched out in tests) ───────────────────────────

def _fetch_leaderboard() -> list[dict]:
    resp = requests.get(LEADERBOARD_URL, headers={"Accept": "application/json"}, timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return data.get("leaderboardRows", []) if isinstance(data, dict) else (data or [])


def _fetch_positions(address: str) -> list[dict]:
    resp = requests.post(INFO_URL, json={"type": "clearinghouseState", "user": address},
                         timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    return (resp.json() or {}).get("assetPositions", [])


# ── Pure aggregation (unit-tested directly) ─────────────────────────

def _window_perf(row: dict, window: str) -> dict:
    for w, perf in row.get("windowPerformances", []):
        if w == window:
            return perf or {}
    return {}


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def select_top_traders(rows: list[dict], n: int, window: str, min_account_usd: float) -> list[dict]:
    """Rank sizeable accounts by their window PnL and keep the top n winners. Pure."""
    cand = []
    for r in rows:
        av = _f(r.get("accountValue"))
        if av < min_account_usd:
            continue
        pnl = _f(_window_perf(r, window).get("pnl"))
        if pnl <= 0:                      # "better traders" = winners over the window
            continue
        cand.append({"addr": r.get("ethAddress"), "name": r.get("displayName"),
                     "account_value": av, "pnl": pnl,
                     "roi": _f(_window_perf(r, window).get("roi"))})
    cand.sort(key=lambda t: t["pnl"], reverse=True)
    return [t for t in cand if t["addr"]][:n]


def aggregate_positions(positions_by_trader: list[list[dict]]) -> dict:
    """List of each trader's assetPositions → per-coin book: net USD notional (long−short),
    gross notional, and how many traders are long vs short. Pure."""
    book: dict[str, dict] = {}
    for aps in positions_by_trader:
        for ap in aps or []:
            p = ap.get("position", {})
            coin = (p.get("coin") or "").upper()
            if not coin:
                continue
            szi = _f(p.get("szi"))
            notional = abs(_f(p.get("positionValue")))
            if szi == 0 or notional == 0:
                continue
            e = book.setdefault(coin, {"net_notional": 0.0, "gross": 0.0, "n_long": 0, "n_short": 0})
            e["gross"] += notional
            if szi > 0:
                e["net_notional"] += notional
                e["n_long"] += 1
            else:
                e["net_notional"] -= notional
                e["n_short"] += 1
    return book


def build_book(force: bool = False) -> dict:
    """Fetch the leaderboard, read the top traders' live positions, and aggregate into a per-coin
    book. Cached (30 min) so per-coin lookups and the leaders view are one network build. Never raises."""
    key = "hl:book"
    if not force:
        cached = cache.get(key)
        if cached is not None:
            return cached
    empty = {"coins": {}, "n_traders": 0, "window": getattr(settings, "hl_window", "month"), "as_of": None}
    try:
        rows = _fetch_leaderboard()
    except Exception as e:
        logger.warning("Hyperliquid leaderboard fetch failed: %s", e)
        return empty
    window = getattr(settings, "hl_window", "month")
    top = select_top_traders(rows, getattr(settings, "hl_top_traders", 40), window,
                             getattr(settings, "hl_min_account_usd", 1_000_000.0))
    positions: list[list[dict]] = []
    if top:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(_fetch_positions, t["addr"]) for t in top]
            for f in as_completed(futs):
                try:
                    positions.append(f.result())
                except Exception:
                    continue      # one unreachable account must not sink the build
    out = {"coins": aggregate_positions(positions), "n_traders": len(top),
           "window": window, "as_of": _now_iso()}
    if getattr(settings, "sm_crypto_labels_enabled", False):
        out["top_traders"] = [{"label": _label(t["addr"], t.get("name")),
                               "pnl": round(t["pnl"], 0), "account_value": round(t["account_value"], 0)}
                              for t in top[:10]]
    cache.set(key, out, expire=HL_TTL)
    return out


# ── smart_money source-module contract ──────────────────────────────

def _conviction(direction: str, agree: float, strength: float) -> Optional[str]:
    if direction != "accumulate":
        return None
    if agree >= 0.66 and strength >= 0.4:
        return "high"
    if agree >= 0.55 and strength >= 0.2:
        return "medium"
    return "low"


def signal_from_book(book: dict, coin: str) -> Optional[dict]:
    """One coin's entry in the aggregate book → normalized smart-money signal, or None. Pure."""
    entry = (book.get("coins") or {}).get(coin)
    if not entry:
        return None
    n_long, n_short = entry["n_long"], entry["n_short"]
    nt = n_long + n_short
    if nt == 0:
        return None
    net, gross = entry["net_notional"], entry["gross"] or 1.0
    direction = "accumulate" if net > 0 else "distribute" if net < 0 else "neutral"
    agree = max(n_long, n_short) / nt
    strength = abs(net) / gross
    net_m = net / 1e6
    detail = (f"Top {book.get('n_traders', nt)} Hyperliquid traders net "
              f"{'+' if net >= 0 else '−'}${abs(net_m):.1f}m {'long' if net >= 0 else 'short'} "
              f"{coin} ({n_long}/{nt} long)")
    return {
        "available": True,
        "source": "hyperliquid",
        "family": "flow",
        "direction": direction,
        "net_delta_pct": round(strength * 100 * (1 if net >= 0 else -1), 1),
        "conviction": _conviction(direction, agree, strength),
        "actors": [f"{nt} top HL traders"],
        "n_actors": nt,
        "label": "Whales",
        "detail": detail,
        "as_of": book.get("as_of"),
    }


def live_signal(symbol: str) -> Optional[dict]:
    """Live Hyperliquid top-trader positioning for a crypto symbol (smart_money contract)."""
    return signal_from_book(build_book(), _coin(symbol))


def leaders(limit: int = 10) -> dict:
    """The coattail view: coins where the top Hyperliquid traders are most one-sided right now,
    ranked by net notional. Feeds the smart-money leaders endpoint."""
    book = build_book()
    coins = []
    for coin, e in (book.get("coins") or {}).items():
        nt = e["n_long"] + e["n_short"]
        if nt == 0:
            continue
        coins.append({
            "coin": coin, "net_notional": round(e["net_notional"], 0), "gross": round(e["gross"], 0),
            "n_long": e["n_long"], "n_short": e["n_short"],
            "bias": "long" if e["net_notional"] > 0 else "short" if e["net_notional"] < 0 else "flat",
        })
    coins.sort(key=lambda c: abs(c["net_notional"]), reverse=True)
    out = {"leaders": coins[:limit], "n_traders": book.get("n_traders", 0),
           "window": book.get("window"), "as_of": book.get("as_of")}
    if book.get("top_traders"):        # present only when sm_crypto_labels_enabled
        out["top_traders"] = book["top_traders"]
    return out


def series(symbol: str):
    """No queryable history on Hyperliquid — crypto smart money is a live overlay only."""
    return None


def enrich_frames(df_by_ticker: dict) -> None:
    """No historical series ⇒ nothing to merge onto daily bars (crypto is live-only)."""
    return None
