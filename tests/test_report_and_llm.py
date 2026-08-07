"""Report rendering — including the agy NameError regression case —
and LLM JSON extraction/parsing robustness."""

import json

import pytest

from pipeline_core.agents.report_generator import ReportGeneratorAgent
from pipeline_core.services import llm_service


def _fundamental_with_concerns() -> dict:
    """The exact shape that crashed agy's generator (concerns non-empty)."""
    return {
        "score": 62.0,
        "strengths": ["High Return on Equity (ROE: 24.0%)"],
        "concerns": ["Rich valuation: High P/E ratio (58.0x)", "Elevated debt-to-equity ratio (1.8x)"],
        "valuation_status": "Overvalued",
        "roce": 0.21,
        "piotroski_score": 7,
        "interest_coverage": 6.2,
        "moat_assessment": "Strong brand and distribution moat.",
    }


def _technical() -> dict:
    return {
        "score": 71.0,
        "signal": "BUY",
        "rsi": 58.0,
        "sma_50": 1200.0,
        "sma_200": 1100.0,
        "week_52_high": 1400.0,
        "week_52_low": 900.0,
        "pct_from_52w_high": -7.1,
        "triggers": ["Above 50-DMA (bullish short term)", "MACD above signal line"],
    }


def _sentiment() -> dict:
    return {
        "score": 65.0,
        "consensus": "Bullish",
        "summary": "Headlines lean positive on earnings momentum.",
        "method": "lexicon",
        "articles": [{"title": "TESTCORP beats estimates", "link": "https://x", "source": "MockWire"}],
    }


def _risk() -> dict:
    return {
        "score": 35.0,
        "level": "Low",
        "annual_volatility_pct": 22.0,
        "beta": 0.95,
        "debt_to_equity_ratio": 0.25,
        "risk_flags": ["Elevated price volatility (annual vol: 22.0%)"],
    }


@pytest.mark.asyncio
async def test_report_renders_with_concerns_regression():
    """Regression for agy's report_generator NameError on non-empty concerns."""
    md = await ReportGeneratorAgent().generate_markdown_report(
        symbol="TESTCORP",
        name="Test Corp Ltd",
        fundamental=_fundamental_with_concerns(),
        technical=_technical(),
        sentiment=_sentiment(),
        risk=_risk(),
        composite_score=68.0,
    )
    assert "# Test Corp Ltd (TESTCORP)" in md
    assert "Rich valuation: High P/E ratio (58.0x)" in md  # concerns rendered, not crashed
    assert "Executive Summary" in md
    assert "Final Verdict" in md
    assert "not investment advice" in md  # disclaimer present


@pytest.mark.asyncio
async def test_report_renders_without_llm():
    """No LLM configured → template executive summary, report still complete."""
    assert not llm_service.llm_available()
    md = await ReportGeneratorAgent().generate_markdown_report(
        symbol="TESTCORP",
        name="Test Corp Ltd",
        fundamental=_fundamental_with_concerns(),
        technical=_technical(),
        sentiment=_sentiment(),
        risk=_risk(),
        composite_score=68.0,
    )
    assert "scores 68.0/100" in md  # template fallback summary


@pytest.mark.asyncio
async def test_report_handles_empty_sections():
    md = await ReportGeneratorAgent().generate_markdown_report(
        symbol="SPARSE",
        name="Sparse Data Co",
        fundamental={"score": 50, "strengths": [], "concerns": []},
        technical={"score": 50, "signal": "HOLD"},
        sentiment={"score": 50, "consensus": "Neutral", "articles": []},
        risk={"score": 30, "level": "Low", "risk_flags": []},
        composite_score=50.0,
    )
    assert "None identified" in md
    assert "# Sparse Data Co (SPARSE)" in md


# ── LLM JSON extraction / structured parsing ──────────────────────


def test_extract_json_plain():
    assert llm_service.extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_fenced():
    text = '```json\n{"a": 1, "b": [2, 3]}\n```'
    assert llm_service.extract_json(text) == {"a": 1, "b": [2, 3]}


def test_extract_json_embedded_in_prose():
    text = 'Sure! Here are the filters: {"roe_min": 0.2} Hope that helps.'
    assert llm_service.extract_json(text) == {"roe_min": 0.2}


def test_extract_json_garbage_returns_none():
    assert llm_service.extract_json("not json at all") is None
    assert llm_service.extract_json("") is None
    assert llm_service.extract_json("[1, 2, 3]") is None  # arrays rejected, dict expected


@pytest.mark.asyncio
async def test_nl_parse_returns_empty_without_llm():
    """Rules-only mode: parse is a safe no-op ({} merges over explicit filters)."""
    result = await llm_service.parse_natural_language_query("cheap IT stocks with ROE > 20%")
    assert result == {}


@pytest.mark.asyncio
async def test_structured_output_validates_schema(monkeypatch):
    from pydantic import BaseModel

    class F(BaseModel):
        roe_min: float | None = None

    async def fake_complete(prompt, system_prompt=None, temperature=0.1, max_tokens=1000):
        return json.dumps({"roe_min": 0.2})

    monkeypatch.setattr(llm_service, "llm_available", lambda: True)
    monkeypatch.setattr(llm_service, "llm_complete", fake_complete)

    parsed = await llm_service.llm_structured("give filters", F)
    assert parsed is not None
    assert parsed.roe_min == 0.2


@pytest.mark.asyncio
async def test_structured_output_retries_then_none(monkeypatch):
    from pydantic import BaseModel

    class F(BaseModel):
        roe_min: float

    async def bad_complete(prompt, system_prompt=None, temperature=0.1, max_tokens=1000):
        return "totally not json"

    monkeypatch.setattr(llm_service, "llm_available", lambda: True)
    monkeypatch.setattr(llm_service, "llm_complete", bad_complete)

    parsed = await llm_service.llm_structured("give filters", F, retries=1)
    assert parsed is None


# ── Gemini provider (OpenAI-compatible endpoint, no extra deps) ────


def test_gemini_availability_gated_on_key(monkeypatch):
    monkeypatch.setattr(llm_service.settings, "llm_provider", "gemini")
    monkeypatch.setattr(llm_service.settings, "gemini_api_key", "")
    assert llm_service.llm_available() is False
    assert llm_service.llm_mode() == "rules-only"
    monkeypatch.setattr(llm_service.settings, "gemini_api_key", "AIza-test")
    assert llm_service.llm_available() is True
    assert llm_service.llm_mode() == "gemini"


@pytest.mark.asyncio
async def test_gemini_routes_through_openai_compat(monkeypatch):
    """gemini → _openai_complete with Google base_url/key and a Gemini model
    (the OpenAI-style default is swapped out)."""
    captured = {}

    async def fake_openai(prompt, system_prompt, temperature, max_tokens,
                          *, base_url=None, api_key=None, model=None, extra_params=None):
        captured.update(base_url=base_url, api_key=api_key, model=model)
        return "strong brand moat"

    monkeypatch.setattr(llm_service.settings, "llm_provider", "gemini")
    monkeypatch.setattr(llm_service.settings, "gemini_api_key", "AIza-test")
    monkeypatch.setattr(llm_service.settings, "gemini_base_url", "https://gg/openai/")
    monkeypatch.setattr(llm_service.settings, "llm_model", "gpt-4o-mini")  # OpenAI-style default
    monkeypatch.setattr(llm_service, "_openai_complete", fake_openai)

    out = await llm_service.llm_complete("assess moat", system_prompt="analyst")
    assert out == "strong brand moat"
    assert captured["base_url"] == "https://gg/openai/"
    assert captured["api_key"] == "AIza-test"
    assert captured["model"] == "gemini-2.5-flash"  # swapped off the gpt default


@pytest.mark.asyncio
async def test_gemini_preserves_explicit_model(monkeypatch):
    captured = {}

    async def fake_openai(prompt, system_prompt, temperature, max_tokens,
                          *, base_url=None, api_key=None, model=None, extra_params=None):
        captured["model"] = model
        return "ok"

    monkeypatch.setattr(llm_service.settings, "llm_provider", "gemini")
    monkeypatch.setattr(llm_service.settings, "gemini_api_key", "AIza-test")
    monkeypatch.setattr(llm_service.settings, "llm_model", "gemini-2.5-flash")
    monkeypatch.setattr(llm_service, "_openai_complete", fake_openai)

    await llm_service.llm_complete("x")
    assert captured["model"] == "gemini-2.5-flash"  # explicit Gemini model kept


# ── Circuit breaker ──────────────────────────────────────────────────


async def test_breaker_trips_on_quota_and_short_circuits(monkeypatch):
    """A quota/429 error trips the breaker immediately; further calls short-circuit to the
    rules-only fallback instead of each repeating the slow, doomed provider round-trip."""
    monkeypatch.setattr(llm_service.settings, "llm_provider", "gemini")
    monkeypatch.setattr(llm_service.settings, "gemini_api_key", "AIza-test")
    calls = {"n": 0}

    async def quota_boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("Error code: 429 - RESOURCE_EXHAUSTED quota exceeded")

    monkeypatch.setattr(llm_service, "_dispatch", quota_boom)
    for _ in range(5):
        with pytest.raises(Exception):
            await llm_service.llm_complete("hi")
    assert calls["n"] == 1                 # first call trips; the other four never reach the provider
    assert llm_service._breaker_open()


async def test_breaker_needs_threshold_for_generic_errors(monkeypatch):
    """A non-quota (transient) error only trips after `llm_breaker_threshold` consecutive fails —
    a single network blip shouldn't disable the provider."""
    monkeypatch.setattr(llm_service.settings, "llm_provider", "gemini")
    monkeypatch.setattr(llm_service.settings, "gemini_api_key", "AIza-test")
    monkeypatch.setattr(llm_service.settings, "llm_breaker_threshold", 3)
    calls = {"n": 0}

    async def flaky(*a, **k):
        calls["n"] += 1
        raise RuntimeError("temporary network blip")

    monkeypatch.setattr(llm_service, "_dispatch", flaky)
    for _ in range(6):
        with pytest.raises(Exception):
            await llm_service.llm_complete("hi")
    assert calls["n"] == 3 and llm_service._breaker_open()   # 3 real attempts, then open


async def test_breaker_resets_on_success(monkeypatch):
    monkeypatch.setattr(llm_service.settings, "llm_provider", "gemini")
    monkeypatch.setattr(llm_service.settings, "gemini_api_key", "AIza-test")
    monkeypatch.setattr(llm_service.settings, "llm_breaker_threshold", 3)
    state = {"fail": True}

    async def sometimes(*a, **k):
        if state["fail"]:
            raise RuntimeError("blip")
        return "ok"

    monkeypatch.setattr(llm_service, "_dispatch", sometimes)
    with pytest.raises(Exception):
        await llm_service.llm_complete("hi")     # one failure, below threshold → breaker stays closed
    state["fail"] = False
    assert await llm_service.llm_complete("hi") == "ok"
    assert llm_service._breaker["fails"] == 0    # a healthy response resets the failure counter
