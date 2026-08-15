"""
US Congressional trades — the US market's disclosed "political insider" smart money.

The STOCK Act requires members of Congress to file a Periodic Transaction Report (PTR) within ~45 days
of a trade. Those filings are public and keyless at the House Clerk's disclosure site — a second US
smart-money *actor family* alongside the SEC-13F institutions (services/edgar_13f.py), captured through
the SAME normalized signal so it flows into the coattail basket / ranking pillar / badge automatically.

Two feeds, both keyless:
  • an annual XML INDEX  — `.../financial-pdfs/{YEAR}FD.ZIP` → `{YEAR}FD.xml`, listing every filing with
    its member, type, filing date and DocID. We keep the `FilingType == 'P'` (PTR) rows.
  • the per-filing PTR PDF — `.../ptr-pdfs/{YEAR}/{DocID}.pdf`. Recent (e-filed) reports are TEXT PDFs we
    parse with pypdf; older SCANNED reports extract no text and are skipped gracefully (no OCR).

Everything is disclosure-dated by the PUBLIC notification/filing date (never the older transaction date)
so a backtest can't see a trade before it was disclosed. Mirrors the edgar_13f panel pattern: a bounded,
disk-cached panel (recent filings only — a full year is ~500 PDFs) built explicitly, then read live.

Implements the smart_money source-module contract — `live_signal(symbol)`, `series(symbol)`,
`enrich_frames(df_by_ticker)` — plus `build_panel` / `get_panel` / `leaders`. Degrades to None / no-op on
any failure (network, a missing pypdf dependency, a scanned filing); never raises into the caller.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from datetime import datetime, timezone
from typing import Optional

import diskcache
import pandas as pd
import requests

from pipeline_core.config import CACHE_DIR, settings

logger = logging.getLogger(__name__)
cache = diskcache.Cache(str(CACHE_DIR / "congress"))

INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.ZIP"
PTR_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{docid}.pdf"
INDEX_TTL = 12 * 3600               # the annual index keeps growing through the year → refresh daily-ish
PTR_TTL = 365 * 24 * 3600           # a filed PTR is immutable — cache it effectively forever
PANEL_TTL = 12 * 3600
_HTTP_TIMEOUT = 25
_UA = ("Mozilla/5.0 (StockSense/1.0; +set sec_user_agent) AppleWebKit/537.36")

# A transaction row in the extracted PTR text. pypdf collapses the table columns onto adjacent tokens,
# e.g.  "... (GOOGL) [ST]\nP 04/03/202504/16/2025$1,001 - $15,000" — so we capture ticker + asset-type,
# then the action (P/S/E, optionally "(partial)"/"(full)"), the transaction & notification dates, and the
# dollar range in one shot. Validated against real 2025 filings (single- and multi-row, incl. "S (partial)").
_ROW = re.compile(
    r"\(([A-Z][A-Z0-9.\-]{0,6})\)\s*\[[A-Z]{1,3}\]\s*"          # (TICKER) [ASSET-TYPE]
    r"([PSE])(?:\s*\((?:partial|full)\))?\s*"                    # action: P(urchase)/S(ale)/E(xchange)
    r"(\d{2}/\d{2}/\d{4})\s*(\d{2}/\d{2}/\d{4})\s*"              # transaction date, notification date
    r"\$([\d,]+)\s*-\s*\$([\d,]+)",                              # $low - $high
    re.DOTALL,
)


def _ua() -> str:
    return getattr(settings, "sec_user_agent", None) or _UA


# ── Feed 1: the annual filing index (XML) ───────────────────────────

def _fetch_index(year: int) -> list[dict]:
    """The `FilingType == 'P'` (PTR) rows of one year's House disclosure index, disk-cached. Network."""
    ck = f"idx:{year}"
    hit = cache.get(ck)
    if hit is not None:
        return hit
    rows: list[dict] = []
    try:
        resp = requests.get(INDEX_URL.format(year=year), headers={"User-Agent": _ua()},
                            timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            name = next((n for n in zf.namelist() if n.lower().endswith(".xml")), None)
            xml = zf.read(name) if name else b""
        rows = parse_index(xml)
    except Exception as e:
        logger.warning("congress index fetch failed for %s: %s", year, e)
        return []
    cache.set(ck, rows, expire=INDEX_TTL)
    return rows


def parse_index(xml: bytes) -> list[dict]:
    """House FD XML → list of PTR filing dicts (member, docid, year, filing_date). Pure — unit-tested."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml)
    except Exception:
        return []
    out = []
    for m in root:
        if (m.findtext("FilingType") or "").strip() != "P":
            continue
        docid = (m.findtext("DocID") or "").strip()
        if not docid:
            continue
        first, last = (m.findtext("First") or "").strip(), (m.findtext("Last") or "").strip()
        out.append({
            "docid": docid,
            "member": f"{first} {last}".strip(),
            "year": (m.findtext("Year") or "").strip(),
            "state_dst": (m.findtext("StateDst") or "").strip(),
            "filing_date": _parse_date(m.findtext("FilingDate")),
        })
    return out


# ── Feed 2: the per-filing PTR PDF (text) ───────────────────────────

def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """PTR PDF bytes → text, or "" if pypdf is unavailable / the PDF is a scan / extraction fails.
    pypdf is imported lazily so the whole source degrades to no-op when the dependency isn't present."""
    try:
        import pypdf
    except Exception:
        logger.warning("congress: pypdf not installed — PTR parsing disabled (source no-ops)")
        return ""
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as e:
        logger.debug("congress PDF extract failed: %s", e)
        return ""


def parse_ptr_text(text: str) -> list[dict]:
    """Extracted PTR text → transaction rows [{ticker, action, txn_date, notif_date, amount}]. `action`
    is 'buy' (P) / 'sell' (S); exchanges (E) are dropped. `amount` is the midpoint of the disclosed range.
    Empty for a scanned filing (no text) or a report with no tickered securities. Pure — unit-tested."""
    rows = []
    for tk, act, tdate, ndate, lo, hi in _ROW.findall(text or ""):
        if act == "E":
            continue
        rows.append({
            "ticker": tk.upper(),
            "action": "buy" if act == "P" else "sell",
            "txn_date": _parse_date(tdate),
            "notif_date": _parse_date(ndate),
            "amount": _amount_mid(lo, hi),
        })
    return rows


def _fetch_ptr(docid: str, year: str) -> list[dict]:
    """Transactions for one PTR (disk-cached forever — a filed report is immutable). Scanned/old filings
    extract no text → []. Network on a cache miss; never raises."""
    ck = f"ptr:{year}:{docid}"
    hit = cache.get(ck)
    if hit is not None:
        return hit
    rows: list[dict] = []
    try:
        resp = requests.get(PTR_URL.format(year=year, docid=docid), headers={"User-Agent": _ua()},
                            timeout=_HTTP_TIMEOUT)
        if resp.status_code == 200:
            rows = parse_ptr_text(_extract_pdf_text(resp.content))
    except Exception as e:
        logger.debug("congress PTR fetch failed %s/%s: %s", year, docid, e)
        return []                          # transient — don't poison the cache
    cache.set(ck, rows, expire=PTR_TTL)
    return rows


# ── Panel: aggregate recent PTRs → per-ticker disclosed flow ────────

def build_panel(years: Optional[list[int]] = None, max_filings: Optional[int] = None,
                force: bool = False) -> dict[str, list[dict]]:
    """Build (and cache) the congress panel: {TICKER: [disclosed transaction records]}, each record dated
    by its PUBLIC disclosure date. Bounded to the most recent `max_filings` PTRs across `years` (a full
    year is ~500 PDFs) — per-DocID caching makes a daily rebuild incremental. Cached under `panel:CONGRESS`.
    Resilient: a failed / scanned filing is skipped. Returns {} on total failure; never raises."""
    if not force:
        cached = cache.get("panel:CONGRESS")
        if cached is not None:
            return cached
    if years is None:
        this_year = datetime.now(timezone.utc).year
        n = max(1, int(getattr(settings, "congress_lookback_years", 2)))
        years = list(range(this_year - n + 1, this_year + 1))
    cap = int(max_filings if max_filings is not None else getattr(settings, "congress_max_filings", 250))

    filings: list[dict] = []
    for y in years:
        filings.extend(_fetch_index(y))
    # Most recently DISCLOSED first, then keep the bounded head — recent activity is what a live coattail
    # signal needs, and it caps the first-build cost.
    filings.sort(key=lambda f: f.get("filing_date") or "", reverse=True)
    filings = filings[:cap]

    panel: dict[str, list[dict]] = {}
    parsed = 0
    for f in filings:
        txns = _fetch_ptr(f["docid"], f["year"])
        if not txns:
            continue
        parsed += 1
        for tx in txns:
            # Public on the max of (notification, index filing) date — never the older transaction date.
            disc = max(d for d in (tx.get("notif_date"), f.get("filing_date")) if d) if \
                (tx.get("notif_date") or f.get("filing_date")) else None
            if not disc:
                continue
            panel.setdefault(tx["ticker"], []).append({
                "date": disc, "member": f.get("member"),
                "action": tx["action"], "amount": tx["amount"],
            })
    for recs in panel.values():
        recs.sort(key=lambda r: r["date"])
    logger.info("congress panel: %d tickers from %d/%d parsed filings", len(panel), parsed, len(filings))
    cache.set("panel:CONGRESS", panel, expire=PANEL_TTL)
    return panel


def get_panel() -> Optional[dict]:
    """The last-built congress panel from cache, or None if it hasn't been built yet (call build_panel)."""
    return cache.get("panel:CONGRESS")


def series_from_records(records: list[dict]) -> Optional[pd.DataFrame]:
    """A ticker's disclosed-flow records → a daily series indexed by disclosure date: `congress_net` (a
    trailing net buy−sell dollar flow) + `congress_buys`/`congress_sells` counts. Pure — unit-tested."""
    if not records:
        return None
    df = pd.DataFrame(records)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date")
    if df.empty:
        return None
    signed = df["amount"].where(df["action"] == "buy", -df["amount"])
    daily = pd.DataFrame({
        "congress_flow": signed.values,
        "congress_buys": (df["action"] == "buy").astype(int).values,
        "congress_sells": (df["action"] == "sell").astype(int).values,
    }, index=pd.DatetimeIndex(df["date"].values))
    daily = daily.groupby(level=0).sum()
    win = int(getattr(settings, "congress_flow_window_days", 120))
    out = pd.DataFrame(index=daily.index)
    out["congress_net"] = daily["congress_flow"].rolling(f"{win}D").sum()
    out["congress_buys"] = daily["congress_buys"].rolling(f"{win}D").sum()
    out["congress_sells"] = daily["congress_sells"].rolling(f"{win}D").sum()
    return out


def series(symbol: str) -> Optional[pd.DataFrame]:
    """The disclosed congress-flow series for one ticker (disclosure-dated), or None."""
    panel = get_panel()
    if not panel:
        return None
    return series_from_records(panel.get(symbol.strip().upper(), []))


def signal_from_records(records: list[dict]) -> Optional[dict]:
    """Latest congress reading for a ticker → normalized smart-money signal, or None. Pure — unit-tested.
    Direction from the trailing net buy/sell dollar flow; conviction from breadth (distinct members)."""
    s = series_from_records(records)
    if s is None or s.empty:
        return None
    net = float(s["congress_net"].iloc[-1])
    buys = int(s["congress_buys"].iloc[-1])
    sells = int(s["congress_sells"].iloc[-1])
    members = sorted({r.get("member") for r in records if r.get("member")})
    n_actors = len(members)
    direction = "accumulate" if net > 0 else "distribute" if net < 0 else "neutral"
    verb = "bought" if direction == "accumulate" else "sold" if direction == "distribute" else "traded"
    thresh_high = float(getattr(settings, "congress_conviction_amount", 100_000))
    conviction = ("high" if (abs(net) >= thresh_high or n_actors >= 3)
                  else "medium" if (abs(net) > 0 or n_actors >= 2) else None) \
        if direction != "neutral" else None
    return {
        "available": True,
        "source": "house-clerk",
        "family": "congress",
        "direction": direction,
        "net_delta_pct": round(net / thresh_high * 100, 1),   # net flow scaled to a −100..+100-ish read
        "conviction": conviction,
        "actors": members[:8],
        "n_actors": n_actors,
        "label": "Congress",
        "detail": f"{n_actors} member{'s' if n_actors != 1 else ''} net {verb} "
                  f"${abs(net):,.0f} ({buys}B/{sells}S, {int(getattr(settings, 'congress_flow_window_days', 120))}d)",
        "as_of": s.index[-1].tz_localize(timezone.utc).isoformat() if s.index[-1].tzinfo is None
                 else s.index[-1].isoformat(),
    }


def live_signal(symbol: str) -> Optional[dict]:
    """The live congress overlay for one ticker (smart_money source-module contract). None if unbuilt."""
    panel = get_panel()
    if not panel:
        return None
    return signal_from_records(panel.get(symbol.strip().upper(), []))


def _ffill_onto(src: pd.Series, target_index) -> pd.Series:
    """Forward-fill a (disclosure-dated, tz-naive) column onto a daily price index, tolerating a tz-aware
    price index (mirrors cot._ffill_onto)."""
    tgt = target_index.tz_localize(None) if getattr(target_index, "tz", None) is not None else target_index
    s = src
    if getattr(s.index, "tz", None) is not None:
        s = s.copy()
        s.index = s.index.tz_localize(None)
    aligned = s.sort_index().reindex(tgt, method="ffill")
    aligned.index = target_index
    return aligned


def enrich_frames(df_by_ticker: dict) -> None:
    """Merge `congress_net` onto each frame whose ticker appears in the panel (forward-filled from the
    disclosure date). Mutates in place; skips unknown symbols; per-symbol failures isolated. No-op when
    the panel isn't built. Never raises."""
    panel = get_panel()
    if not panel:
        return
    merged = 0
    for tk, df in df_by_ticker.items():
        try:
            recs = panel.get(str(tk).upper())
            s = series_from_records(recs) if recs else None
            if s is None or s.empty:
                continue
            df["congress_net"] = _ffill_onto(s["congress_net"], df.index)
            merged += 1
        except Exception as e:      # pragma: no cover — defensive
            logger.warning("congress enrichment skipped for %s: %s", tk, e)
    if merged:
        logger.info("congress enrichment: merged flow onto %d/%d frames", merged, len(df_by_ticker))


def leaders(limit: int = 10) -> dict:
    """The coattail 'who is Congress buying now' board — tickers ranked by trailing net disclosed buying.
    Reads the cached panel; empty (with a note) when it hasn't been built yet. Never raises."""
    panel = get_panel()
    if not panel:
        return {"market": "US", "available": False, "leaders": [],
                "note": "Congress panel not built — call refresh_congress.", "source": "house-clerk"}
    rows = []
    for tk, recs in panel.items():
        sig = signal_from_records(recs)
        if not sig or sig.get("direction") != "accumulate":
            continue
        rows.append({"ticker": tk, "net": round(float(sig["net_delta_pct"]), 1),
                     "n_members": sig["n_actors"], "detail": sig["detail"]})
    rows.sort(key=lambda r: r["net"], reverse=True)
    return {"market": "US", "available": bool(rows), "leaders": rows[:limit], "source": "house-clerk"}


# ── small helpers ───────────────────────────────────────────────────

def _parse_date(s: Optional[str]) -> Optional[str]:
    """A House date (M/D/YYYY or MM/DD/YYYY) → ISO 'YYYY-MM-DD', or None."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _amount_mid(lo: str, hi: str) -> float:
    """Midpoint of a disclosed '$1,001 - $15,000' range (the House discloses bands, not exact amounts)."""
    try:
        return (float(lo.replace(",", "")) + float(hi.replace(",", ""))) / 2.0
    except Exception:
        return 0.0
