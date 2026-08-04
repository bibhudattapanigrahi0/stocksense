"""Sentiment agent — market-aware news query + market-agnostic lexicon fallback.
Network-free (pure query construction + lexicon scoring)."""

from pipeline_core.agents import sentiment


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
