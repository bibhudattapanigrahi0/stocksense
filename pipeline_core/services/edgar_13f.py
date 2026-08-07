"""
SEC EDGAR 13F — free *historical* institutional holdings for US equities.

yfinance only exposes a current ownership snapshot (no history → not backtestable).
13F-HR filings, filed quarterly by large investment managers ~45 days after quarter
end, give a real time series. This service turns a curated basket of large managers'
filings into a per-ticker quarterly panel of aggregate shares held + QoQ change.

Honest limits (see docs/institutional-data.md):
  • US-only (13F is a US filing).
  • We aggregate a CURATED BASKET of large, mostly-active managers — deliberately not
    the passive Big Three (Vanguard/BlackRock/State Street), which hold ~everything and
    would drown a conviction signal in index noise. So it's "major-manager positioning",
    not total float.
  • Quarterly cadence + a 45-day filing lag: we index each quarter's data by its FILING
    date, so forward-filling onto daily bars can never look ahead.
  • Structured XML info tables exist ~2013+, so history starts there.

Everything degrades to None on failure (SEC/OpenFIGI unreachable); the live overlay
falls back to the yfinance snapshot and the backtest strategy simply goes inert.
"""

from __future__ import annotations

import logging
import threading
import time
import xml.etree.ElementTree as ET
from typing import Optional

import diskcache
import pandas as pd
import requests

from pipeline_core.config import CACHE_DIR, settings

logger = logging.getLogger(__name__)
cache = diskcache.Cache(str(CACHE_DIR / "edgar"))

SEC_TTL = 30 * 24 * 3600     # filings are immutable; a 30-day refresh only picks up new quarters
FIGI_TTL = 180 * 24 * 3600   # a CUSIP↔ticker mapping is effectively permanent
SEC_MIN_INTERVAL = 0.16      # ≥6 req/s headroom under SEC's 10 req/s fair-access limit

# ── Tracked managers (CIK → name) ───────────────────────────────────
# A curated basket of large, CONCENTRATED, mostly-ACTIVE 13F filers whose aggregate
# position in a name is a genuine conviction signal. Deliberately EXCLUDED: passive index
# giants (Vanguard/BlackRock/State Street/Geode) that hold ~everything, and quant multi-
# strats (Citadel/Millennium/RenTech/Two Sigma/D.E. Shaw/Point72) whose thousands of
# high-turnover positions are noise, not conviction — both would also explode the CUSIP
# universe (and the one-time build time). A wrong/renamed CIK just contributes nothing
# (graceful); every CIK validated against EDGAR. Expandable — more managers = more coverage.
TRACKED_MANAGERS: dict[str, str] = {
    "0001067983": "Berkshire Hathaway",
    "0001422848": "Capital Research Global Investors",
    "0001422849": "Capital World Investors",
    "0000080255": "T. Rowe Price",
    "0000902219": "Wellington Management",
    "0000315066": "FMR (Fidelity)",
    "0001088875": "Baillie Gifford",
    "0000850529": "Fisher Asset Management",
    "0000200217": "Dodge & Cox",
    "0000763212": "PRIMECAP Management",
    "0000728014": "Ruane Cunniff (Sequoia)",
    "0001697233": "GQG Partners",
    "0001167483": "Tiger Global Management",
    "0001135730": "Coatue Management",
    "0001103804": "Viking Global Investors",
    "0001350694": "Bridgewater Associates",
    "0001656456": "Appaloosa",
    "0001336528": "Pershing Square Capital",
    "0001040273": "Third Point",
    "0001079114": "Greenlight Capital",
    "0000921669": "Icahn (Carl C)",
    "0001061768": "Baupost Group",
    "0001112520": "Akre Capital Management",
}

# ── Famed "guru" investors (CIK → name) ─────────────────────────────
# The coattail idea in its purest form: legendary, highly-concentrated, long-only-ish stock
# pickers whose 13F is followed precisely because the picker is famous. Kept SEPARATE from the
# institutional basket above (config-gated via smart_money.sm_us_gurus_enabled) so the guru set
# can be tuned independently, and folded into the panel build by `tracked_managers()`. Every CIK
# below was validated against EDGAR's submissions API. A wrong/renamed CIK is a graceful no-op
# (contributes nothing); expandable — more gurus = more coverage. See docs/institutional-data.md.
GURU_MANAGERS: dict[str, str] = {
    "0001649339": "Scion Asset Management (Michael Burry)",
    "0001536411": "Duquesne Family Office (Stanley Druckenmiller)",
    "0001709323": "Himalaya Capital (Li Lu)",
    "0001061165": "Lone Pine Capital",
    "0001647251": "TCI Fund Management (Chris Hohn)",
    "0000807985": "Southeastern Asset Management (Longleaf)",
    "0000813917": "Harris Associates (Oakmark)",
    "0001351069": "ValueAct Capital",
    "0001520023": "Fundsmith (Terry Smith)",
    "0000936753": "Ariel Investments",
}


def tracked_managers() -> dict[str, str]:
    """The manager basket to aggregate into the panel: the institutional core, plus the famed
    gurus when `sm_us_gurus_enabled` (the default). One place so build_panel and callers agree."""
    if getattr(settings, "sm_us_gurus_enabled", True):
        return {**TRACKED_MANAGERS, **GURU_MANAGERS}
    return dict(TRACKED_MANAGERS)


# ── Polite, cached SEC fetch ────────────────────────────────────────

_rate_lock = threading.Lock()
_last_req = [0.0]


def _throttle() -> None:
    with _rate_lock:
        dt = time.time() - _last_req[0]
        if dt < SEC_MIN_INTERVAL:
            time.sleep(SEC_MIN_INTERVAL - dt)
        _last_req[0] = time.time()


_ua_warned = [False]


def _check_user_agent() -> None:
    """SEC 403s a placeholder/missing contact UA — warn once so the cause is obvious
    (the feature still degrades to the yfinance snapshot, it just gets no EDGAR data)."""
    if not _ua_warned[0] and (".env" in settings.sec_user_agent or "@" not in settings.sec_user_agent):
        logger.warning("SEC EDGAR: sec_user_agent looks like a placeholder (%r) — SEC will likely "
                       "return 403. Set a real contact string in .env to enable 13F data.",
                       settings.sec_user_agent)
        _ua_warned[0] = True


def _sec_get(url: str, *, as_json: bool = True, ttl: int = SEC_TTL):
    """Cached GET against SEC with the required contact User-Agent. Returns parsed
    JSON (as_json) or raw bytes, or None on any non-200/error (never raises)."""
    ck = f"sec:{'j' if as_json else 'b'}:{url}"
    hit = cache.get(ck)
    if hit is not None:
        return hit
    _check_user_agent()
    _throttle()
    try:
        resp = requests.get(url, headers={"User-Agent": settings.sec_user_agent,
                                           "Accept-Encoding": "gzip, deflate"}, timeout=30)
    except Exception as e:
        logger.warning("SEC fetch failed %s: %s", url, e)
        return None
    if resp.status_code != 200:
        logger.warning("SEC %s → HTTP %s", url, resp.status_code)
        return None
    out = resp.json() if as_json else resp.content
    cache.set(ck, out, expire=ttl)
    return out


# ── 13F filing discovery + parsing ──────────────────────────────────

def _filing_list(cik: str) -> list[dict]:
    """Every 13F-HR filing for a CIK: [{quarter, filed, accession}], across history
    (submissions 'recent' block + older shards)."""
    cik10 = str(int(cik)).zfill(10) if cik.isdigit() else cik  # tolerate placeholder keys
    data = _sec_get(f"https://data.sec.gov/submissions/CIK{cik10}.json")
    if not data:
        return []
    out: list[dict] = []

    def _collect(block: dict) -> None:
        forms = block.get("form", [])
        rpt = block.get("reportDate", [])
        fil = block.get("filingDate", [])
        acc = block.get("accessionNumber", [])
        for i, f in enumerate(forms):
            if f == "13F-HR":
                out.append({"quarter": rpt[i] if i < len(rpt) else "",
                            "filed": fil[i] if i < len(fil) else "",
                            "accession": acc[i]})

    filings = data.get("filings", {})
    _collect(filings.get("recent", {}))
    for shard in filings.get("files", []):
        sd = _sec_get(f"https://data.sec.gov/submissions/{shard['name']}")
        if sd:
            _collect(sd)
    return out


def parse_info_table(body: bytes) -> dict[str, int]:
    """Parse a 13F information-table XML → {cusip: total_shares}, summing the multiple
    rows a manager may report per security. Pure (no I/O) — unit-tested directly."""
    holdings: dict[str, int] = {}
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return {}
    for el in root.iter():
        el.tag = el.tag.rsplit("}", 1)[-1]          # drop XML namespace
    for row in root.findall(".//infoTable"):
        cusip = (row.findtext("cusip") or "").strip().upper()
        shares_txt = (row.findtext(".//sshPrnamt") or "").strip().replace(",", "")
        if not cusip or not shares_txt:
            continue
        try:
            holdings[cusip] = holdings.get(cusip, 0) + int(float(shares_txt))
        except ValueError:
            continue
    return holdings


def _info_table(cik: str, accession: str) -> dict[str, int]:
    """{cusip: shares} for one 13F-HR filing (fetches the info-table XML in its folder)."""
    accn = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn}"
    idx = _sec_get(base + "/index.json")
    if not idx:
        return {}
    xmls = [it["name"] for it in idx.get("directory", {}).get("item", [])
            if it["name"].lower().endswith(".xml") and it["name"] != "primary_doc.xml"]
    for name in xmls:
        body = _sec_get(base + "/" + name, as_json=False)
        if body and (b"infoTable" in body or b"informationTable" in body):
            return parse_info_table(body)
    return {}


# ── CUSIP → ticker (OpenFIGI) ───────────────────────────────────────

def _map_cusips(cusips: list[str]) -> dict[str, Optional[str]]:
    """CUSIP → US ticker via OpenFIGI, per-CUSIP cached permanently. Batches the misses
    (10/req without a key, 100 with) and respects the free rate limit."""
    result: dict[str, Optional[str]] = {}
    misses = []
    for c in cusips:
        hit = cache.get(f"figi:{c}")
        if hit is not None:
            result[c] = hit or None
        else:
            misses.append(c)
    if not misses:
        return result

    keyed = bool(settings.openfigi_api_key)
    headers = {"Content-Type": "application/json"}
    if keyed:
        headers["X-OPENFIGI-APIKEY"] = settings.openfigi_api_key
    batch_n = 100 if keyed else 10
    for i in range(0, len(misses), batch_n):
        batch = misses[i:i + batch_n]
        try:
            r = requests.post("https://api.openfigi.com/v3/mapping",
                              json=[{"idType": "ID_CUSIP", "idValue": c} for c in batch],
                              headers=headers, timeout=30)
            if r.status_code != 200:
                logger.warning("OpenFIGI HTTP %s — backing off", r.status_code)
                time.sleep(3)
                continue
            for c, res in zip(batch, r.json()):
                data = (res or {}).get("data") or []
                tick = data[0].get("ticker") if data else None
                cache.set(f"figi:{c}", tick or "", expire=FIGI_TTL)
                result[c] = tick
        except Exception as e:
            logger.warning("OpenFIGI batch failed: %s", e)
        time.sleep(0.3 if keyed else 2.6)   # 25 req/min unkeyed, generous keyed
    return result


# ── Panel build + per-ticker series ─────────────────────────────────

def build_panel(target_tickers: list[str], force: bool = False) -> dict[str, list[dict]]:
    """Aggregate the tracked managers' 13F holdings into a per-ticker quarterly panel.

    Returns {ticker: [{"filed": YYYY-MM-DD, "quarter": YYYY-MM-DD, "shares": int,
    "managers": int}, ...]} sorted by filing date. Heavy one-time network build; every
    SEC/OpenFIGI call is disk-cached so a re-run resumes. Persisted under `panel:US`."""
    pk = "panel:US"
    if not force:
        cached = cache.get(pk)
        if cached is not None:
            return cached

    targets = {t.upper() for t in target_tickers}
    cusip_universe: set[str] = set()
    raw: list[tuple] = []   # (filed, quarter, cik, {cusip: shares})

    for cik, name in tracked_managers().items():
        filings = _filing_list(cik)
        if not filings:
            logger.info("EDGAR: no 13F-HR for %s (%s) — skipped", name, cik)
            continue
        for f in filings:
            holds = _info_table(cik, f["accession"])
            if not holds:
                continue
            cusip_universe.update(holds.keys())
            raw.append((f["filed"], f["quarter"], cik, holds))

    cusip_ticker = _map_cusips(sorted(cusip_universe))

    # Aggregate by (ticker, QUARTER) — sum shares across ALL managers who reported that
    # quarter, so each row is one true quarterly figure (not one per manager-filing). The
    # availability date is the LAST manager's filing for the quarter: the full aggregate
    # isn't knowable until everyone has filed, which keeps the series anti-lookahead.
    agg: dict[tuple, dict] = {}   # (ticker, quarter) → {shares, managers:set, filed}
    for filed, quarter, cik, holds in raw:
        for cusip, shares in holds.items():
            tk = cusip_ticker.get(cusip)
            if not tk or tk.upper() not in targets:
                continue
            rec = agg.setdefault((tk.upper(), quarter), {"shares": 0, "managers": set(), "filed": filed})
            rec["shares"] += shares
            rec["managers"].add(cik)
            if filed > rec["filed"]:
                rec["filed"] = filed

    panel: dict[str, list[dict]] = {}
    for (tk, quarter), rec in agg.items():
        panel.setdefault(tk, []).append({
            "filed": rec["filed"], "quarter": quarter,
            "shares": rec["shares"], "managers": len(rec["managers"]),
        })
    for tk in panel:
        panel[tk].sort(key=lambda r: r["quarter"])   # chronological by report quarter

    cache.set(pk, panel, expire=SEC_TTL)
    logger.info("EDGAR panel built: %d tickers, %d CUSIPs mapped", len(panel), len(cusip_ticker))
    return panel


def series_from_records(records: list[dict]) -> Optional[pd.DataFrame]:
    """Per-ticker records → DataFrame indexed by FILING date (availability = anti-lookahead),
    with agg_shares, qoq_change_pct, n_managers. Pure — unit-tested directly."""
    if not records:
        return None
    df = pd.DataFrame(records)
    df["filed"] = pd.to_datetime(df["filed"])
    df = df.dropna(subset=["filed"]).sort_values("filed").set_index("filed")
    if df.empty:
        return None
    df = df[~df.index.duplicated(keep="last")]
    out = pd.DataFrame(index=df.index)
    out["quarter"] = df["quarter"]
    out["agg_shares"] = df["shares"].astype(float)
    # A brand-new position (prev 0 shares) makes pct_change infinite — clip so it reads as
    # strong accumulation rather than poisoning the column with inf.
    out["qoq_change_pct"] = (out["agg_shares"].pct_change() * 100).clip(upper=500)
    out["n_managers"] = df["managers"].astype(int)
    return out


def get_panel() -> Optional[dict]:
    """The last-built S&P-500 panel from cache, or None if it hasn't been built yet.
    Read-only — never triggers the heavy network build (callers degrade gracefully)."""
    return cache.get("panel:US")


def get_institutional_series(ticker: str) -> Optional[pd.DataFrame]:
    """Historical 13F series for one US ticker (indexed by filing date), read from the
    cached panel. None if the panel isn't built, the ticker isn't held by any tracked
    manager, or EDGAR is unavailable — callers fall back to the yfinance snapshot."""
    panel = get_panel()
    if not panel:
        return None
    return series_from_records(panel.get(ticker.upper(), []))


def enrich_frames(df_by_ticker: dict) -> None:
    """Merge the 13F series onto each US ticker's daily frame (columns `inst13f_shares` /
    `inst13f_qoq`), forward-filled from each filing date so a backtest can never see a filing
    before it was public. No-op when the panel hasn't been built; mutates in place. Never raises
    — the run proceeds without 13F on failure. (Called via smart_money.enrich_frames for US.)"""
    try:
        panel = get_panel()
        if not panel:
            logger.info("13F panel not built — institutional-13f strategy is inert this run")
            return
        merged = 0
        for tk, df in df_by_ticker.items():
            recs = panel.get(tk.upper())
            series = series_from_records(recs) if recs else None
            if series is None or series.empty:
                continue
            df["inst13f_shares"] = series["agg_shares"].reindex(df.index, method="ffill")
            df["inst13f_qoq"] = series["qoq_change_pct"].reindex(df.index, method="ffill")
            merged += 1
        logger.info("13F enrichment: merged holdings onto %d/%d tickers", merged, len(df_by_ticker))
    except Exception as e:
        logger.warning("13F enrichment skipped: %s", e)
