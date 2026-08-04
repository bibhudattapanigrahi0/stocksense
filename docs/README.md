# StockSense Documentation

StockSense is an agentic stock **screening + multi-agent analysis** pipeline for Indian
equities (NSE/BSE), plus a **38-strategy backtesting lab** over 10 years of Nifty 500
history. One shared engine (`pipeline_core`) sits behind two thin surfaces: a **FastAPI**
REST/WebSocket app with a dashboard, and an **MCP server** for Claude / any MCP client.

> ⚠️ Outputs are **decision-support research, not investment advice**. Every backtest
> result ships with an explicit bias disclaimer (survivorship, long-only, costs).

---

## Documentation map

| Doc | What's inside |
|---|---|
| [getting-started.md](getting-started.md) | Install, configure, and run the app, the MCP server, the backtester, and the tests. |
| [markets.md](markets.md) | **Multi-asset support** — India, US, metals, crypto: the market registry, per-market data sources/benchmarks/units, and how price-only markets (metals/crypto) differ from equities. |
| [architecture.md](architecture.md) | The shared engine: 8 agents, the 8-stage pipeline, `StockContext`, services, storage, scoring, and the LLM policy. |
| [screening-and-analysis.md](screening-and-analysis.md) | How screening + the four analysis agents work — filters, staged registry, composite scoring, explainability. |
| [backtesting.md](backtesting.md) | **The Strategy Lab**: execution model, metrics, current BUY/SELL/HOLDING signals, data/caching, biases, and how to add a strategy. |
| [strategies.md](strategies.md) | Catalog of all **35 backtest strategies** and the **10 curated screening strategies**. |
| [portfolios.md](portfolios.md) | **Portfolio management**: construct risk-shaped, theme-tilted baskets from the strategies — risk presets, themes, weighting, rebalance, persistence, REST + MCP. |
| [indicators.md](indicators.md) | Every technical indicator (`pipeline_core/agents/indicators.py`) with math and conventions. |
| [api-reference.md](api-reference.md) | Full REST API — every endpoint, params, and response shape. |
| [mcp-tools.md](mcp-tools.md) | Full MCP tool + resource reference. |
| [configuration.md](configuration.md) | Every setting / environment variable, cache TTLs, weights, data paths. |

---

## "I want to…"

| Goal | Go to |
|---|---|
| Screen US stocks, metals, or crypto | [markets.md](markets.md) |
| Run the dashboard locally | [getting-started.md → Run the REST app](getting-started.md#run-the-rest-api--dashboard) |
| Wire it into Claude Desktop | [getting-started.md → Run the MCP server](getting-started.md#run-the-mcp-server) |
| Understand how a screen is scored | [screening-and-analysis.md](screening-and-analysis.md) |
| Run / re-run a 10-year backtest | [backtesting.md → Running a backtest](backtesting.md#running-a-backtest) |
| See which stocks a strategy is buying now | [backtesting.md → Current signals](backtesting.md#current-signals-bu--sell--holding) |
| Build a risk-shaped or themed portfolio | [portfolios.md](portfolios.md) |
| Add a new trading strategy | [backtesting.md → Adding a strategy](backtesting.md#adding-a-strategy) |
| Look up an API endpoint / MCP tool | [api-reference.md](api-reference.md) · [mcp-tools.md](mcp-tools.md) |

---

## At a glance

- **Engine:** `pipeline_core/` — 8 agents, staged deterministic screening, parallel deep-dive,
  configurable score fusion, SQLite run persistence, rules-only LLM fallback.
- **Backtester:** `pipeline_core/backtest/` — 35 long-only price/volume strategies, next-open
  execution, Indian cost model, full metrics + equity curves + live signals.
- **Surfaces:** `app.py` (REST + WS + `static/` dashboard, 6 tabs incl. **Strategy Lab**),
  `server.py` (FastMCP, ~20 tools incl. 6 backtest tools).
- **Tests:** `env/bin/python -m pytest -q` — **288 network-free tests**.
- **Default mode:** rules-only (`llm_provider=none`); no API key required to run.
