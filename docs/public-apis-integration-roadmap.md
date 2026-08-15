# Free Public-API Integration — Findings & Roadmap

> **Status (2026-08-15):** **PROPOSED — nothing shipped yet.** A discovery pass over the
> [`public-apis/public-apis`](https://github.com/public-apis/public-apis) catalog (Finance, Cryptocurrency,
> Currency Exchange, Economic/Government data sections), mapped onto StockSense's existing provider,
> regime, smart-money, sentiment, and fundamentals seams. Ordered by *impact ÷ effort*, keyless-first to
> match the project's no-key philosophy. Next: a **validation spike** (Tier 0) before any adapter is
> written.

*Grounded in a read-through of the live service layer (`pipeline_core/services/*`) as of 2026-08. The
goal is **resilience** (widen the fallback chains so a single upstream 429 can't empty the board) and a
few **new signals** (congressional trades, equity sentiment) that fit seams already built for the
smart-money / sentiment layers.*

## The core idea, in one sentence

`public-apis` is **not an API — it's a directory of ~1,400 free APIs**, so the value to StockSense is as a
**discovery menu**: mine it for free sources that slot into the fallback chains and signal layers we
already have, validate each one, and adopt only the winners.

## Honest caveats (read before building)

- **The catalog is community-maintained and partly stale.** It still lists **IEX Cloud** (retired 2024)
  and **Nomics** (shut down). Several "No-auth" crypto entries (**CoinCap**, **exchangerate.host**) have
  since moved endpoints or added keys. **Validate every candidate before writing an adapter.**
- **Keyless-first.** The project already prefers no-key sources (Stooq fallback, crypto Fear & Greed).
  Tier 1 below is entirely keyless; keys are pushed to Tier 3 and gated behind flags.
- **Adopt into the existing interface, not around it.** Every new source should implement the
  `services/providers.py` fallback contract and register at `/health/providers` — no bespoke fetch paths.

---

## Tier 0 — Validation spike (do first; half a day, zero code committed)

### 0.1 Curl-test each candidate for schema, rate limits, and liveness  ★ gate for everything below
- **What:** Before any adapter, hit each Tier-1 endpoint (SEC companyfacts, Frankfurter, CoinGecko,
  Goldprice.dev, Fed Treasury) with `curl` and record: response schema, rate limit, HTTPS, and whether
  it's actually alive.
- **Why:** The catalog's staleness means ~1 in 4 entries is dead or changed. An afternoon here avoids
  building on a defunct source.
- **Where:** throwaway script in `scratch/` or a notebook; capture findings back into this doc's
  reference table.
- **How:** confirm each maps cleanly to an existing frame shape (OHLCV / fundamentals panel / scalar
  rate) so the adapter is thin.

---

## Tier 1 — Keyless drop-ins (high trust, reuse existing seams)

### 1.1 SEC EDGAR `companyfacts` → fundamentals fallback  ★ highest-impact
- **What:** The multibagger 6-pillar screen leans on **yfinance fundamentals**, which are flaky and
  rate-limited. SEC's `data.sec.gov/api/xbrl/companyfacts` serves revenue / EPS / debt straight from
  filings, keyless — and we **already hit EDGAR** for 13F, so the client patterns exist.
- **Why:** hardens two seams at once (growth/quality/balance pillars) with data more authoritative than
  yfinance, and removes a live single-point dependency for US fundamentals.
- **Where:** new `edgar_facts.py` next to `pipeline_core/services/edgar_13f.py`; consumed by
  `pipeline_core/multibagger.py`.
- **How:** map XBRL concepts (Revenues, NetIncomeLoss, Liabilities, …) → the pillar inputs; serve as a
  fallback when the yfinance fundamentals frame is empty/stale. Config: `edgar_facts_enabled`.

### 1.2 Frankfurter FX → regime gate
- **What:** `market_regime.py` computes a **DXY-inverse** tilt for metals by hand. Frankfurter (ECB-backed,
  keyless, time-series endpoint) gives clean FX + a real dollar proxy.
- **Why:** firms up the metals risk-on/off input and gives a keyless FX source for multi-market currency
  normalization.
- **Where:** `pipeline_core/services/market_regime.py` (and available to `metals_data.py`).
- **How:** pull the DXY basket (or EUR/USD as a proxy) time series; fall back to **Currency-api** (jsDelivr,
  fully keyless) if Frankfurter is down. Config: `fx_provider` = `frankfurter` | `currency_api`.

### 1.3 CoinGecko market-cap & coin universe
- **What:** Binance/Kraken give OHLCV but **not market cap or categories**. CoinGecko (keyless free tier)
  adds both.
- **Why:** unlocks a **crypto multibagger-style universe** (screen small-cap crypto by market cap the way
  equities screen by size) and a price/market-data fallback rung.
- **Where:** `pipeline_core/services/crypto_data.py` (universe + market-cap enrichment).
- **How:** cache the coin list + market caps daily; feed size/liquidity filters. Respect the free-tier rate
  limit (cache aggressively). Config: `crypto_marketcap_enabled`.

### 1.4 Goldprice.dev → metals price fallback
- **What:** keyless gold/silver/copper spot pricing.
- **Why:** a direct fallback rung for the metals market, which currently has the thinnest provider chain.
- **Where:** `pipeline_core/services/metals_data.py`.
- **How:** slot beneath the primary metals source in the fallback order. Config: `metals_fallback_enabled`.

### 1.5 Fed Treasury / Econdb → macro regime inputs
- **What:** keyless US Treasury rates (Fed Treasury) and global macro (Econdb).
- **Why:** adds **treasury yields + yield-curve slope** as regime inputs alongside VIX/DXY — a classic
  risk-on/off tell the gate doesn't use yet.
- **Where:** `pipeline_core/services/market_regime.py`.
- **How:** derive 10y–2y slope as a signed tilt; blend into the existing per-market tide. Config:
  `regime_macro_enabled`.

---

## Tier 2 — New signals (fit existing layers, higher effort)

### 2.1 US Congressional trades → coattail layer  ★ best new capability
- **What:** the coattail layer covers 13F, COT, Hyperliquid, and NSE bulk deals — but **not US congressional
  trading**, a well-known disclosed signal.
- **Why:** a natural extension of the smart-money basket for the US market; disclosure-derived like the 13F
  seam already is.
- **Where:** new `us_congress.py` sibling to `pipeline_core/services/cot.py`, feeding the coattail basket in
  `pipeline_core/services/smart_money.py`.
- **How:** catalog lists **CongressInvests** (keyed) and **Funding Signals** (SEC-derived); also evaluate the
  keyless public **House/Senate disclosure datasets** as a no-key alternative. Config: `sm_us_congress_enabled`.

### 2.2 Equity sentiment (WallstreetBets) → sentiment layer
- **What:** crypto ideas get a news + Fear & Greed **soft down-rank**; US/IN equities get **no sentiment**.
  WallstreetBets (keyless Reddit sentiment) closes that gap.
- **Why:** parity — equities deserve the same sentiment haircut the crypto shortlist already applies.
- **Where:** `pipeline_core/services/market_sentiment.py`.
- **How:** map ticker → sentiment score; apply as the existing soft down-rank, not a hard gate. Treat
  reliability as low — behind a flag. Config: `sentiment_wsb_enabled`.

---

## Tier 3 — Keyed resilience upgrades (free tier, flag-gated)

### 3.1 One keyed equity provider (Finnhub *or* Twelve Data)
- **What:** the equity fallback chain is only **yfinance → Stooq**, so a yfinance 429 leans hard on one CSV
  source. Finnhub (stocks+FX+crypto+fundamentals in one) or Twelve Data (800 calls/day free) adds a real
  rung.
- **Why:** the direct fix for the yfinance-429 fragility the project already worked around once.
- **Where:** `pipeline_core/services/providers.py` equity chain.
- **How:** one adapter behind the fallback interface; key from env, disabled if absent. Config:
  `finnhub_enabled` / `twelvedata_enabled`.

### 3.2 Financial Modeling Prep → fundamentals depth
- **What:** full financial statements for deeper multibagger fundamentals.
- **Why:** richer than yfinance/EDGAR-facts for statement-level pillars.
- **Where:** `pipeline_core/multibagger.py` (behind the same fundamentals fallback used by 1.1).
- **How:** free-tier key, flag-gated. Config: `fmp_enabled`.

---

## Cross-cutting work

- **Provider-adapter contract + health.** Every new source implements the `services/providers.py` fallback
  interface and registers at `/health/providers` (the health strip already exists).
- **Config flags, keyless-first.** Follow existing naming (`*_enabled`), default keyless sources on and keyed
  sources off-until-key-present.
- **Caching + rate-limit discipline.** Free tiers punish chattiness — cache universe/market-cap/FX daily,
  reuse the last-good-frame `df.attrs['stale']` pattern already in `providers.py`.
- **Tests.** Mirror `tests/test_market_sentiment.py` / `tests/test_idea_efficiency.py`: mocked-response unit
  tests per adapter + a fallback-chain test proving graceful degradation.

---

## Candidate reference table

| API | Seam / file | Key? | Catalog status | Tier |
|---|---|---|---|---|
| SEC EDGAR companyfacts | `edgar_facts.py` → `multibagger.py` | No | Live | 1.1 |
| Frankfurter (FX) | `market_regime.py` | No | Live | 1.2 |
| Currency-api (FX alt) | `market_regime.py` | No | Live | 1.2 |
| CoinGecko | `crypto_data.py` | No (free tier) | Live | 1.3 |
| Goldprice.dev | `metals_data.py` | No | Verify | 1.4 |
| Fed Treasury | `market_regime.py` | No | Live | 1.5 |
| Econdb | `market_regime.py` | No | Live | 1.5 |
| CongressInvests | `us_congress.py` → `smart_money.py` | apiKey | Verify | 2.1 |
| Funding Signals | `us_congress.py` | apiKey | Verify | 2.1 |
| WallstreetBets | `market_sentiment.py` | No | Verify | 2.2 |
| Finnhub | `providers.py` | apiKey (free) | Live | 3.1 |
| Twelve Data | `providers.py` | apiKey (free) | Live | 3.1 |
| Financial Modeling Prep | `multibagger.py` | apiKey (free) | Live | 3.2 |
| ~~IEX Cloud~~ | — | — | **Retired 2024 — do not use** | — |
| ~~Nomics~~ | — | — | **Shut down — do not use** | — |

---

## Suggested sequencing

1. **Tier 0 spike** — validate endpoints, update the table's "status" column.
2. **1.1 SEC companyfacts** — cleanest first build (reuses EDGAR patterns, hardens two seams).
3. **1.2 Frankfurter FX** + **1.4 Goldprice** — small keyless resilience wins for the regime gate + metals.
4. **1.3 CoinGecko** — unlocks the crypto-universe screen.
5. **2.1 Congress trades** — the marquee new signal, once keyless-vs-keyed source is settled.
6. **Tier 3** — add a keyed equity rung only if 429s remain a live problem after Tier 1.
