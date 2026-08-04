# Getting Started

## Prerequisites

- **Python 3.13** (a virtualenv already exists at `./env`).
- macOS/Linux. Network access for live data (yfinance / NSE); historical backtest data is
  cached on disk after the first pull.
- **No LLM key required** — StockSense runs **rules-only** by default.

## Install

```bash
# Use the bundled venv (or create your own: python3.13 -m venv env)
env/bin/pip install -r requirements.txt
```

## Configure (optional)

Everything is configured via `.env` / environment variables (pydantic-settings). The
defaults run fully offline of any LLM. To enable LLM features:

```bash
cp .env.example .env
# then edit:
#   llm_provider=openai        # openai | anthropic | ollama | none
#   openai_api_key=sk-...
#   llm_model=gpt-4o-mini
```

See [configuration.md](configuration.md) for the full list of settings.

Data directories (`data/cache`, `data/reports`, `data/stocksense.db`) are created
automatically at import time. Override the data location with `STOCKSENSE_DATA_DIR`.

---

## Run the REST API + Dashboard

```bash
env/bin/uvicorn app:app --reload --port 8000
```

- Dashboard: <http://127.0.0.1:8000/>
- Interactive API docs (Swagger): <http://127.0.0.1:8000/docs>

The dashboard (vanilla-JS SPA in `static/`) has **six tabs**:

| Tab | Purpose |
|---|---|
| **Screener** | NL query + grouped filter form → candidate cards with match % and per-filter evidence. |
| **Strategies** | One-click curated screening strategies (Quality Compounders, Value Hunters, …). |
| **Strategy Lab** | 10-year backtest leaderboard for all 38 strategies; click a row for metrics, equity curve, and BUY/SELL/HOLDING stock lists. |
| **Pipeline** | Full run with live WebSocket progress → ranked results + report links. |
| **Analyze Stock** | Single-ticker four-agent deep dive. |
| **Reports** | Generated markdown research notes, rendered in-browser. |
| **History** | Persisted pipeline runs (SQLite). |

---

## Run the MCP server

```bash
env/bin/python server.py                        # stdio (Claude Desktop)
env/bin/python server.py --http --port 8001     # streamable HTTP (remote clients)
```

Claude Desktop config
(`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "stocksense": {
      "command": "/Users/bdpanigrahi/projects/mcp_projects/stock-data/env/bin/python",
      "args": ["/Users/bdpanigrahi/projects/mcp_projects/stock-data/server.py"]
    }
  }
}
```

Then ask Claude things like *"screen for quality IT stocks with ROE above 20%"*,
*"show the backtest leaderboard by win rate"*, or *"which stocks is Connors RSI(2)
flagging as a buy?"*. Full tool list: [mcp-tools.md](mcp-tools.md).

---

## Run a backtest

The Strategy Lab backtests all 38 strategies over 10 years of Nifty 500 history. The first
run downloads ~500 stocks' daily history (minutes); subsequent runs are cached (~75–140 s).

- **Dashboard:** Strategy Lab tab → **⟳ Run Backtests**.
- **REST:** `POST /api/v1/backtest/run` (see [api-reference.md](api-reference.md#backtesting)).
- **MCP:** `run_backtest` → poll `get_backtest_status` → read `get_backtest_leaderboard`.
- **Script:**

  ```bash
  env/bin/python -c "import asyncio; from pipeline_core.backtest.runner import run_full_backtest; \
  print(asyncio.run(run_full_backtest('NIFTY 500', 10))['data_stats'])"
  ```

Results persist to `data/backtests/latest.json` and are served to the UI/REST/MCP.
Full details: [backtesting.md](backtesting.md).

---

## Run the tests

```bash
env/bin/python -m pytest -q      # 288 tests, all network-free (I/O mocked/fixtured)
```

See [architecture.md → Testing](architecture.md#testing) for what's covered.
