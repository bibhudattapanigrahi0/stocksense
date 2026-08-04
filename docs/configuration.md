# Configuration

All settings live in [`pipeline_core/config.py`](../pipeline_core/config.py) as a
pydantic-settings `Settings` model. Values load from **environment variables** or a
**`.env`** file at the project root (`extra="ignore"`, so unknown keys are harmless).
Access them via `from pipeline_core.config import settings`.

## Paths

| Constant | Default | Notes |
|---|---|---|
| `BASE_DIR` | package parent | Project root. |
| `DATA_DIR` | `./data` | Override with env `STOCKSENSE_DATA_DIR` (used when embedding the core elsewhere). |
| `CACHE_DIR` | `data/cache` | diskcache stores (incl. `data/cache/backtest`). |
| `REPORTS_DIR` | `data/reports` | Generated markdown reports. |
| `DB_PATH` | `data/stocksense.db` | SQLite run store + saved screens. |

`DATA_DIR`, `CACHE_DIR`, and `REPORTS_DIR` are created at import time.
Backtest results are written to `DATA_DIR/backtests/latest.json`.

## Settings

### App

| Setting | Env var | Default | Meaning |
|---|---|---|---|
| `app_name` | `APP_NAME` | `StockSense` | Display name. |
| `debug` | `DEBUG` | `true` | Debug flag. |

### LLM provider

| Setting | Env var | Default | Meaning |
|---|---|---|---|
| `llm_provider` | `LLM_PROVIDER` | `none` | `openai` \| `anthropic` \| `ollama` \| `none`. `none` = **rules-only mode**. |
| `openai_api_key` | `OPENAI_API_KEY` | `""` | Required for `openai`. |
| `anthropic_api_key` | `ANTHROPIC_API_KEY` | `""` | Required for `anthropic`. |
| `ollama_base_url` | `OLLAMA_BASE_URL` | `http://localhost:11434` | Local Ollama daemon. |
| `llm_model` | `LLM_MODEL` | `gpt-4o-mini` | Model id. The Anthropic path substitutes a Claude model when this looks like a GPT id; the Ollama path substitutes `llama3`. |

When no provider is configured, every LLM call site degrades gracefully to a deterministic
fallback (lexicon sentiment, template report prose, no-op NL parsing). See
[architecture.md → LLM policy](architecture.md#llm-policy).

### Cache TTLs (seconds)

| Setting | Default | Applies to |
|---|---|---|
| `cache_ttl_price` | `900` (15 min) | Price history / quotes. |
| `cache_ttl_fundamentals` | `86400` (1 d) | Profile / statements. |
| `cache_ttl_news` | `3600` (1 h) | News RSS. |
| `cache_ttl_holdings` | `86400` (1 d) | NSE promoter/pledge/delivery. |

> The **backtest** history loader uses its own fixed TTL of **20 h** (`BT_CACHE_TTL` in
> `data_loader.py`), independent of these — historical daily bars are effectively immutable.

### Screening defaults

| Setting | Default | Meaning |
|---|---|---|
| `default_index` | `NIFTY 50` | Universe when a request omits one. |
| `max_candidates` | `20` | Default candidate cap. |
| `analysis_depth` | `standard` | `quick` \| `standard` \| `deep`. |

### Concurrency

| Setting | Default | Meaning |
|---|---|---|
| `ingestion_concurrency` | `8` | Semaphore bound on parallel data fetches (also used by the backtest history loader). |
| `analysis_concurrency` | `6` | Semaphore bound on parallel analysis. |

### Composite score weights

Must sum to **1.0** (`settings.validate_weights()` enforces this). The risk component is
**inverted** in fusion (higher risk lowers the score).

| Setting | Default |
|---|---|
| `weight_fundamental` | `0.40` |
| `weight_technical` | `0.30` |
| `weight_sentiment` | `0.15` |
| `weight_risk` | `0.15` |

Composite = `0.40·fundamental + 0.30·technical + 0.15·sentiment + 0.15·(100 − risk)`.

### Reports

| Setting | Default | Meaning |
|---|---|---|
| `report_top_n` | `1` | How many top candidates get a generated report. |

## Example `.env`

```ini
llm_provider=openai
openai_api_key=sk-...
llm_model=gpt-4o-mini

default_index=NIFTY 500
ingestion_concurrency=10
weight_fundamental=0.40
weight_technical=0.30
weight_sentiment=0.15
weight_risk=0.15
```
