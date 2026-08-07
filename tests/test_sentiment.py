"""Sentiment agent — market-aware news query + market-agnostic lexicon fallback.
Network-free (pure query construction + lexicon scoring)."""

from pipeline_core.agents import sentiment
from pipeline_core.services import market_sentiment as ms
# The real coin_news (the autouse conftest fixture replaces the module attribute with a stub; this
# alias, bound at import, still points at the actual implementation so its cache can be tested).
from pipeline_core.services.market_sentiment import coin_news as real_coin_news


def test_news_query_is_market_aware():
    q_in, loc_in = sentiment._news_query("RELIANCE", "Reliance Industries", "IN")
    assert "IN:NSE" in q_in and "gl=IN" in loc_in

    q_us, loc_us = sentiment._news_query("AAPL", "Apple", "US")
    assert "gl=US" in loc_us and "IN:NSE" not in q_us

    q_c, loc_c = sentiment._news_query("BTC", "Bitcoin", "CRYPTO")
    assert "crypto" in q_c.lower() and "gl=US" in loc_c

    q_m, _ = sentiment._news_query("GC=F", "Gold", "METALS")
    assert "price news" in q_m.lower()


def test_news_query_unknown_market_defaults_to_in():
    _, loc = sentiment._news_query("X", "X", "FOO")
    assert "gl=IN" in loc


def test_lexicon_fallback_is_market_agnostic():
    agent = sentiment.SentimentAnalysisAgent()
    bull = agent._evaluate_sentiment_lexicon([{"title": "Stock surges to record high on strong profit beat"}])
    bear = agent._evaluate_sentiment_lexicon([{"title": "Shares plunge on fraud probe and downgrade"}])
    assert bull["consensus"] == "Bullish" and bull["score"] > 50
    assert bear["consensus"] == "Bearish" and bear["score"] < 50


# ── coin_news result cache (keeps the idea board off the per-coin LLM on every load) ──


async def test_coin_news_result_cached(monkeypatch, tmp_path):
    import diskcache
    monkeypatch.setattr(ms, "_coin_news_cache", diskcache.Cache(str(tmp_path / "cnc")))
    calls = {"n": 0}

    async def fake_analyze(ticker, name=None, market="CRYPTO"):
        calls["n"] += 1
        return {"score": 72.0, "consensus": "Bullish", "summary": "up", "method": "lexicon"}

    monkeypatch.setattr(sentiment, "analyze_ticker_sentiment", fake_analyze)
    r1 = await real_coin_news("FOO")
    r2 = await real_coin_news("FOO")
    assert r1["score"] == 72.0 and r2["consensus"] == "Bullish"
    assert calls["n"] == 1                 # second load served from cache — no repeat agent/LLM call


async def test_coin_news_does_not_cache_failure(monkeypatch, tmp_path):
    import diskcache
    monkeypatch.setattr(ms, "_coin_news_cache", diskcache.Cache(str(tmp_path / "cnc2")))
    calls = {"n": 0}

    async def boom(ticker, name=None, market="CRYPTO"):
        calls["n"] += 1
        raise RuntimeError("news source down")

    monkeypatch.setattr(sentiment, "analyze_ticker_sentiment", boom)
    r1 = await real_coin_news("BAR")
    r2 = await real_coin_news("BAR")
    assert r1["method"] == "unavailable"   # degraded to a neutral reading
    assert calls["n"] == 2                 # a transient failure is NOT cached → retried next load
