"""
Sentiment Analysis Agent — aggregates recent news headlines and scores
market sentiment. Uses the LLM when configured; otherwise falls back to a
deterministic lexicon scorer so the pipeline never depends on an API key.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse

import diskcache
import feedparser

from pipeline_core.agents.context import StockContext
from pipeline_core.config import CACHE_DIR, settings
from pipeline_core.services import llm_service

logger = logging.getLogger(__name__)

_news_cache = diskcache.Cache(str(CACHE_DIR / "news"))

# Google-News locale (hl/gl/ceid) + query shape per asset class, so US / crypto / metals
# ideas surface relevant headlines instead of India-only results.
_NEWS_LOCALE = {
    "IN": "hl=en-IN&gl=IN&ceid=IN:en",
    "US": "hl=en-US&gl=US&ceid=US:en",
    "METALS": "hl=en-US&gl=US&ceid=US:en",
    "CRYPTO": "hl=en-US&gl=US&ceid=US:en",
}


def _news_query(symbol: str, company_name: str, market: str = "IN") -> tuple[str, str]:
    """(query, locale) for the Google-News RSS search, tuned to the asset class."""
    mk = (market or "IN").upper()
    locale = _NEWS_LOCALE.get(mk, _NEWS_LOCALE["IN"])
    if mk == "IN":
        query = f"{company_name} IN:NSE OR {symbol} stock news"
    elif mk == "US":
        query = f"{company_name} OR {symbol} stock news"
    elif mk == "CRYPTO":
        query = f"{company_name or symbol} {symbol} crypto price news"
    else:  # METALS and any other price-only class
        query = f"{company_name or symbol} price news"
    return query, locale

# Minimal finance lexicon for the rules-only fallback.
_POSITIVE = {
    "surge", "soar", "rally", "rallies", "gain", "gains", "jump", "rise", "rises",
    "record", "profit", "beats", "beat", "growth", "upgrade", "upgraded", "buy",
    "outperform", "strong", "bullish", "dividend", "wins", "win", "expands",
    "expansion", "breakout", "high", "approval", "order", "deal", "partnership",
    "recovery", "boost", "positive", "optimistic", "acquisition", "bonus",
}
_NEGATIVE = {
    "fall", "falls", "fell", "drop", "drops", "decline", "declines", "loss",
    "losses", "miss", "misses", "missed", "downgrade", "downgraded", "sell",
    "weak", "bearish", "concern", "concerns", "risk", "risks", "fraud",
    "investigation", "probe", "penalty", "fine", "debt", "default", "cut",
    "cuts", "layoff", "layoffs", "resignation", "resigns", "plunge", "plunges",
    "crash", "warning", "negative", "lawsuit", "pledge", "low",
}


class SentimentAnalysisAgent:
    """Gauges media sentiment from news headlines in a StockContext."""

    def __init__(self):
        self.name = "Sentiment Analysis Agent"

    async def analyze(self, ctx: StockContext) -> dict:
        logger.info(f"[{self.name}] Analyzing sentiment for {ctx.ticker}")

        articles = ctx.news_articles or await self._fetch_news(ctx.ticker, ctx.name, ctx.market)
        if not articles:
            return {
                "score": 50.0,
                "consensus": "Neutral",
                "summary": "No recent news headlines found on public RSS channels.",
                "articles": [],
            }

        if llm_service.llm_available():
            sentiment = await self._evaluate_sentiment_llm(ctx.ticker, ctx.name, articles)
        else:
            sentiment = self._evaluate_sentiment_lexicon(articles)

        sentiment["articles"] = articles[:5]
        return sentiment

    async def _fetch_news(self, symbol: str, company_name: str, market: str = "IN") -> list[dict]:
        """
        Fetch headlines from Google News RSS. The HTTP fetch is timeout-bounded
        (requests), then parsed locally — feedparser itself has no timeout and
        can hang forever on a filtered network. The query + locale adapt to the
        asset class so US / crypto / metals ideas get relevant headlines too.
        """
        import requests

        cache_key = f"news:{(market or 'IN').upper()}:{symbol}"
        cached = _news_cache.get(cache_key)
        if cached is not None:
            return list(cached)

        query, locale = _news_query(symbol, company_name, market)
        encoded = urllib.parse.quote(query)
        rss_url = f"https://news.google.com/rss/search?q={encoded}&{locale}"

        try:
            response = await asyncio.to_thread(
                requests.get, rss_url, headers={"User-Agent": "StockSense/2.0"}, timeout=15
            )
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            articles = [
                {
                    "title": entry.get("title"),
                    "link": entry.get("link"),
                    "published": entry.get("published"),
                    "source": entry.get("source", {}).get("title", "Google News"),
                }
                for entry in feed.entries[:10]
            ]
            _news_cache.set(cache_key, articles, expire=settings.cache_ttl_news)
            return articles
        except Exception as e:
            logger.error(f"Failed to fetch news feed for {symbol}: {e}")
            return []

    def _evaluate_sentiment_lexicon(self, articles: list[dict]) -> dict:
        """Deterministic headline sentiment: positive/negative word balance."""
        pos = neg = 0
        for article in articles:
            words = {w.strip(".,!?:;\"'()").lower() for w in (article.get("title") or "").split()}
            pos += len(words & _POSITIVE)
            neg += len(words & _NEGATIVE)

        total = pos + neg
        if total == 0:
            score = 50.0
        else:
            # Map net balance to 0–100 with a neutral center of 50.
            score = 50.0 + (pos - neg) / total * 50.0

        if score >= 60:
            consensus = "Bullish"
        elif score <= 40:
            consensus = "Bearish"
        else:
            consensus = "Neutral"

        return {
            "score": round(score, 1),
            "consensus": consensus,
            "summary": (
                f"Lexicon scan of {len(articles)} headlines: "
                f"{pos} positive / {neg} negative signal words. "
                "(Rules-only mode — configure an LLM provider for narrative analysis.)"
            ),
            "method": "lexicon",
        }

    async def _evaluate_sentiment_llm(self, symbol: str, company_name: str, articles: list[dict]) -> dict:
        """LLM narrative sentiment with schema-validated JSON output."""
        from pydantic import BaseModel

        class SentimentOut(BaseModel):
            score: float = 50.0
            consensus: str = "Neutral"
            summary: str = "Consensus is neutral with sparse event-driven updates."

        headlines = "\n".join(
            f"- {a['title']} (Source: {a['source']})" for a in articles[:8]
        )
        parsed = await llm_service.llm_structured(
            prompt=(
                f"Analyze the market sentiment for {company_name} ({symbol}) from these "
                f"recent headlines:\n\n{headlines}\n\n"
                "score: 0.0 (highly bearish) to 100.0 (highly bullish), 50 = neutral.\n"
                "consensus: Bullish | Bearish | Neutral.\n"
                "summary: two sentences explaining the score."
            ),
            schema=SentimentOut,
            system_prompt="You are a brilliant financial sentiment analyst.",
            temperature=0.1,
            max_tokens=300,
        )
        if parsed is None:
            logger.warning(f"[{self.name}] LLM sentiment failed for {symbol}; using lexicon")
            return self._evaluate_sentiment_lexicon(articles)

        return {
            "score": max(0.0, min(100.0, parsed.score)),
            "consensus": parsed.consensus,
            "summary": parsed.summary,
            "method": "llm",
        }


async def analyze_ticker_sentiment(ticker: str, name: str | None = None, market: str = "IN") -> dict:
    """News sentiment for a bare ticker (no full analysis pipeline needed).

    Builds a minimal StockContext and runs the SentimentAnalysisAgent, so any caller
    (trade-idea AI, tracker) gets LLM-or-lexicon sentiment without re-implementing the
    news fetch/scoring. Returns {score, consensus, summary, articles, method}.
    """
    ctx = StockContext(ticker=ticker, market=(market or "IN").upper(),
                       info={"name": name or ticker})
    return await SentimentAnalysisAgent().analyze(ctx)
