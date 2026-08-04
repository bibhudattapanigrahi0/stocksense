# Institutional ownership data

StockSense surfaces "what are institutions doing?" two ways, from two different sources.
The distinction matters, because one is backtestable and one is not.

| Signal | Source | History? | Markets | Where |
| --- | --- | --- | --- | --- |
| **Accumulation footprint** | price/volume (OHLCV) | ✅ full | all | the `Accumulation` strategies (CMF / A-D line / MFI) |
| **13F institutional holdings** | SEC EDGAR | ✅ quarterly, ~2013+ | **US** | live badge on ideas + `institutional-13f-accumulation` strategy |
| **Ownership snapshot** | yfinance | ❌ current only | US / IN | live badge fallback |
| **FII / DII / promoter** | NSE | ⚠️ recent quarters | IN | screener filters (`nse_data.get_promoter_holding`) |

## Why not just "institutional ownership %"?

Raw institutional-flow data (FII-DII, 13F) has **no honest daily history**, so it was
originally excluded from every backtest (see [ENHANCEMENTS.md](../ENHANCEMENTS.md) §2.1).
yfinance's `heldPercentInstitutions` is a *current snapshot* — fine as a live confirmation,
useless for "what would this have signalled 5 years ago?".

Two things fix that:

1. The **price/volume footprint** of accumulation *is* fully in the OHLCV and always was —
   that's the `Accumulation` strategy family (Chaikin Money Flow, Accumulation/Distribution
   line, Money Flow Index). It's the *timely* signal (daily).
2. **SEC 13F filings** give a *real historical* institutional series for US stocks — quarterly,
   lagged, but genuine filed holdings. That's the `institutional-13f-accumulation` strategy and
   the richer US live badge (with a real quarter-over-quarter change).

## SEC EDGAR 13F pipeline (`pipeline_core/services/edgar_13f.py`)

Every quarter, large investment managers file a **Form 13F-HR** listing their US holdings
(~45 days after quarter end). We aggregate a **curated basket of ~30 large, mostly-active
managers** (`TRACKED_MANAGERS`) — Berkshire, Capital Group, T. Rowe, Wellington, Fidelity,
Tiger/Coatue/Viking, Citadel/Millennium/Two Sigma/RenTech/D.E. Shaw, Pershing/Third
Point/Appaloosa/Icahn, Baupost, Dodge & Cox, Ruane/Sequoia, GQG, and more.

Flow: `submissions API → 13F-HR filings → info-table XML ({cusip: Σ shares}) → OpenFIGI
CUSIP→ticker → aggregate per ticker per quarter`, keyed by **filing date** so a backtest can
never see a filing before it was public. The panel is cached to disk (`data/cache/edgar/`,
immutable filings) and read by both the live badge and the backtest merge.

### Setup

SEC's fair-access policy **requires a contact User-Agent** on every request — without a real
one you get HTTP 403 and no data (the app then silently falls back to the yfinance snapshot).
Set it in `.env`:

```
sec_user_agent=YourApp/1.0 (you@example.com)
# openfigi_api_key=      # optional — CUSIP mapping works without a key, a key just raises the limit
```

Building the full S&P-500 panel is a one-time ~30–45 min job (SEC + OpenFIGI rate limits),
fully cached/resumable afterwards. It runs as part of a US backtest when the panel is stale.

## Honest limits

- **US only** — 13F is a US filing.
- **A basket, not total float** — we track ~30 large managers, deliberately excluding the
  passive Big Three (Vanguard/BlackRock/State Street) whose index holdings would drown a
  conviction signal. So it reads "major-manager positioning", not total institutional %.
- **Quarterly + ~45-day lag** — a slow position/filter, never a daily timing edge. 13F "alpha"
  is well-studied and thin; treat it as confirmation, not a standalone edge.
- **~2013+** — structured XML info tables start then, so the backtest window begins there.
- **External** — EDGAR + OpenFIGI are cached and health-tracked; any failure degrades to the
  yfinance snapshot (live) or leaves the 13F strategy inert (backtest).
