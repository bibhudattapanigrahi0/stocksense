"""
Intraday **Trade Ideas** for crypto — the conviction layer over the signal-bot family.

Where the daily Trade Ideas aggregate the 35 backtest strategies by conviction, this does
the same for the *intraday* signal bots on a chosen timeframe (5m / 10m / 15m / 30m / 1h /
4h). For each coin it runs **every** signal bot once (data loaded a single time), then groups
the fired signals by side. A coin is a *long idea* when several bots independently agree long,
a *short idea* when they agree short — conviction = how many bots concur. Both books are
returned, ranked by conviction then reward:risk.

Representative levels are the median entry / stop / target across the agreeing bots, with R
recomputed from them, so an idea reads as one actionable setup rather than N overlapping ones.

When an intraday backtest has been run (`bots/backtest.py`) the measured edge doesn't just
weight votes, it **selects** ideas — quality over quantity:

  • *Regime eligibility* — in each regime (trend/range) only the bots that are competitive
    there generate ideas at all (those clearing the better half of the measured pack plus an
    absolute floor); the measurably-worst bot/regime combos are pruned before conviction is
    even counted, so a known-losing bot can't manufacture a setup.
  • *Signed quality ranking* — ideas sort by the agreeing bots' mean measured expectancy, so
    the least-bad rise to the top even when the whole book is negative.
  • *Edge-backed vs exploratory* — an idea is `edge_backed` only when a genuinely positive-edge
    bot agrees; `strict=True` hides everything else (a true "stand aside" book).

Absent a backtest, every bot is eligible, votes weigh 1.0, and ranking falls back to the raw
agreement count exactly as before.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
from datetime import datetime, timezone
from typing import Optional

from pipeline_core.agents import indicators as ind
from pipeline_core.bots import backtest as bt
from pipeline_core.bots import selection
from pipeline_core.bots.base import get_bot, htf_for, list_bots, reward_risk
from pipeline_core import dedup, sizing
from pipeline_core.config import settings
from pipeline_core.markets import get_universe_tickers
from pipeline_core.services import crypto_data, market_sentiment
from pipeline_core.utils import round_price

logger = logging.getLogger(__name__)

_DISCLAIMER = ("Decision-support research, not investment advice. Intraday crypto is volatile "
               "and shorting carries uncapped risk — signals are live snapshots; always trade "
               "with the given stop and size positions to your risk.")

# A measured bot is disqualified in a regime if its expectancy is below both the pack median
# (relative) and this absolute floor (belt-and-suspenders against a pathologically bad bot).
DISQUALIFY_FLOOR = -0.30   # % net expectancy per trade


def _signal_bots() -> list:
    return [b for b in (get_bot(d["id"]) for d in list_bots()) if b and b.kind == "signal"]


def _regime(df) -> str:
    """Current regime for edge-weighting: trending (ADX high) vs ranging."""
    try:
        adx = float(ind.adx(df["High"], df["Low"], df["Close"], 14)["adx"].iloc[-1])
        return "trend" if adx == adx and adx > bt.TREND_ADX else "range"   # adx==adx: NaN guard
    except Exception:
        return "range"


def _spark(df, points: int = 32) -> list:
    """A compact recent-close series for the idea card's inline sparkline (entry/stop/target are
    drawn over it client-side). Small on purpose — the last `points` closes."""
    try:
        return [round_price(float(v)) for v in df["Close"].astype(float).tail(points).tolist()]
    except Exception:
        return []


def _edge_weight(bot_name: str, tf: str, regime: str, edge: Optional[dict]) -> tuple[float, Optional[float]]:
    """(weight, measured win-rate) for a bot's vote. Weight maps measured expectancy → 0..3
    (≤0 expectancy contributes ~nothing); neutral 1.0 when there's no measured edge yet."""
    st = bt.edge_for(bot_name, tf, regime, edge=edge) if edge else None
    if not st or not st.get("enough"):
        return 1.0, None
    exp = st.get("expectancy_pct", 0.0) or 0.0
    return max(0.0, min(exp / 0.5, 3.0)), st.get("win_rate")


def _eligible_bots(bot_names: list[str], tf: str, regime: str, edge: Optional[dict]) -> set[str]:
    """The subset of bots worth trading in this regime: those with no measured stat yet
    (unproven → kept neutral) plus measured bots whose regime expectancy clears the pack median
    and the absolute floor. Prunes the measurably-worst bots so a known-bad bot/regime combo
    can't generate an idea. No backtest → everyone is eligible."""
    if not edge:
        return set(bot_names)
    measured: dict[str, float] = {}
    for name in bot_names:
        st = bt.edge_for(name, tf, regime, edge=edge)
        if st and st.get("enough"):
            measured[name] = st.get("expectancy_pct", 0.0) or 0.0
    if not measured:
        return set(bot_names)
    threshold = max(DISQUALIFY_FLOOR, statistics.median(list(measured.values())))
    return {name for name in bot_names if name not in measured or measured[name] >= threshold}


def _rank_key(edge: Optional[dict]):
    """Sort key for the idea books (used with reverse=True). With a backtest: edge-backed ideas
    first, then signed measured quality (least-bad first), then edge_score / conviction / R.
    Without a backtest: raw conviction then reward:risk, as before."""
    if edge:
        return lambda i: (bool(i["edge_backed"]), i["quality"] if i["quality"] is not None else -9e9,
                          i["edge_score"] or 0, i["conviction"], i["r_multiple"] or 0)
    return lambda i: (i["conviction"], i["r_multiple"] or 0)


def _idea(ticker: str, side: str, sigs: list[dict], tf: str, regime: str, edge: Optional[dict]) -> dict:
    """Blend N agreeing bot signals for one coin/side into a single ranked idea, weighting each
    bot's vote by its measured edge (regime-aware) when a backtest exists."""
    med = lambda k: statistics.median([s[k] for s in sigs])
    entry, stop, target = med("entry"), med("stop"), med("target")
    bots = [s["bot"] for s in sigs]
    weights, win_rates, exps, trade_counts = [], [], [], []
    for s in sigs:
        w, wr = _edge_weight(s["bot"], tf, regime, edge)
        weights.append(w)
        if wr is not None:
            win_rates.append(wr)
        st = bt.edge_for(s["bot"], tf, regime, edge=edge) if edge else None
        if st and st.get("enough"):
            exps.append(st.get("expectancy_pct", 0.0) or 0.0)
            trade_counts.append(int(st.get("trades", 0) or 0))
    edge_score = round(sum(weights), 3)
    # Signed quality (mean measured expectancy of the agreeing bots) ranks least-bad first even
    # when the whole book is negative; edge_backed flags a genuinely positive-edge agreer.
    quality = round(statistics.mean(exps), 3) if exps else None
    edge_backed = any(e > 0 for e in exps)
    return {
        "ticker": ticker, "side": side, "conviction": len(sigs),
        "edge_score": edge_score if edge else None,
        "quality": quality if edge else None,
        "edge_backed": edge_backed if edge else None,
        "est_win_pct": round(statistics.mean(win_rates), 1) if win_rates else None,
        "pooled_trades": sum(trade_counts) if trade_counts else 0,   # sample size behind conf_win_pct
        "regime": regime,
        "price": round_price(entry), "entry": round_price(entry),
        "stop": round_price(stop), "target": round_price(target),
        "r_multiple": reward_risk(side, entry, stop, target),
        "avg_r": round(statistics.mean([s["r_multiple"] for s in sigs if s["r_multiple"] is not None]), 2)
        if any(s["r_multiple"] is not None for s in sigs) else None,
        "bots": bots,
        "reason": f"{len(sigs)} bots agree {side}: " + ", ".join(bots),
        "signals": [{"bot": s["bot"], "reason": s["reason"], "r_multiple": s["r_multiple"]} for s in sigs],
    }


def _stance(alignment: float) -> str:
    """Plain-language read of how live sentiment relates to an idea's side."""
    if alignment >= 0.15:
        return "supportive"
    if alignment <= -0.15:
        return "contradicting"
    return "neutral"


def _sentiment_view(news: Optional[dict], alignment: float) -> dict:
    """Compact sentiment block attached to a shortlisted idea (for the API + why-now card)."""
    return {
        "news_score": round(float(news["score"]), 1) if news and news.get("score") is not None else None,
        "consensus": (news or {}).get("consensus"),
        "method": (news or {}).get("method"),
        "alignment": alignment,          # [-1,+1] support for THIS side (news + Fear & Greed)
        "stance": _stance(alignment),
    }


def _why_selected(idea: dict, fng: Optional[dict]) -> str:
    """One-line rationale for why an idea made the shortlist — the human view of the composite."""
    bits = [f"{idea['conviction']} bot{'s' if idea['conviction'] != 1 else ''} agree {idea['side']}"]
    if idea.get("conf_win_pct") is not None:
        bits.append(f"~{idea['conf_win_pct']}% conf. win")
    if idea.get("quality") is not None:
        bits.append(f"edge {idea['quality']:+}%/trade")
    sv = idea.get("sentiment") or {}
    if sv.get("stance") and sv["stance"] != "neutral":
        news = f"{sv['consensus'].lower()} news" if sv.get("consensus") else "sentiment"
        bits.append(f"{sv['stance']} {news}")
    if fng and not fng.get("stale"):
        bits.append(f"F&G {fng['value']} ({fng['classification']})")
    return " · ".join(bits)


async def _fetch_sentiment(tickers: list[str]) -> tuple[Optional[dict], dict]:
    """Fetch the market Fear & Greed reading once + per-coin news for the candidate `tickers`,
    concurrently and bounded. Every failure degrades to neutral — returns (fng, {ticker: news})."""
    fng = await asyncio.to_thread(market_sentiment.fear_greed)
    sem = asyncio.Semaphore(settings.ingestion_concurrency)

    async def one(tk: str):
        async with sem:
            return tk, await market_sentiment.coin_news(tk)

    news_by_ticker = dict(await asyncio.gather(*(one(t) for t in tickers)))
    return fng, news_by_ticker


async def _build_shortlist(longs: list[dict], shorts: list[dict],
                           use_sentiment: bool, top_k: int, min_score: float) -> dict:
    """Curate the (already ranked + deduped) long/short books into one small best-of shortlist,
    scoring each candidate on the Wilson-adjusted win rate + measured edge + agreement + live
    sentiment (see selection.py). Candidates are COPIES, so the long/short books stay untouched."""
    pool = settings.ideas_candidate_pool
    candidates = [dict(i) for i in longs[:pool]] + [dict(i) for i in shorts[:pool]]
    if not candidates:
        return {"shortlist": [], "fear_greed": None,
                "diagnostics": {"considered": 0, "kept": 0, "cut": 0, "top_k": top_k,
                                "min_score": min_score}, "sizing": sizing.size_book([])}

    fng, news_by_ticker = (await _fetch_sentiment(list({c["ticker"] for c in candidates}))
                           if use_sentiment else (None, {}))
    fng_value = fng["value"] if fng else None

    for c in candidates:
        news = news_by_ticker.get(c["ticker"])
        scored = selection.score_candidate(
            side=c["side"], conviction=c["conviction"], quality=c.get("quality"),
            r_multiple=c.get("r_multiple"), est_win_pct=c.get("est_win_pct"),
            trades=c.get("pooled_trades"),
            news_score=(news.get("score") if news else None), fng_value=fng_value,
        )
        c.update(scored)
        c["sentiment"] = _sentiment_view(news, scored["alignment"])
        c["win_prob"] = scored["conf_win_pct"]     # sizer reads win_prob ahead of est_win_pct
        c["why_selected"] = _why_selected(c, fng)

    shortlist, diagnostics = selection.curate(candidates, top_k=top_k, min_score=min_score)
    return {"shortlist": shortlist, "fear_greed": fng, "diagnostics": diagnostics,
            "sizing": sizing.size_book(shortlist)}


async def intraday_ideas(
    timeframe: str = "15m",
    universe: str = "Top 50",
    bars: int = 300,
    limit: int = 20,
    min_conviction: int = 1,
    strict: bool = False,
    top_k: Optional[int] = None,
    min_score: Optional[float] = None,
    sentiment: bool = True,
) -> dict:
    """Aggregate the signal bots across `universe` on `timeframe` into ranked long + short idea
    books, then curate ONE small **shortlist** of the best setups overall.

    When a backtest exists, only regime-competitive bots contribute (quality over quantity) and
    ideas are ranked by measured quality; `strict=True` keeps only edge-backed ideas (a
    positive-edge bot agrees). No backtest → conviction = raw agreeing-bot count.

    The `shortlist` (long + short mixed, ≤ `top_k`) ranks each candidate on a transparent
    composite — a sample-size-aware (Wilson) win rate + measured edge + agreement + live
    sentiment (per-coin news + market-wide Fear & Greed), the last applied as a soft down-rank.
    `sentiment=False` (or an offline feed) makes the sentiment term neutral; the full `long` /
    `short` books are returned untouched for backward compatibility. See `selection.py`."""
    bots = _signal_bots()
    if timeframe not in {tf for b in bots for tf in b.timeframes}:
        timeframe = "15m"
    needs_htf = any(b.needs_htf for b in bots)
    needs_btc = any(b.needs_btc for b in bots)
    htf = htf_for(timeframe) if needs_htf else None

    edge = bt.load_edge()   # measured per-bot edge (None until a backtest has been run)
    all_names = [b.name for b in bots]
    # Regime-conditional eligibility, computed once (depends only on tf + regime, not the coin).
    eligible = ({r: _eligible_bots(all_names, timeframe, r, edge) for r in ("trend", "range")}
                if edge else None)
    symbols = await asyncio.to_thread(get_universe_tickers, "CRYPTO", universe)
    btc_df = None
    if needs_btc:
        btc_df = await asyncio.to_thread(crypto_data.get_klines, "BTC", htf or timeframe, 200)

    sem = asyncio.Semaphore(settings.ingestion_concurrency)

    async def load(sym: str):
        async with sem:
            try:
                df = await asyncio.to_thread(crypto_data.get_klines, sym, timeframe, bars)
                htf_df = await asyncio.to_thread(crypto_data.get_klines, sym, htf, 200) if htf else None
                return sym, df, htf_df
            except Exception as e:
                logger.warning(f"ideas load failed for {sym}: {e}")
                return sym, None, None

    loaded = await asyncio.gather(*(load(s) for s in symbols))

    longs, shorts, scanned = [], [], 0
    for sym, df, htf_df in loaded:
        if df is None or df.empty:
            continue
        scanned += 1
        regime = _regime(df)
        spark = _spark(df)                               # recent closes for the idea-card sparkline
        allow = eligible[regime] if eligible else None   # None → every bot eligible (no backtest)
        ctx = {"htf_df": htf_df, "btc_df": btc_df, "timeframe": timeframe}
        by_side: dict[str, list[dict]] = {"long": [], "short": []}
        for bot in bots:
            if allow is not None and bot.name not in allow:   # not competitive in this regime
                continue
            try:
                sig = bot.evaluate(df, ctx)
            except Exception as e:
                logger.warning(f"{bot.id} failed for {sym}: {e}")
                continue
            side = sig.get("side")
            if side in ("long", "short") and sig["entry"] and sig["stop"] and sig["target"]:
                by_side[side].append({**sig, "bot": bot.name})
        for side, sigs in by_side.items():
            if len(sigs) < min_conviction:
                continue
            idea = _idea(sym, side, sigs, timeframe, regime, edge)
            if strict and edge and not idea["edge_backed"]:   # hide exploratory / no-edge ideas
                continue
            idea["spark"] = spark                              # recent closes for the card sparkline
            (longs if side == "long" else shorts).append(idea)

    key = _rank_key(edge)
    longs.sort(key=key, reverse=True)
    shorts.sort(key=key, reverse=True)

    # De-duplicate correlated ideas BEFORE sizing (and before counts/notes): alts that move
    # together — e.g. all following BTC — would otherwise stack the book with one bet repeated
    # under many tickers. Keep the best-ranked idea per correlated cluster so conviction and size
    # reflect independent setups. Uses the OHLCV already loaded above → no extra I/O.
    frames_by_sym = {sym: df for sym, df, _ in loaded if df is not None and not df.empty}
    clusters = dedup.cluster_symbols(frames_by_sym)
    longs, long_dd = dedup.dedup_book(longs, clusters)
    shorts, short_dd = dedup.dedup_book(shorts, clusters)
    n_clusters = (max(clusters.values()) + 1) if clusters else 0

    # Size the *shown* books: capped ¼-Kelly risk % + portfolio allocation % per idea, from each
    # idea's measured win-rate (est_win_pct) and reward:risk. Mutates the idea dicts in place.
    long_book, short_book = longs[:limit], shorts[:limit]
    long_sizing = sizing.size_book(long_book)
    short_sizing = sizing.size_book(short_book)

    # Curate ONE small best-of shortlist across both books — fewer, higher-conviction ideas,
    # ranked by a Wilson-adjusted win rate + measured edge + agreement + live sentiment.
    top_k = settings.ideas_top_k if top_k is None else top_k
    min_score = settings.ideas_min_score if min_score is None else min_score
    use_sentiment = bool(sentiment and settings.ideas_sentiment_enabled)
    curated = await _build_shortlist(longs, shorts, use_sentiment, top_k, min_score)
    shortlist, fng = curated["shortlist"], curated["fear_greed"]

    eb_long = sum(1 for i in longs if i["edge_backed"]) if edge else 0
    eb_short = sum(1 for i in shorts if i["edge_backed"]) if edge else 0
    if edge:
        n_elig = len(eligible["trend"] | eligible["range"])
        explor = len(longs) + len(shorts) - eb_long - eb_short
        note = (f"{len(longs)} long / {len(shorts)} short ideas from the {n_elig}/{len(bots)} "
                f"regime-competitive bots on {timeframe} ({scanned} coins) — "
                f"{eb_long + eb_short} edge-backed, {explor} exploratory"
                + (" (exploratory hidden by strict)" if strict else "")
                + (f", gated by the {htf} trend + BTC regime" if htf else "")
                + ". Ranked by measured quality.")
    else:
        note = (f"{len(longs)} long / {len(shorts)} short ideas across {len(bots)} bots on "
                f"{timeframe} ({scanned} coins scanned)"
                + (f", gated by the {htf} trend + BTC regime" if htf else "")
                + ", ranked by conviction (run the bot backtest to select by measured edge)")
    dropped = long_dd["dropped"] + short_dd["dropped"]
    if dropped:
        note += (f" · {dropped} correlated duplicate{'s' if dropped != 1 else ''} collapsed "
                 f"(≥{settings.dedup_corr_threshold:g} return corr)")
    # Lead with the curated shortlist — the primary, actionable output.
    n_sl = len(shortlist)
    fng_txt = (f", Fear & Greed {fng['value']} ({fng['classification']})"
               if use_sentiment and fng and not fng.get("stale") else "")
    note = (f"Top {n_sl} shortlisted from {len(longs) + len(shorts)} ideas"
            + (", sentiment-weighted" if use_sentiment else "") + fng_txt
            + f". {note}")
    return {
        "timeframe": timeframe, "htf": htf, "universe": universe,
        "as_of": datetime.now(timezone.utc).isoformat(), "scanned": scanned,
        "bots": all_names, "min_conviction": min_conviction, "strict": strict,
        "edge_weighted": bool(edge), "edge_as_of": edge.get("as_of") if edge else None,
        "counts": {"long": len(longs), "short": len(shorts)},
        "edge_backed": {"long": eb_long, "short": eb_short},
        "dedup": {"long": long_dd, "short": short_dd, "clusters": n_clusters,
                  "threshold": settings.dedup_corr_threshold},
        # Curated best-of: fewer, higher-conviction, sentiment-aware ideas (the headline output).
        "shortlist": shortlist,
        "shortlist_sizing": curated["sizing"],
        "selection": {"top_k": top_k, "min_score": min_score, "sentiment": use_sentiment,
                      "weights": selection.score_weights(), **curated["diagnostics"]},
        "fear_greed": fng,
        "long": long_book, "short": short_book,
        "sizing": {"params": sizing.sizing_params(), "long": long_sizing, "short": short_sizing},
        "note": note,
        "disclaimer": _DISCLAIMER,
    }
