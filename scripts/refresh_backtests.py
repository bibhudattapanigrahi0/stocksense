#!/usr/bin/env python3
"""Refresh the full-universe strategy backtests (data/backtests/latest_<MARKET>.json) per market.

The app's auto-refresh loop (app.py `_auto_refresh_loop`) keeps SIGNALS fresh every 10 min, but NOT
the heavy 10y strategy metrics (win-rate / Sharpe / drawdown) that the board and the realized-edge
gate read — those only update on a full `run_full_backtest`. This script runs that per market. It is
driven weekly by the `com.stocksense.backtests` launchd agent, and is runnable ad-hoc:

    env/bin/python scripts/refresh_backtests.py              # all markets
    env/bin/python scripts/refresh_backtests.py US CRYPTO    # just these

Note on overlap: `run_full_backtest` rewrites latest_<MARKET>.json wholesale. If the app is running,
its 10-min signal refresh also writes that file. The weekly agent fires Sunday 03:37 (market closed,
signals static) so the overlap is negligible and self-corrects on the next loop cycle. For an ad-hoc
refresh during active hours, prefer running it while the app is stopped (as Phase-0 setup does).
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Make the project importable regardless of CWD (the launchd agent sets WorkingDirectory, but ad-hoc
# runs may not), so `import pipeline_core` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline_core.backtest import runner          # noqa: E402
from pipeline_core.markets import MARKETS           # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("refresh_backtests")

DEFAULT_MARKETS = ["US", "IN", "CRYPTO", "METALS"]


async def _refresh(requested: list[str]) -> int:
    known = {k.upper() for k in MARKETS}
    failures = 0
    for mk in [m.upper() for m in requested]:
        if mk not in known:
            log.warning("skip unknown market %s (known: %s)", mk, sorted(known))
            continue
        if runner.job_state.get("running"):
            log.warning("a backtest job is already running in-process; skipping %s", mk)
            continue
        log.info("full backtest: %s …", mk)
        try:
            res = await runner.run_full_backtest(market=mk)
            log.info("done %s: %d strategies, benchmark=%s",
                     mk, len(res.get("strategies", [])), res.get("benchmark_label"))
        except Exception:
            failures += 1
            log.exception("full backtest FAILED for %s", mk)
    return failures


if __name__ == "__main__":
    requested = [a for a in sys.argv[1:] if not a.startswith("-")] or DEFAULT_MARKETS
    rc = asyncio.run(_refresh(requested))
    log.info("refresh complete (%d failure(s))", rc)
    sys.exit(1 if rc else 0)
