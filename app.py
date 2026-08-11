"""
StockSense FastAPI app — REST + WebSocket surface over pipeline_core.

Run:  uvicorn app:app --reload --port 8000

Endpoints:
  POST /api/v1/pipeline/run            start a run (background) → {run_id}
  POST /api/v1/pipeline/run/sync       run to completion → full results
  GET  /api/v1/pipeline/runs           recent run history
  GET  /api/v1/pipeline/{run_id}       status + results
  WS   /api/v1/pipeline/{run_id}/ws    live progress events
  POST /api/v1/screen                  screen-only (no deep analysis)
  GET  /api/v1/stocks/search?q=        ticker/name search
  GET  /api/v1/stocks/{ticker}         detailed profile (NSE-enriched)
  GET  /api/v1/stocks/{ticker}/analyze four-agent deep dive
  GET  /api/v1/reports/{filename}      markdown report text
  GET  /api/v1/screens                 saved presets
  POST /api/v1/screens                 save a preset
  GET  /api/v1/health                  config/health info
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pipeline_core.agents.data_ingestion import DataIngestionAgent
from pipeline_core.agents.orchestrator import OrchestratorAgent, active_runs
from pipeline_core.agents.screener import ScreenerAgent
from pipeline_core.config import REPORTS_DIR, settings
from pipeline_core.models.schemas import (
    PipelineRequest,
    PipelineRun,
    SavedScreen,
    StockDetail,
)
from pipeline_core.portfolio import (
    PortfolioError,
    construct_portfolio,
    rebalance_portfolio,
)
from pipeline_core.portfolio import risk as pf_risk
from pipeline_core.portfolio import themes as pf_themes
from pipeline_core.portfolio.models import PortfolioBasis, PortfolioSpec, WeightingScheme
from pipeline_core import adherence, alerts, paper
from pipeline_core.services import auth, llm_service, market_data, run_store

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("stocksense.api")


async def _auto_refresh_loop() -> None:
    """Keep the Trade Ideas board fresh: every `auto_refresh_interval_min`, fast-refresh signals
    for each configured market that's in session and has a prior backtest — so new ideas surface
    even when nobody is viewing. Runs for the life of the server."""
    from pipeline_core.backtest import runner

    interval = max(1, settings.auto_refresh_interval_min) * 60
    markets = [m.strip().upper() for m in settings.auto_refresh_markets.split(",") if m.strip()]
    logger.info("Auto idea-refresh: every %d min for %s", settings.auto_refresh_interval_min, markets)
    while True:
        try:
            await asyncio.sleep(interval)
            for mk in markets:
                if not runner._market_active(mk) or runner.load_latest(mk) is None:
                    continue
                try:
                    res = await runner.refresh_current_signals(mk)
                    if res.get("updated"):
                        logger.info("Auto-refresh %s: %s buys, +%s tracked",
                                    mk, res.get("total_buys"), res.get("added_tracked"))
                except Exception as e:
                    logger.warning("Auto-refresh failed for %s: %s", mk, e)
                # Evaluate open ideas forward so any that hit their stop / target / time-stop are
                # CLOSED on schedule — even when nobody opens the tracker. Otherwise a setup that
                # should have timed out lingers as a "carried" board idea and crowds out fresh
                # signals (open ideas were accumulating unbounded). Persists closures in place.
                try:
                    st = await runner.tracked_ideas_status(mk)
                    logger.info("Idea lifecycle %s: %d open, %d closed",
                                mk, len(st.get("open", [])), len(st.get("closed", [])))
                except Exception as e:
                    logger.warning("Idea lifecycle eval failed for %s: %s", mk, e)
                # Evaluate alert triggers off the freshly-refreshed state (idea closures, watch
                # levels, new ideas for watched tickers). Persisted + de-duplicated by fingerprint.
                try:
                    summary = await alerts.evaluate_market(mk)
                    if summary.get("emitted"):
                        logger.info("Alerts %s: %d new %s", mk, summary["emitted"], summary["by_type"])
                except Exception as e:
                    logger.warning("Alert evaluation failed for %s: %s", mk, e)
                # Weekly re-sync of a deployed basket portfolio (the live 'rebalance'): idempotent,
                # cost-basis-preserving, and a no-op unless one exists and is due.
                try:
                    redep = await runner.maybe_redeploy_basket(mk)
                    if redep and redep.get("deployed"):
                        logger.info("Basket portfolio re-synced for %s (%s positions)",
                                    mk, (redep.get("basket") or {}).get("count"))
                except Exception as e:
                    logger.warning("Basket re-deploy failed for %s: %s", mk, e)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Auto-refresh loop error: %s", e)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Touch the store first so the schema + one-time per-user migration run before the
    # background loop (or any request) reads or writes it.
    await run_store.get_owner()
    task = asyncio.create_task(_auto_refresh_loop()) if settings.auto_refresh_enabled else None
    try:
        yield
    finally:
        if task:
            task.cancel()


app = FastAPI(
    title="StockSense",
    description="Agentic Stock Filter and Multi-Agent Analysis Pipeline for Indian Markets (NSE/BSE)",
    version="2.0.0",
    lifespan=_lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Authentication ─────────────────────────────────────────────────
# Server-side sessions in an httpOnly cookie (opaque, revocable). Everything under
# /api/ requires a session except a few open prefixes and the SPA/static assets.
# Per-user data is scoped by the logged-in account; the MCP server + background loop
# act as the "owner" (see pipeline_core/services/run_store.py).

_SESSION_COOKIE = "ss_session"
_OPEN_API_PREFIXES = ("/api/v1/health", "/api/v1/markets", "/api/v1/auth")


def _public_user(u: dict) -> dict:
    return {k: u.get(k) for k in ("id", "email", "display_name", "is_owner", "prefs", "created_at")}


async def _issue_session(response: Response, user_id: str) -> None:
    token = await run_store.create_session(user_id, ttl_days=settings.auth_session_ttl_days)
    response.set_cookie(
        _SESSION_COOKIE, token, max_age=settings.auth_session_ttl_days * 86400,
        httponly=True, samesite="lax", secure=settings.auth_cookie_secure, path="/",
    )


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    path = request.url.path
    if (request.method == "OPTIONS" or not path.startswith("/api/")
            or any(path.startswith(p) for p in _OPEN_API_PREFIXES)):
        return await call_next(request)
    user = await run_store.get_session_user(request.cookies.get(_SESSION_COOKIE, ""))
    if user is None:
        return JSONResponse({"detail": "Authentication required"}, status_code=401)
    request.state.user = user
    return await call_next(request)


def current_user(request: Request) -> dict:
    """The logged-in user (populated by _auth_gate). 401 if somehow unset."""
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


class SignupRequest(BaseModel):
    email: str
    password: str
    display_name: Optional[str] = None


class LoginRequest(BaseModel):
    email: str
    password: str


class ProfileUpdate(BaseModel):
    display_name: Optional[str] = None
    password: Optional[str] = None
    prefs: Optional[dict] = None


@app.get("/api/v1/auth/me")
async def auth_me(request: Request):
    user = await run_store.get_session_user(request.cookies.get(_SESSION_COOKIE, ""))
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"user": _public_user(user), "allow_signup": settings.auth_allow_signup}


@app.post("/api/v1/auth/signup")
async def auth_signup(req: SignupRequest, response: Response):
    email = auth.normalize_email(req.email)
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
    if len(req.password or "") < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    if await run_store.get_user_by_email(email):
        raise HTTPException(status_code=409, detail="That email is already registered — try logging in.")
    owner = await run_store.get_owner()
    pw_hash = auth.hash_password(req.password)
    if not owner["has_password"]:
        # First account adopts the owner (which holds any pre-accounts data).
        user = await run_store.claim_owner(email, pw_hash, req.display_name or "")
    elif settings.auth_allow_signup:
        user = await run_store.create_user(email, pw_hash, req.display_name or "")
    else:
        raise HTTPException(status_code=403, detail="New sign-ups are disabled on this server.")
    await _issue_session(response, user["id"])
    return {"user": _public_user(user)}


@app.post("/api/v1/auth/login")
async def auth_login_endpoint(req: LoginRequest, response: Response):
    user = await run_store.auth_login(req.email, req.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Incorrect email or password.")
    await _issue_session(response, user["id"])
    return {"user": _public_user(user)}


@app.post("/api/v1/auth/logout")
async def auth_logout(request: Request, response: Response):
    token = request.cookies.get(_SESSION_COOKIE)
    if token:
        await run_store.delete_session(token)
    response.delete_cookie(_SESSION_COOKIE, path="/")
    return {"ok": True}


@app.patch("/api/v1/auth/me")
async def auth_update_me(req: ProfileUpdate, user: dict = Depends(current_user)):
    """Update the signed-in user's profile and/or personalization preferences."""
    if req.password is not None and len(req.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    if req.prefs is not None:
        await run_store.update_user_prefs(user["id"], req.prefs)
    if req.display_name is not None or req.password is not None:
        pw_hash = auth.hash_password(req.password) if req.password else None
        await run_store.update_user_profile(user["id"], display_name=req.display_name, password_hash=pw_hash)
    fresh = await run_store.get_user_by_id(user["id"])
    return {"user": _public_user(fresh)}


orchestrator = OrchestratorAgent()
ingestion = DataIngestionAgent()
screener = ScreenerAgent()

# run_id -> list of subscribed websockets
_ws_subscribers: dict[str, list[WebSocket]] = {}
# run_id -> background task
_run_tasks: dict[str, asyncio.Task] = {}


# ── WebSocket plumbing ─────────────────────────────────────────────


async def _broadcast(run_id: str, message: dict) -> None:
    sockets = _ws_subscribers.get(run_id, [])
    stale = []
    for ws in sockets:
        try:
            await ws.send_json(message)
        except Exception:
            stale.append(ws)
    for ws in stale:
        sockets.remove(ws)


def _progress_cb(run_id: str):
    def cb(stage: str, pct: float, count: int) -> None:
        message = {
            "pipeline_id": run_id,
            "stage": stage,
            "progress_pct": round(pct, 1),
            "candidates_found": count,
        }
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_broadcast(run_id, message))
        except RuntimeError:
            pass
    return cb


# ── Pipeline endpoints ─────────────────────────────────────────────


@app.post("/api/v1/pipeline/run")
async def start_pipeline(request: PipelineRequest):
    """Start a pipeline run in the background; poll or subscribe for progress."""
    run_id = str(uuid.uuid4())

    async def _tracked() -> None:
        run = await orchestrator.execute(request, progress_cb=_progress_cb(run_id), run_id=run_id)
        await _broadcast(run_id, {
            "pipeline_id": run_id,
            "stage": run.current_stage,
            "progress_pct": 100.0,
            "status": run.status.value,
            "candidates_found": run.candidates_found,
        })

    _run_tasks[run_id] = asyncio.create_task(_tracked())
    return {"run_id": run_id, "status": "running", "ws": f"/api/v1/pipeline/{run_id}/ws"}


@app.post("/api/v1/pipeline/run/sync", response_model=PipelineRun)
async def run_pipeline_sync(request: PipelineRequest):
    """Run the pipeline to completion and return the full run (long request)."""
    return await orchestrator.execute(request)


@app.get("/api/v1/pipeline/runs")
async def list_runs(limit: int = Query(20, ge=1, le=100)):
    runs = await run_store.list_runs(limit=limit)
    return [
        {
            "id": r.id,
            "created_at": r.created_at.isoformat(),
            "status": r.status.value,
            "query": r.query,
            "candidates_found": r.candidates_found,
            "report_files": r.report_files,
            "error": r.error,
        }
        for r in runs
    ]


@app.get("/api/v1/pipeline/{run_id}", response_model=PipelineRun)
async def get_pipeline(run_id: str):
    run = active_runs.get(run_id) or await run_store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.websocket("/api/v1/pipeline/{run_id}/ws")
async def pipeline_ws(websocket: WebSocket, run_id: str):
    # The HTTP auth middleware doesn't run for websockets — check the session cookie here.
    if await run_store.get_session_user(websocket.cookies.get(_SESSION_COOKIE, "")) is None:
        await websocket.close(code=1008)   # policy violation (unauthenticated)
        return
    await websocket.accept()
    _ws_subscribers.setdefault(run_id, []).append(websocket)

    run = active_runs.get(run_id) or await run_store.get_run(run_id)
    if run is not None:
        await websocket.send_json({
            "pipeline_id": run_id,
            "stage": run.current_stage,
            "progress_pct": run.progress_pct,
            "status": run.status.value,
            "candidates_found": run.candidates_found,
        })
    try:
        while True:
            await websocket.receive_text()  # keepalive
    except WebSocketDisconnect:
        pass
    finally:
        if run_id in _ws_subscribers and websocket in _ws_subscribers[run_id]:
            _ws_subscribers[run_id].remove(websocket)


# ── Screening endpoint ─────────────────────────────────────────────


@app.post("/api/v1/screen")
async def screen_only(request: PipelineRequest):
    """Screen-only path: filters → ranked candidates, no deep analysis."""
    try:
        results = await orchestrator.screen_only(request)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {
        "filters_applied": request.filters.model_dump(exclude_unset=True),
        "count": len(results),
        "candidates": [r.model_dump() for r in results],
        "llm_mode": llm_service.llm_mode(),
    }


# ── Stock endpoints ────────────────────────────────────────────────


@app.get("/api/v1/stocks/search")
async def search_symbols(q: str = Query(..., min_length=1), market: str = Query("IN")):
    try:
        results = await asyncio.to_thread(market_data.search_stocks, q, 10, market)
        return [{"symbol": s["ticker"], "name": s["name"], "sector": s.get("sector")} for s in results]
    except Exception as e:
        logger.error(f"Search error: {e}")
        raise HTTPException(status_code=500, detail="Stock search lookup failed")


@app.get("/api/v1/stocks/suggest")
async def suggest_symbols(q: str = Query("", max_length=40), limit: int = 10, market: str = Query("IN")):
    """Fast ticker autocomplete: substring match over the selected market's default
    (cached) universe. Prefix matches rank first. No per-ticker network."""
    from pipeline_core.markets import get_market

    q = q.strip().upper()
    limit = max(1, min(limit, 25))
    m = get_market(market)
    try:
        universe = await asyncio.to_thread(market_data.get_index_tickers, m.default_universe, m.key)
    except Exception:
        universe = []
    symbols = set(universe)
    if m.key == "IN":
        symbols |= set(market_data.NIFTY_50_TICKERS)
    symbols = sorted(symbols)
    if not q:
        matches = symbols[:limit]
    else:
        starts = [t for t in symbols if t.startswith(q)]
        contains = [t for t in symbols if q in t and not t.startswith(q)]
        matches = (starts + contains)[:limit]
    return [{"symbol": t} for t in matches]


@app.get("/api/v1/stocks/{ticker}", response_model=StockDetail)
async def get_stock_details(ticker: str, market: str = Query("IN")):
    symbol = ticker.upper().strip()
    info = await ingestion.fetch_stock_data(symbol, enrich_nse=True, market=market)
    if "error" in info:
        raise HTTPException(status_code=404, detail=f"Stock {symbol} not found")

    return StockDetail(
        ticker=info.get("ticker", symbol),
        name=info.get("name", symbol),
        sector=info.get("sector"),
        industry=info.get("industry"),
        exchange=info.get("exchange"),
        market_cap_cr=info.get("market_cap_cr"),
        price=info.get("price"),
        change_pct=info.get("change_pct"),
        day_high=info.get("day_high"),
        day_low=info.get("day_low"),
        week_52_high=info.get("week_52_high"),
        week_52_low=info.get("week_52_low"),
        volume=info.get("volume"),
        avg_volume=info.get("avg_volume"),
        pe_ratio=info.get("pe_ratio"),
        pb_ratio=info.get("pb_ratio"),
        ev_ebitda=info.get("ev_ebitda"),
        roe=round(info["roe"] * 100, 2) if info.get("roe") is not None else None,
        opm=round(info["operating_margins"] * 100, 2) if info.get("operating_margins") is not None else None,
        npm=round(info["profit_margins"] * 100, 2) if info.get("profit_margins") is not None else None,
        debt_to_equity=round(info["debt_to_equity"] / 100, 2) if info.get("debt_to_equity") is not None else None,
        dividend_yield=round(info["dividend_yield"] * 100, 2) if info.get("dividend_yield") is not None else None,
        eps=info.get("eps"),
        book_value=info.get("book_value"),
        free_cash_flow=info.get("free_cash_flow"),
        promoter_holding=info.get("promoter_holding"),
        promoter_pledge=info.get("promoter_pledge"),
        fii_holding=info.get("fii_holding"),
        dii_holding=info.get("dii_holding"),
        delivery_pct=info.get("delivery_pct"),
    )


@app.get("/api/v1/stocks/{ticker}/analyze")
async def analyze_stock(ticker: str, depth: str = "standard", market: str = Query("IN")):
    result = await orchestrator.analyze_ticker(ticker.upper().strip(), depth, market=market)
    if "error" in result and "composite_score" not in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


# ── Reports ────────────────────────────────────────────────────────


@app.get("/api/v1/reports")
async def list_reports(limit: int = Query(50, ge=1, le=200)):
    """List generated report files, newest first."""
    files = sorted(REPORTS_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {"filename": f.name, "modified": f.stat().st_mtime, "size_bytes": f.stat().st_size}
        for f in files[:limit]
    ]


@app.get("/api/v1/reports/{filename}", response_class=PlainTextResponse)
async def get_report_file(filename: str):
    clean = Path(filename).name
    if not clean.endswith(".md"):
        raise HTTPException(status_code=400, detail="Only markdown reports (.md) can be read")
    path = REPORTS_DIR / clean
    if not path.exists():
        raise HTTPException(status_code=404, detail="Report not found")
    return path.read_text(encoding="utf-8")


@app.post("/api/v1/stocks/{ticker}/report")
async def generate_stock_report(ticker: str, market: str = Query("IN")):
    """Run the analysis fresh and render a markdown report."""
    symbol = ticker.upper().strip()
    analysis = await orchestrator.analyze_ticker(symbol, "standard", market=market)
    if "error" in analysis:
        raise HTTPException(status_code=404, detail=analysis["error"])

    from pipeline_core.agents.report_generator import ReportGeneratorAgent

    report_md = await ReportGeneratorAgent().generate_markdown_report(
        symbol=symbol,
        name=analysis.get("name", symbol),
        fundamental=analysis["evidence"]["fundamental"],
        technical=analysis["evidence"]["technical"],
        sentiment=analysis["evidence"]["sentiment"],
        risk=analysis["evidence"]["risk"],
        composite_score=analysis.get("composite_score") or 50.0,
    )
    filename = f"{symbol}_adhoc.md"
    (REPORTS_DIR / filename).write_text(report_md, encoding="utf-8")
    return {"ticker": symbol, "filename": filename, "markdown": report_md}


# ── Curated strategies ─────────────────────────────────────────────


@app.get("/api/v1/markets")
async def list_markets():
    """Registry of supported markets (India, US, metals, crypto) — drives the
    frontend market switcher: labels, selectable universes, units, benchmark
    labels, and which filter groups apply per market."""
    from pipeline_core.markets import list_markets as _list

    return {"markets": _list()}


@app.get("/api/v1/strategies")
async def list_strategies(market: str = Query("IN")):
    """Curated filter strategies for one-click pipeline runs (market-scoped)."""
    from pipeline_core.strategies import list_strategies as _list

    return {"strategies": _list(market)}


# ── Backtesting (Strategy Lab) ─────────────────────────────────────


class BacktestRunRequest(BaseModel):
    market: str = "IN"
    index: Optional[str] = None   # None → the market's default universe
    years: int = Field(default=10, ge=2, le=15)
    cost_roundtrip_bps: float = Field(default=25.0, ge=0, le=200)
    strategy_ids: Optional[list[str]] = None  # None → all strategies


@app.post("/api/v1/backtest/run")
async def start_backtest(req: BacktestRunRequest):
    """Start a full-universe backtest of the strategy library (background job)."""
    from pipeline_core.backtest import runner

    if runner.job_state["running"]:
        raise HTTPException(status_code=409, detail="A backtest job is already running")

    async def _job() -> None:
        try:
            await runner.run_full_backtest(
                index=req.index,
                years=req.years,
                cost_roundtrip_bps=req.cost_roundtrip_bps,
                strategy_ids=req.strategy_ids,
                market=req.market,
            )
        except Exception as e:
            logger.error(f"Backtest job failed: {e}")

    asyncio.create_task(_job())
    return {"status": "started", "strategies": len(req.strategy_ids) if req.strategy_ids else "all"}


@app.get("/api/v1/backtest/status")
async def backtest_status(market: str = Query("IN")):
    """Current/last backtest job state and (if present) last completed run info for the market."""
    from pipeline_core.backtest import runner

    latest = runner.load_latest(market)
    return {
        "job": runner.job_state,
        "market": market,
        "last_run_at": latest.get("run_at") if latest else None,
        "last_run_market": latest.get("market") if latest else None,
        "last_run_index": latest.get("index") if latest else None,
        "last_run_benchmark_label": latest.get("benchmark_label") if latest else None,
        "last_run_years": latest.get("years") if latest else None,
        "elapsed_seconds": latest.get("elapsed_seconds") if latest else None,
    }


@app.post("/api/v1/backtest/signals/refresh")
async def refresh_signals(market: str = Query("IN")):
    """FAST idea generation: re-scan every strategy's current buy signals on fresh prices and
    fold them into the board — without the full 10y metrics recompute (seconds, not minutes).
    New ideas are auto-tracked. Requires a prior full backtest for the market. Runs inline
    (it's quick); 409 if a full backtest is in progress."""
    from pipeline_core.backtest import runner

    if runner.job_state["running"]:
        raise HTTPException(status_code=409, detail="A full backtest is running")
    return await runner.refresh_current_signals(market)


# ── Strategy discovery (generate → validate OOS → keep survivors, staged) ──


@app.post("/api/v1/backtest/discover")
async def start_discovery(
    market: str = Query("IN"),
    n_candidates: int = Query(200, ge=10, le=2000),
    years: int = Query(10, ge=2, le=15),
    split: float = Query(0.70, ge=0.4, le=0.9),
    top_k: int = Query(25, ge=1, le=200),
    seed: Optional[int] = Query(None),
):
    """Discover new strategies: sample candidates from a parametric grammar, rank them in-sample,
    validate the top-K out-of-sample, and keep only survivors (both-slice gate + degradation
    guard). Background job; a few minutes. Survivors stage in a catalog until promoted. 409 if one
    is already running."""
    from pipeline_core.backtest import discovery

    if discovery.discovery_job_state["running"]:
        raise HTTPException(status_code=409, detail="A discovery job is already running")

    async def _job() -> None:
        try:
            await discovery.run_discovery(market=market, n_candidates=n_candidates, years=years,
                                          split=split, top_k=top_k, seed=seed)
        except Exception as e:
            logger.error(f"Discovery job failed: {e}")

    asyncio.create_task(_job())
    return {"status": "started", "market": market, "n_candidates": n_candidates}


@app.get("/api/v1/backtest/discover/status")
async def discovery_status(market: str = Query("IN")):
    """Discovery job state + a summary of the last completed run for the market."""
    from pipeline_core.backtest import discovery

    return discovery.status(market)


@app.get("/api/v1/backtest/discovered")
async def list_discovered(market: str = Query("IN")):
    """The Discovered catalog for a market — survivors with in-sample + out-of-sample metrics,
    the rule spec, and promote flags."""
    from pipeline_core.backtest import discovery

    return discovery.list_discovered(market)


@app.post("/api/v1/backtest/discovered/{strategy_id}/promote")
async def promote_discovered(strategy_id: str, market: str = Query("IN"), promoted: bool = Query(True)):
    """Promote (or un-promote) a discovered strategy. Promoted strategies join the curated library
    on the next backtest/Trade-Ideas run and resolve like built-ins."""
    from pipeline_core.backtest import discovery

    res = discovery.promote_discovered(strategy_id, market, promoted)
    if "error" in res:
        raise HTTPException(status_code=404, detail=res["error"])
    return res


@app.delete("/api/v1/backtest/discovered/{strategy_id}")
async def delete_discovered(strategy_id: str, market: str = Query("IN")):
    """Remove a discovered strategy from the catalog."""
    from pipeline_core.backtest import discovery

    res = discovery.delete_discovered(strategy_id, market)
    if "error" in res:
        raise HTTPException(status_code=404, detail=res["error"])
    return res


@app.get("/api/v1/backtest/signals/refresh/status")
async def refresh_signals_status():
    """State of the fast signals-only refresh job (separate from the full backtest)."""
    from pipeline_core.backtest import runner

    return {"job": runner.signals_job_state}


@app.get("/api/v1/backtest/leaderboard")
async def backtest_leaderboard(sort_by: str = "sharpe", market: str = Query("IN")):
    """
    All strategies with metrics + signal counts for the market, sortable by sharpe,
    win_rate, cagr, max_drawdown, profit_factor, expectancy, calmar, alpha, sortino,
    total_return.
    """
    from pipeline_core.backtest import runner

    board = runner.leaderboard(sort_by, market)
    if board is None:
        return {"strategies": [], "run_at": None, "disclaimer": None, "market": market}
    return board


@app.get("/api/v1/backtest/strategy/{strategy_id}")
async def backtest_strategy_detail(strategy_id: str, market: str = Query("IN")):
    """Full detail for one strategy: metrics, equity curve, signals, sample trades."""
    from pipeline_core.backtest import runner

    detail = runner.strategy_detail(strategy_id, market=market)
    if detail is not None:
        return detail
    if runner.load_latest(market) is None:
        raise HTTPException(status_code=404, detail=f"No {market} backtest results yet — run one first")
    raise HTTPException(status_code=404, detail=f"Strategy '{strategy_id}' not in latest {market} results")


@app.get("/api/v1/backtest/signals")
async def backtest_signals(market: str = Query("IN")):
    """Current BUY/SELL/HOLDING signals for every strategy (compact summary)."""
    from pipeline_core.backtest import runner

    summary = runner.signals_summary(market=market)
    return summary if summary is not None else {"strategies": [], "run_at": None, "market": market}


@app.get("/api/v1/backtest/strategies")
async def backtest_strategies(market: str = Query("IN")):
    """Static catalog of the strategy library (available even before a run), including any
    user-promoted discovered strategies for the market."""
    from pipeline_core.backtest import runner

    strategies = runner.strategy_library(market)
    return {"count": len(strategies), "strategies": strategies}


@app.get("/api/v1/backtest/ideas")
async def backtest_ideas(max_per_bucket: int = 10, market: str = Query("IN"),
                         live: bool = Query(True)):
    """Cross-strategy trade ideas for the market, grouped into short / medium / long term
    buckets, ranked by conviction (how many strategies agree). Each bucket capped at
    `max_per_bucket` candidates (default 10, hard max 10). With `live=true` (default) each idea is priced
    with a fresh live quote (adds live_price/live_change_pct/zone/live_levels without changing
    the generated plan). Empty until a backtest has run.

    A top-level `regime` block gives the market's risk-on/off tide (benchmark 200-DMA + a vol/macro
    factor — VIX, DXY, or crypto Fear & Greed): regime-aligned longs rank first (each idea tagged
    `regime_fit`), and in risk-off the book is sized down (`regime_size_factor` per idea + on the
    bucket sizing). A neutral/offline reading leaves ordering and sizing unchanged."""
    from pipeline_core.backtest import runner

    ideas = runner.horizon_ideas(max_per_bucket, market)
    if ideas is None:
        return {"horizons": ["short", "medium", "long"], "buckets": {}, "run_at": None, "market": market}
    if live:
        ideas = await runner.enrich_ideas_live(ideas, market)
    return ideas


@app.get("/api/v1/backtest/ideas/tracked")
async def backtest_ideas_tracked(market: str = Query("IN")):
    """Tracked trade ideas for the market, evaluated against the latest price, with a
    winning/losing/stopped-out scoreboard. Prices are fetched live (cached ~15 min)."""
    from pipeline_core.backtest import runner

    return await runner.tracked_ideas_status(market)


@app.post("/api/v1/backtest/ideas/track")
async def backtest_ideas_track(max_per_bucket: int = 10, market: str = Query("IN")):
    """Snapshot the current horizon trade ideas into the tracker so their win/loss can be
    followed over time (idempotent per ticker+horizon+market). Also happens automatically
    after every backtest run."""
    from pipeline_core.backtest import runner

    return await runner.snapshot_current_ideas(max_per_bucket, market)


@app.delete("/api/v1/backtest/ideas/tracked")
async def backtest_ideas_untrack(market: str = Query("IN")):
    """Reset the idea tracker for the market (forget its tracked ideas)."""
    from pipeline_core.backtest import runner

    return runner.clear_tracked(market)


class IdeaActionRequest(BaseModel):
    ticker: str
    horizon: str
    market: str = "IN"
    action: str = "in"           # "in" | "out"
    price: Optional[float] = None


@app.post("/api/v1/backtest/ideas/tracked/action")
async def backtest_idea_action(req: IdeaActionRequest):
    """Record what the user actually did on a tracked idea (I'm-in / I'm-out) — the 'you' side of the
    discipline scorecard. Applies to the most-recent open tracked record for (ticker, horizon, market)."""
    from pipeline_core.backtest import runner

    return runner.mark_idea_action(req.ticker, req.horizon, req.market, req.action, req.price)


@app.get("/api/v1/adherence")
async def adherence_scorecard(market: str = Query("IN"), user: dict = Depends(current_user)):
    """Discipline scorecard — 'system vs you': how the user's actual entries/exits (paper trades +
    explicit I'm-in/out) compare to following each idea's plan to stop/target/expiry."""
    return await adherence.discipline_scorecard(market, user_id=user["id"])


@app.get("/api/v1/backtest/basket")
async def backtest_basket(max_per_bucket: int = 10, market: str = Query("IN")):
    """The curated **Today's Basket** for the market — the full 30-idea board distilled into ONE small,
    deployable best-of (3–5 trades) sized as a single cross-horizon ¼-Kelly book (allocations sum to
    ≤100%). Balanced/risk-adjusted (expected value × confidence × rank), greedily diversified (no two
    correlated names; soft sector/horizon spread), regime-scaled in size, and sticky day-to-day
    (hysteresis) so it's holdable. Also rides along on `/api/v1/backtest/ideas` as `basket`."""
    from pipeline_core.backtest import runner

    ideas = runner.horizon_ideas(max_per_bucket, market)
    if ideas is None:
        return {"enabled": True, "items": [], "market": market,
                "note": f"No {market} backtest yet — run one in the Strategy Lab first."}
    ideas = await runner.enrich_ideas_live(ideas, market)
    return ideas.get("basket") or {"enabled": True, "items": [], "market": market}


@app.get("/api/v1/smart-money")
async def smart_money_signal(symbol: str = Query(..., min_length=1), market: str = Query("IN")):
    """The smart-money / coattail signal for one symbol — who the proven money is and whether they're
    accumulating, normalized across markets: SEC 13F + famed gurus (US), CFTC COT commercials (metals),
    Hyperliquid top-trader positioning (crypto), NSE bulk/block deals (IN). Fields: direction,
    conviction, actors, detail. `available: false` when no source covers the symbol."""
    from pipeline_core.services import smart_money
    sig = await asyncio.to_thread(smart_money.get_signal, market, symbol)
    return sig or {"available": False, "market": market, "symbol": symbol}


@app.get("/api/v1/smart-money/leaders")
async def smart_money_leaders(market: str = Query("IN"), limit: int = Query(10, ge=1, le=50)):
    """The coattail leaders board — who the smart money is buying now: the Hyperliquid top-trader book
    (crypto), the metals whose CFTC commercials are at a positioning extreme (metals), or the top 13F
    QoQ accumulators (US). India bulk deals are per-symbol — use /smart-money?symbol=."""
    from pipeline_core.services import smart_money
    return await asyncio.to_thread(smart_money.get_leaders, market, limit)


@app.get("/api/v1/backtest/coattail-basket")
async def backtest_coattail_basket(max_per_bucket: int = 10, market: str = Query("IN")):
    """The COATTAIL basket — today's curated basket filtered to names the smart money is accumulating
    (SEC 13F+gurus / CFTC commercials / Hyperliquid top traders / NSE bulk buyers). "What to buy that
    the pros are buying too." Read-only; also rides along on `/api/v1/backtest/ideas` as `coattail_basket`."""
    from pipeline_core.backtest import runner

    ideas = runner.horizon_ideas(max_per_bucket, market)
    if ideas is None:
        return {"enabled": True, "items": [], "market": market,
                "note": f"No {market} backtest yet — run one in the Strategy Lab first."}
    ideas = await runner.enrich_ideas_live(ideas, market)
    return ideas.get("coattail_basket") or {"enabled": True, "items": [], "market": market}


@app.post("/api/v1/backtest/basket/track")
async def backtest_basket_track(max_per_bucket: int = 10, market: str = Query("IN")):
    """Commit the current curated basket as the day's anchor and track its members (idempotent). Tagged
    `source=basket` in the tracker. Reuses the idea snapshot, so it also advances the hysteresis anchor."""
    from pipeline_core.backtest import runner

    return await runner.snapshot_current_ideas(max_per_bucket, market)


@app.post("/api/v1/backtest/basket/deploy")
async def backtest_basket_deploy(max_per_bucket: int = 10, market: str = Query("IN")):
    """Deploy the curated basket as a saved, NAV-tracked **Portfolio** — track it as a unit (NAV curve /
    P&L / rebalance / AI review, all in the Portfolios tab) rather than as individual ideas. Idempotent
    and cost-basis-preserving (re-deploying re-syncs the same portfolio, keeping entry prices on holds)."""
    from pipeline_core.backtest import runner

    return await runner.deploy_basket(market, max_per_bucket=max_per_bucket)


@app.post("/api/v1/backtest/basket/backtest/run")
async def basket_backtest_run(market: str = Query("IN"), years: int = Query(None)):
    """Backtest the basket CONSTRUCTION method — a held-basket, weekly-rebalanced historical sim with
    anti-lookahead → equity curve + CAGR/Sharpe/maxDD/alpha vs benchmark. Background job; 409 if one is
    already running."""
    from pipeline_core.backtest import basket_backtest as bb

    if bb.job_state["running"]:
        raise HTTPException(status_code=409, detail="A basket backtest is already running")

    async def _job() -> None:
        try:
            await bb.run_basket_backtest(market, years)
        except Exception as e:
            logger.error(f"Basket backtest job failed: {e}")

    asyncio.create_task(_job())
    return {"status": "started", "market": market}


@app.get("/api/v1/backtest/basket/backtest/status")
async def basket_backtest_status():
    """Progress of the running/last basket backtest job."""
    from pipeline_core.backtest import basket_backtest as bb

    return bb.status()


@app.get("/api/v1/backtest/basket/backtest")
async def basket_backtest_result(market: str = Query("IN")):
    """The last basket-method backtest result for the market (equity curve + metrics). Empty until run."""
    from pipeline_core.backtest import basket_backtest as bb

    return bb.load_result(market) or {"ran": False, "market": market,
                                      "note": f"No basket backtest for {market} yet — run one."}


# ── My Hold Basket (committed, conviction-held book with material-change rebalance) ────


@app.get("/api/v1/backtest/hold-basket")
async def hold_basket_get(market: str = Query("IN")):
    """The committed **Hold Basket**: its live holdings + P&L + the (usually empty) MATERIAL rebalance plan
    (EXIT on stop/target/thesis-break, ADD only for a dramatically-better idea). If nothing is committed
    yet, returns a `preview` of today's hold-worthy picks to commit."""
    from pipeline_core.backtest import runner

    return await runner.hold_basket_status(market)


@app.post("/api/v1/backtest/hold-basket/commit")
async def hold_basket_commit(market: str = Query("IN")):
    """Commit today's hold basket as your book — persisted (entry prices + cost basis) and deployed as a
    NAV-tracked portfolio. It then stays put until you apply a material change."""
    from pipeline_core.backtest import runner

    return await runner.commit_hold_basket(market)


@app.post("/api/v1/backtest/hold-basket/apply")
async def hold_basket_apply(market: str = Query("IN")):
    """Apply the reviewed material rebalance (held − exits + material adds), keeping cost-basis on
    survivors, and re-deploy. No-op when there's nothing material to change."""
    from pipeline_core.backtest import runner

    return await runner.apply_hold_rebalance(market)


@app.delete("/api/v1/backtest/hold-basket")
async def hold_basket_uncommit(market: str = Query("IN")):
    """Forget the committed hold basket (the NAV-tracked portfolio is left intact)."""
    from pipeline_core.backtest import runner

    return runner.uncommit_hold_basket(market)


# ── Multi-bagger screen (fundamentals-first long-horizon ideas) ────


@app.get("/api/v1/backtest/multibaggers")
async def get_multibaggers(market: str = Query("IN"), top_n: int = Query(20)):
    """The market's ranked multi-bagger candidates — a fundamentals-first screen for high-growth,
    high-quality, smaller-cap compounders. Loads the last persisted screen instantly; if none exists
    yet it computes one inline. Equity markets only (others return an empty, note-bearing payload)."""
    from pipeline_core import multibagger

    cached = multibagger.load_latest_multibaggers(market)
    if cached is not None:
        cached["candidates"] = cached.get("candidates", [])[:top_n]
        cached["count"] = len(cached["candidates"])
        return cached
    return await multibagger.screen_multibaggers(market, top_n)


@app.post("/api/v1/backtest/multibaggers/refresh")
async def refresh_multibaggers(market: str = Query("IN"), top_n: int = Query(20)):
    """Recompute the market's multi-bagger screen in the background (poll `.../multibaggers/status`
    for progress). 409 if a screen is already running; gated to equity markets."""
    from pipeline_core import multibagger
    from pipeline_core.markets import get_market

    if not get_market(market).has_fundamentals:
        raise HTTPException(status_code=400, detail="Multi-bagger screening is for equity markets only")
    if multibagger.job_state["running"]:
        raise HTTPException(status_code=409, detail="A multi-bagger screen is already running")

    async def _job() -> None:
        try:
            await multibagger.screen_multibaggers(market, top_n)
        except Exception as e:
            logger.error(f"Multi-bagger screen failed: {e}")

    asyncio.create_task(_job())
    return {"status": "started", "market": market}


@app.get("/api/v1/backtest/multibaggers/status")
async def multibaggers_status(market: str = Query("IN")):
    """Current/last multi-bagger screen job state + when the persisted screen was last run."""
    from pipeline_core import multibagger

    latest = multibagger.load_latest_multibaggers(market)
    return {
        "job": multibagger.job_state,
        "market": market,
        "last_run_at": latest.get("run_at") if latest else None,
        "universe": latest.get("universe") if latest else None,
        "count": latest.get("count") if latest else None,
    }


@app.post("/api/v1/backtest/multibaggers/track")
async def track_multibaggers(market: str = Query("IN"), ticker: Optional[str] = Query(None),
                             top_n: Optional[int] = Query(None)):
    """Track multi-bagger candidate(s) so their bagger multiple can be followed over time. `ticker`
    given → track just that one; else track the current top `top_n` from the persisted screen.
    Idempotent per ticker+market."""
    from pipeline_core import multibagger

    return multibagger.track_multibaggers(market, ticker, top_n)


@app.get("/api/v1/backtest/multibaggers/tracked")
async def tracked_multibaggers(market: str = Query("IN")):
    """Live status of tracked multi-baggers — each re-priced to a bagger multiple + return-since-entry
    + milestone + thesis drift, plus a basket scoreboard and unrealised equity curve."""
    from pipeline_core import multibagger

    return await multibagger.tracked_multibaggers_status(market)


@app.delete("/api/v1/backtest/multibaggers/tracked")
async def untrack_multibaggers(market: str = Query("IN"), ticker: Optional[str] = Query(None)):
    """Stop tracking one multi-bagger (ticker) or reset the market's whole tracked set."""
    from pipeline_core import multibagger

    return multibagger.untrack_multibaggers(market, ticker)


# ── AI analyst (advisory LLM layer, rules-only fallback) ───────────


class IdeaAIRequest(BaseModel):
    idea: dict = Field(description="A trade-idea dict (a horizon candidate or a tracked idea)")
    market: str = "IN"
    with_sentiment: bool = True


@app.post("/api/v1/ai/idea")
async def ai_idea(req: IdeaAIRequest):
    """LLM analyst read of one trade idea — thesis, buy/watch/avoid verdict, key risk,
    invalidation, and news sentiment. Advisory only; degrades to a deterministic template
    when no LLM is configured (`source: rules`)."""
    from pipeline_core.backtest import idea_ai

    return await idea_ai.analyze_idea(req.idea, req.market, req.with_sentiment)


@app.get("/api/v1/backtest/ideas/postmortem")
async def ai_ideas_postmortem(market: str = Query("IN")):
    """AI post-mortem over the market's realized trade-idea record (what worked + lessons).
    Advisory; degrades to a deterministic template with no LLM configured."""
    from pipeline_core.backtest import idea_ai, runner

    status = await runner.tracked_ideas_status(market)
    review = await idea_ai.analyze_track_record(status)
    return {"market": (market or "IN").upper(), "review": review,
            "closed_scoreboard": status.get("closed_scoreboard", {})}


@app.get("/api/v1/backtest/calibration")
async def backtest_calibration(market: str = Query("IN")):
    """Win-rate calibration for a market from the realized trade ledger: an isotonic advertised→
    realized reliability curve (with Brier before/after) plus a realized-win-rate table by
    conviction. Too few closed trades → `active: false` (ideas keep their advertised win-rate)."""
    from pipeline_core import calibration

    return calibration.build_from_ledger(market)


@app.get("/api/v1/backtest/fusion")
async def backtest_fusion(market: str = Query("IN")):
    """Learned idea-win fusion diagnostics: whether a logistic model over conviction + advertised
    win-rate + Sharpe beats the base rate out-of-sample (walk-forward), with log-loss before/after.
    Active models feed position sizing. Too little data / no OOS edge → active:false."""
    from pipeline_core import fusion

    return fusion.public_view(fusion.build_idea_model(market))


@app.post("/api/v1/backtest/robustness")
async def backtest_robustness(strategy_id: str = Query(...), market: str = Query("IN"),
                              windows: int = Query(4, ge=2, le=10),
                              n_boot: int = Query(2000, ge=200, le=10000)):
    """Walk-forward + Monte-Carlo robustness for one strategy: sequential out-of-sample windows
    (consistent edge vs one lucky regime) plus a trade bootstrap (return/drawdown confidence bands,
    probability of loss & ruin). Loads history and runs inline (a few seconds warm)."""
    from pipeline_core.backtest import runner

    return await runner.run_robustness(strategy_id, market=market, windows=windows, n_boot=n_boot)


# ── Saved screens ──────────────────────────────────────────────────


@app.get("/api/v1/screens")
async def list_saved_screens(user: dict = Depends(current_user)):
    screens = await run_store.list_screens(user_id=user["id"])
    return [s.model_dump() for s in screens]


@app.post("/api/v1/screens", status_code=201)
async def create_saved_screen(screen: SavedScreen, user: dict = Depends(current_user)):
    return (await run_store.save_screen(screen, user_id=user["id"])).model_dump()


@app.delete("/api/v1/screens/{name}")
async def delete_saved_screen(name: str, user: dict = Depends(current_user)):
    if not await run_store.delete_screen(name, user_id=user["id"]):
        raise HTTPException(status_code=404, detail="Saved screen not found")
    return {"deleted": True}


# ── Portfolios ─────────────────────────────────────────────────────
# NOTE: literal paths (/config, /themes, /construct) are declared BEFORE the
# /{portfolio_id} route so FastAPI doesn't match them as an id.


@app.get("/api/v1/portfolios/config")
async def portfolios_config():
    """Risk-profile presets, weighting schemes, and basis options (builder metadata)."""
    return {
        "risk_profiles": pf_risk.list_risk_profiles(),
        "weighting_schemes": [w.value for w in WeightingScheme],
        "bases": [b.value for b in PortfolioBasis],
    }


@app.get("/api/v1/portfolios/themes")
async def portfolios_themes():
    """Curated thematic baskets (AI, green energy, EV, defense, …)."""
    return {"themes": pf_themes.list_themes()}


@app.post("/api/v1/portfolios/construct")
async def portfolios_construct(spec: PortfolioSpec):
    """Construct a portfolio from a spec and return it (preview — not saved)."""
    try:
        pf = await construct_portfolio(spec, screen_fn=orchestrator.screen_only)
    except PortfolioError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return pf.model_dump()


@app.post("/api/v1/portfolios", status_code=201)
async def portfolios_save(spec: PortfolioSpec, user: dict = Depends(current_user)):
    """Construct a portfolio and persist it."""
    try:
        pf = await construct_portfolio(spec, screen_fn=orchestrator.screen_only)
    except PortfolioError as e:
        raise HTTPException(status_code=400, detail=str(e))
    saved = await run_store.save_portfolio(pf, user_id=user["id"])
    return saved.model_dump()


@app.get("/api/v1/portfolios")
async def portfolios_list(user: dict = Depends(current_user)):
    """All saved portfolios, newest first."""
    return [p.model_dump() for p in await run_store.list_portfolios(user_id=user["id"])]


@app.get("/api/v1/portfolios/{portfolio_id}")
async def portfolios_get(portfolio_id: str, user: dict = Depends(current_user)):
    """One saved portfolio by id or name, with its latest mark-to-market snapshot.
    Records a fresh snapshot on load (at most once per calendar day)."""
    from datetime import datetime, timezone

    from pipeline_core.portfolio.track import snapshot_portfolio

    pf = await run_store.get_portfolio(portfolio_id, user_id=user["id"])
    if pf is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")

    latest = await run_store.latest_portfolio_snapshot(pf.id) if pf.id else None
    today = datetime.now(timezone.utc).date().isoformat()
    if pf.id and (not latest or (latest.get("ts", "")[:10] != today)):
        try:
            latest = await snapshot_portfolio(pf)   # auto-snapshot, deduped to once/day
        except Exception as e:
            logger.warning(f"Portfolio auto-snapshot failed for {pf.id}: {e}")
    return {**pf.model_dump(), "latest_snapshot": latest}


@app.post("/api/v1/portfolios/{portfolio_id}/snapshot")
async def portfolios_snapshot(portfolio_id: str, user: dict = Depends(current_user)):
    """Record a mark-to-market snapshot now (prices every position live)."""
    from pipeline_core.portfolio.track import snapshot_portfolio

    pf = await run_store.get_portfolio(portfolio_id, user_id=user["id"])
    if pf is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return await snapshot_portfolio(pf)


@app.get("/api/v1/portfolios/{portfolio_id}/history")
async def portfolios_history(portfolio_id: str, user: dict = Depends(current_user)):
    """Mark-to-market history (NAV / return time series) for a portfolio."""
    pf = await run_store.get_portfolio(portfolio_id, user_id=user["id"])
    if pf is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    snaps = await run_store.list_portfolio_snapshots(pf.id) if pf.id else []
    return {"portfolio_id": pf.id, "name": pf.name, "snapshots": snaps}


@app.post("/api/v1/portfolios/{portfolio_id}/rebalance")
async def portfolios_rebalance(portfolio_id: str, user: dict = Depends(current_user)):
    """Reconstruct the target from the saved spec and return buy/trim/exit actions."""
    pf = await run_store.get_portfolio(portfolio_id, user_id=user["id"])
    if pf is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    try:
        result = await rebalance_portfolio(pf, screen_fn=orchestrator.screen_only)
    except PortfolioError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result.model_dump()


@app.post("/api/v1/portfolios/ai")
async def portfolios_ai(pf: dict):
    """AI read of a portfolio (saved or preview) — a plain-language construction/risk review
    with strengths and severity-tagged risk notes. Advisory only; never re-weights the basket.
    Degrades to a deterministic template with no LLM configured (`source: rules`)."""
    from pipeline_core.portfolio import ai as pf_ai

    return await pf_ai.review_portfolio(pf)


@app.delete("/api/v1/portfolios/{portfolio_id}")
async def portfolios_delete(portfolio_id: str, user: dict = Depends(current_user)):
    if not await run_store.delete_portfolio(portfolio_id, user_id=user["id"]):
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return {"deleted": True}


# ── Trading bots ────────────────────────────────────────────────────


@app.get("/api/v1/bots")
async def bots_list():
    """Registry of available bots (signal + basket) with their supported timeframes."""
    from pipeline_core.bots.base import list_bots

    return {"bots": list_bots()}


@app.get("/api/v1/bots/ideas")
async def bot_intraday_ideas(
    timeframe: str = Query("4h"),
    universe: str = Query("Top 50"),
    limit: int = Query(20, ge=1, le=50),
    min_conviction: int = Query(1, ge=1, le=5),
    strict: bool = Query(True),
    top_k: int = Query(5, ge=1, le=20),
    min_score: float = Query(0.0, ge=0.0, le=1.0),
    sentiment: bool = Query(True),
):
    """Intraday crypto Trade Ideas — the signal bots aggregated on `timeframe` into ranked
    **long** and **short** idea books, plus a curated **`shortlist`** (≤`top_k`, long+short
    mixed) of the best setups overall, scored on a Wilson-adjusted win rate + measured edge +
    agreement + live sentiment (per-coin news + market-wide `fear_greed`). `sentiment=false`
    makes the sentiment term neutral; `min_score` drops weak shortlist ideas. When a backtest
    exists only regime-competitive bots contribute and ideas rank by measured quality;
    `strict=true` keeps only edge-backed ideas. timeframe ∈ 5m/10m/15m/30m/1h/4h."""
    from pipeline_core.bots import ideas as bot_ideas

    return await bot_ideas.intraday_ideas(timeframe=timeframe, universe=universe,
                                          limit=limit, min_conviction=min_conviction, strict=strict,
                                          top_k=top_k, min_score=min_score, sentiment=sentiment)


@app.post("/api/v1/bots/backtest/run")
async def bot_backtest_run(
    timeframes: str = Query("15m", description="Comma-separated: 5m,15m,1h …"),
    universe: str = Query("Top 15"),
    bars: int = Query(1500, ge=300, le=5000),
):
    """Backtest the intraday signal bots (triple-barrier walk-forward) → per-bot edge stats.
    Background job; modest defaults take a few minutes. 409 if one is already running."""
    from pipeline_core.bots import backtest as bt

    if bt.intraday_job_state["running"]:
        raise HTTPException(status_code=409, detail="An intraday backtest is already running")
    tfs = tuple(t.strip() for t in timeframes.split(",") if t.strip()) or ("15m",)

    async def _job() -> None:
        try:
            await bt.run_intraday_backtest(timeframes=tfs, universe=universe, bars=bars)
        except Exception as e:
            logger.error(f"Intraday backtest job failed: {e}")

    asyncio.create_task(_job())
    return {"status": "started", "timeframes": list(tfs), "universe": universe, "bars": bars}


@app.get("/api/v1/bots/backtest/status")
async def bot_backtest_status():
    """Intraday-backtest job state + a summary of the last completed run."""
    from pipeline_core.bots import backtest as bt

    return bt.status()


@app.get("/api/v1/bots/backtest/edge")
async def bot_backtest_edge():
    """The measured edge table (per bot × timeframe, with regime/side breakdowns)."""
    from pipeline_core.bots import backtest as bt

    edge = bt.load_edge()
    return edge or {"bots": {}, "note": "No intraday backtest has been run yet."}


@app.get("/api/v1/bots/{bot_id}/scan")
async def bot_scan(
    bot_id: str,
    timeframe: str = Query("15m"),
    universe: str = Query("Top 50"),
    limit: int = Query(25, ge=1, le=50),
):
    """Run a signal bot across the crypto universe on `timeframe` → coins signaling long
    now (entry / stop / target / R). timeframe ∈ 5m/10m/15m/30m/1h/4h."""
    from pipeline_core.bots import scan as bot_scan_mod

    result = await bot_scan_mod.scan(bot_id, timeframe=timeframe, universe=universe, limit=limit)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.get("/api/v1/bots/crypto-momentum")
async def bot_crypto_momentum(
    top_n: int = Query(10, ge=1, le=25),
    universe: str = Query("Top 50"),
    ai: bool = Query(False, description="Attach an LLM risk review (briefing/conviction/flags)"),
    apply_ai_risk: bool = Query(False, description="Apply the AI de-risk overlay (down-weight flagged names to cash)"),
):
    """Crypto Momentum Bot signal: today's target basket (BTC-regime gated, top-N
    momentum, inverse-vol weighted), or all-cash when BTC is below its trend. `ai` adds an
    LLM risk review (rules-only template when no LLM); `apply_ai_risk` applies its overlay."""
    from pipeline_core.bots import ai as bot_ai
    from pipeline_core.bots import crypto_momentum

    signal = await crypto_momentum.generate_signal(top_n=top_n, universe=universe)
    if ai or apply_ai_risk:
        signal = await bot_ai.annotate(signal, apply=apply_ai_risk)
    return signal


@app.post("/api/v1/bots/crypto-momentum/deploy")
async def bot_crypto_momentum_deploy(
    top_n: int = Query(10, ge=1, le=25),
    universe: str = Query("Top 50"),
    name: str = Query("Crypto Momentum Bot"),
    ai: bool = Query(False),
    apply_ai_risk: bool = Query(False),
):
    """Run the bot and materialize the basket as a saved, NAV-tracked portfolio."""
    from pipeline_core.bots import crypto_momentum

    return await crypto_momentum.deploy(top_n=top_n, universe=universe, name=name,
                                        ai=ai, apply_ai_risk=apply_ai_risk)


# ── Watchlist, Alerts & Digest ─────────────────────────────────────


class WatchRequest(BaseModel):
    ticker: str
    market: str = Field(default="IN")
    note: str = Field(default="")
    level_above: Optional[float] = None
    level_below: Optional[float] = None


class AlertsReadRequest(BaseModel):
    ids: Optional[list[str]] = None
    market: Optional[str] = None


@app.get("/api/v1/watchlist")
async def watchlist_list(market: Optional[str] = Query(None), user: dict = Depends(current_user)):
    return {"items": await run_store.list_watch(market, user_id=user["id"])}


@app.post("/api/v1/watchlist")
async def watchlist_add(req: WatchRequest, user: dict = Depends(current_user)):
    return await run_store.add_watch(req.model_dump(), user_id=user["id"])


@app.delete("/api/v1/watchlist/{watch_id}")
async def watchlist_delete(watch_id: str, user: dict = Depends(current_user)):
    if not await run_store.delete_watch(watch_id, user_id=user["id"]):
        raise HTTPException(status_code=404, detail="Watch item not found")
    return {"deleted": True}


@app.get("/api/v1/alerts")
async def alerts_list(market: Optional[str] = Query(None), unread_only: bool = Query(False),
                      limit: int = Query(50, ge=1, le=200), user: dict = Depends(current_user)):
    return {
        "alerts": await run_store.list_alerts(market, unread_only=unread_only, limit=limit, user_id=user["id"]),
        "unread": await run_store.unread_count(market, user_id=user["id"]),
    }


@app.post("/api/v1/alerts/read")
async def alerts_read(req: AlertsReadRequest, user: dict = Depends(current_user)):
    return {"marked": await run_store.mark_alerts_read(req.ids, req.market, user_id=user["id"])}


@app.delete("/api/v1/alerts")
async def alerts_clear(market: Optional[str] = Query(None), user: dict = Depends(current_user)):
    return {"cleared": await run_store.clear_alerts(market, user_id=user["id"])}


@app.post("/api/v1/alerts/evaluate")
async def alerts_evaluate(market: str = Query("IN"), user: dict = Depends(current_user)):
    """Force an immediate alert-trigger evaluation (otherwise runs on the auto-refresh loop)."""
    return await alerts.evaluate_market(market, user_id=user["id"])


@app.get("/api/v1/digest")
async def digest(market: str = Query("IN"), hours: int = Query(24, ge=1, le=168),
                 user: dict = Depends(current_user)):
    """Morning brief: what changed for a market in the last `hours` (ideas closed, alerts, opens)."""
    return await alerts.build_digest(market, hours=hours, user_id=user["id"])


# ── Paper trading ──────────────────────────────────────────────────


class PaperOpenRequest(BaseModel):
    ticker: str
    side: str = Field(default="long")
    market: str = Field(default="IN")
    alloc_pct: Optional[float] = None
    qty: Optional[float] = None
    stop: Optional[float] = None
    target: Optional[float] = None
    price: Optional[float] = None
    source: str = Field(default="manual")


@app.get("/api/v1/paper")
async def paper_get(market: str = Query("IN"), user: dict = Depends(current_user)):
    """The paper account for a market, marked to live quotes (auto-closes stop/target hits)."""
    return await paper.mark_account(market, user_id=user["id"])


@app.post("/api/v1/paper/open")
async def paper_open(req: PaperOpenRequest, user: dict = Depends(current_user)):
    """Open a paper position (sized by alloc_pct of book equity, or explicit qty)."""
    return await paper.open_idea(req.market, req.ticker, req.side, qty=req.qty, alloc_pct=req.alloc_pct,
                                 stop=req.stop, target=req.target, price=req.price, source=req.source,
                                 user_id=user["id"])


@app.post("/api/v1/paper/close")
async def paper_close(position_id: str = Query(...), market: str = Query("IN"),
                      user: dict = Depends(current_user)):
    """Close one paper position at the live price."""
    return await paper.close_idea(market, position_id, user_id=user["id"])


@app.post("/api/v1/paper/reset")
async def paper_reset(market: str = Query("IN"), start_balance: float = Query(100000.0, gt=0),
                      user: dict = Depends(current_user)):
    """Reset the market's paper account to a fresh starting balance."""
    return await paper.reset_account(market, start_balance, user_id=user["id"])


# ── Health ─────────────────────────────────────────────────────────


@app.get("/api/v1/health")
async def health():
    return {
        "app": settings.app_name,
        "version": app.version,
        "llm_mode": llm_service.llm_mode(),
        "default_index": settings.default_index,
    }


@app.get("/api/v1/health/providers")
async def health_providers():
    """Per-data-provider health (fallback-chain sources): call counts, success rate,
    last error, and latency — so a degrading source (rate-limited Binance, flaky NSE) is visible."""
    from pipeline_core.services.providers import provider_health

    return {"providers": provider_health()}


# ── Dashboard (static SPA) ─────────────────────────────────────────

STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/", include_in_schema=False)
async def dashboard():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
