# Markets — multi-asset support (India, US, Metals, Crypto)

StockSense started as an India-only (NSE/BSE) tool and now runs the same
screening / analysis / Strategy-Lab / portfolio experience across **four asset
classes**, selectable from a single header dropdown in the dashboard and a
`market` parameter on every REST endpoint and MCP tool.

The whole thing hinges on one module — [`pipeline_core/markets.py`](../pipeline_core/markets.py)
— a **market registry** that turns every asset-class-specific value (ticker
suffix, benchmark, currency/units, universe source, whether fundamentals exist)
into a lookup. Every other module just replaces a hardcoded India constant with
`market.<field>`. Because every new `market` parameter defaults to `"IN"`, all
prior behavior (and the whole test suite) is unchanged.

---

## The four markets

| Market | Kind | Universe source | OHLCV / info | Benchmark | Fundamentals | Market cap |
|---|---|---|---|---|---|---|
| **IN** | equity | Nifty Indices CSV (`nse_data`) | yfinance `.NS` | `^NSEI` (NIFTY 50) | yes | INR **Cr** |
| **US** | equity | S&P 500 / NASDAQ-100 / Dow 30 (`us_data`) | yfinance (no suffix) | `^GSPC` (S&P 500) | yes | USD **B** |
| **METALS** | price_only | curated futures + ETFs (`metals_data`) | yfinance `GC=F`/`GLD`… | `GC=F` (Gold) | no | — |
| **CRYPTO** | price_only | CoinGecko top-N (`crypto_data`) | **Binance** klines (+ yfinance fallback) | `BTC-USD` (Bitcoin) | no | USD **B** |

**Two market *kinds*:**

- **`equity`** (IN, US) — full fundamentals exist, so the screener runs its
  valuation/growth/profitability/quality filters and the deep-dive fuses the
  four-agent composite (`0.40·fundamental + 0.30·technical + 0.15·sentiment +
  0.15·(100−risk)`).
- **`price_only`** (metals, crypto) — no P/E, ROE, or promoter data. The screener
  keeps only technical/price filters, and the deep-dive uses a **price composite**
  (`technical + momentum + (100−risk) + sentiment`, weights in `market.price_weights`)
  with a **momentum sub-score** computed from trailing 3/6/12-month returns and
  distance from the 52-week high. The fundamental agent is skipped entirely.

The **backtest engine and technical screening stage are asset-agnostic** — they
were always pure price/volume — so all 38 backtest strategies and the whole
Strategy Lab / Trade Ideas / portfolio machinery run across every market with
only a universe + benchmark swap.

**Backtest results are stored per market** (`data/backtests/latest_<MARKET>.json`),
so the Strategy Lab, Trade Ideas, and the systematic/blend Portfolio basis each
reflect the *selected* market independently — switching the header market reloads
those tabs against that market's own last run (run a backtest per market to
populate it; a pre-existing `latest.json` is honored as the IN run). The Idea
Tracker tags each idea with its market and prices/scoreboards it per market.

**Long *and* short — CRYPTO only.** Crypto trends down as cleanly as it trends up, so its Trade
Ideas board trades both ways: alongside the long ideas it runs a small **bearish-mirror SHORT set**
(`CRYPTO_SHORT_STRATEGIES` — a risk-off Donchian-low breakdown, downside-momentum, and Supertrend
bearish-flip, mirroring the crypto-native longs) that is backtested short-aware (the engine flips the
stop/target/P&L for `direction="short"`). Each board candidate carries a `side`; a short's **stop sits
above** entry and its **target below**, its realized/unrealized P&L and tracker outcome invert, and
longs vs shorts are ranked, gated, de-duped and sized as **independent books** (a risk-off tide is a
short's tailwind). Every other market (IN/US/METALS) stays long-only, and even on crypto the curated
**Today's Basket** / best-odds shortlists remain long-only. Toggle with
`settings.crypto_shorts_enabled` (default on). See the ⚡📈🏔️ columns' **↓ Shorts** subgroup in the UI.

---

## Data sources per market

- **IN** — unchanged: live Nifty Indices constituent CSVs via
  [`nse_data`](../pipeline_core/services/nse_data.py), NSE promoter/pledge/delivery
  enrichment, yfinance `.NS` prices.
- **US** — [`us_data`](../pipeline_core/services/us_data.py): the S&P 500 list is
  fetched from a stable public CSV (cached 1 day, large-cap fallback on failure);
  NASDAQ-100 and Dow 30 are curated constituent lists. yfinance serves prices/info
  with no ticker suffix (`BRK.B` → `BRK-B`).
- **METALS** — [`metals_data`](../pipeline_core/services/metals_data.py): a small,
  fixed universe of precious/industrial-metal futures (`GC=F`, `SI=F`, `PL=F`,
  `PA=F`, `HG=F`) plus liquid ETFs (`GLD`, `SLV`, `PPLT`, `PALL`, `CPER`, …). No
  network fetch — the list is stable.
- **CRYPTO** — [`crypto_data`](../pipeline_core/services/crypto_data.py): a
  split-source design chosen for coverage and quality —
  - **Universe + market cap + name** from **CoinGecko** (`/coins/markets`, cached
    1 day, majors fallback). Stablecoins, wrapped/staked derivatives, tokenized-
    treasury (RWA) funds, and non-Latin tickers are filtered out (they clutter the
    market-cap ranking but aren't tradable). Profiles are built from one cached
    snapshot — no per-coin network call, so ingestion is fast and clean.
  - **OHLCV history** via a **provider fallback chain**
    ([`providers.fetch_with_fallback`](../pipeline_core/services/providers.py)):
    **Binance** (`data-api.binance.vision`, no key, no geo-block) → **Kraken**
    (`api.kraken.com`) → **yfinance**. Full O/H/L/C/V, far broader coverage than
    yfinance alone (e.g. SUI/TIA/SEI/WLD; Kraken adds coins Binance lacks like XMR),
    with retries + backoff and **per-provider health** at `GET /api/v1/health/providers`.
    The DataFrame shape matches yfinance, so indicators / bots / the backtest reuse
    unchanged. Intraday bars (`5m–4h`) come from the same chain (`10m` resampled from `5m`).

---

## Market conditions header — direction + Fear & Greed

The daily **Trade Ideas** board opens with a **Market Conditions** card pairing the tide it trades
with. Both readings are cached ~1h and degrade to a neutral value, so an offline run leaves the board
unchanged, and both ride along on the `/api/v1/backtest/ideas` payload (`regime` + `fear_greed`) and
the `get_market_conditions` MCP tool.

- **Direction** — the risk-on/off regime
  ([`market_regime`](../pipeline_core/services/market_regime.py)): the benchmark's 200-DMA trend + a
  vol/macro factor. This one **acts** on the board — regime-aligned longs rank first and risk-off
  trims position size.
- **Fear & Greed** — a 0–100 market-mood gauge
  ([`market_sentiment.fear_greed`](../pipeline_core/services/market_sentiment.py)), **display-only**.
  Because no single free source covers every market, the source is per-market:
  - **US** — the real **CNN Fear & Greed Index** (keyless, *unofficial* dataviz endpoint; falls back
    to the derived blend below if blocked or if `fear_greed_cnn_enabled` is off).
  - **CRYPTO** — the free **alternative.me** Fear & Greed feed.
  - **IN / other** — a **derived "Market Mood"** (labelled *derived*, not an official index — India
    has no free canonical one): the benchmark's momentum vs its ~125-DMA blended with an inverted
    volatility-index percentile (`fng_w_momentum` / `fng_w_volatility`).

---

## Using it

**Dashboard** — pick the market in the header switcher; all nine tabs re-scope.
India-only filters (promoter/pledge/delivery) hide off-IN; fundamentals filter
groups hide for metals/crypto; market-cap unit and benchmark labels swap
(`Cr`→`B`, `α vs Nifty`→`α vs S&P 500`/`Gold`/`Bitcoin`).

**REST** — every relevant endpoint takes `market` (query param or request body,
default `"IN"`). `GET /api/v1/markets` returns the registry that drives the
frontend. Examples:

```bash
curl /api/v1/markets
curl -X POST /api/v1/screen -d '{"market":"US","filters":{"pe_ratio_max":20,"roe_min":0.15}}'
curl "/api/v1/stocks/AAPL/analyze?market=US"
curl -X POST /api/v1/backtest/run -d '{"market":"METALS"}'
```

**MCP** — the same `market` argument on `screen_stocks`, `analyze_stock`,
`get_stock_overview`, `run_pipeline`, `generate_report`, `run_backtest`,
`get_index_constituents_list`, and `list_strategies`; plus a `list_markets` tool.

```python
screen_stocks(market="US", filters={"pe_ratio_max": 20, "roe_min": 0.15})
analyze_stock(ticker="GLD", market="METALS")   # price-only composite
run_backtest(market="CRYPTO")                  # top-100 coins vs Bitcoin
```

---

## Adding another market

1. Add a `Market(...)` entry to `MARKETS` in
   [`pipeline_core/markets.py`](../pipeline_core/markets.py) (key, kind, benchmark,
   units, universes, cap tiers, and — for `price_only` — `price_weights`).
2. Add a data service exposing `get_universe(name)` (or `get_index_constituents`)
   and wire it into `get_universe_tickers`'s lazy dispatch.
3. If it's an `equity` market, add a curated strategy set to
   [`pipeline_core/strategies.py`](../pipeline_core/strategies.py); `price_only`
   markets reuse the shared technical `PRICE_STRATEGIES`.

Everything else — screener gating, scoring, backtest, portfolios, and the
frontend switcher — picks it up automatically from the registry.

> ⚠️ Same disclaimer as always: **decision-support research, not investment
> advice.** Crypto and metals are volatile and price-only — there are no
> fundamentals to fall back on.
