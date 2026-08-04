"""
Curated thematic baskets for Indian equities.

Themes like "AI" or "green energy" do not map cleanly to yfinance's GICS-style
sectors, so each theme is a **hand-curated list of NSE symbols** (the honest,
auditable approach). `sector_hints` provide an optional keyword fallback used only
to *widen* a curated basket during construction (a stock in a matching sector can
be admitted), never to replace it.

NOTE: constituent symbols drift over time (e.g. ZOMATO→ETERNAL, AMARAJABAT→ARE&M).
Membership here should be validated against the live NIFTY 500 periodically.
Symbols are bare NSE tickers (no ".NS"), matching the rest of the codebase.
"""

from __future__ import annotations

from typing import Optional

# slug -> {name, description, tickers, sector_hints}
THEMES: dict[str, dict] = {
    "ai-it": {
        "name": "AI & IT Services",
        "description": "Indian IT majors and mid-caps riding AI/digital-transformation demand.",
        "tickers": [
            "TCS", "INFY", "WIPRO", "HCLTECH", "TECHM", "LTIM", "PERSISTENT",
            "COFORGE", "MPHASIS", "LTTS", "TATAELXSI", "ZENSARTECH", "KPITTECH",
            "BSOFT", "OFSS", "CYIENT",
        ],
        "sector_hints": ["Technology", "Software", "Information Technology", "IT Services"],
    },
    "green-energy": {
        "name": "Green & Renewable Energy",
        "description": "Renewable generation, clean-power utilities, and wind/solar equipment.",
        "tickers": [
            "NTPC", "POWERGRID", "TATAPOWER", "JSWENERGY", "ADANIGREEN", "ADANIENSOL",
            "SUZLON", "INOXWIND", "WAAREEENER", "PREMIERENE", "NHPC", "SJVN",
            "IREDA", "RPOWER", "GENUSPOWER",
        ],
        "sector_hints": ["Utilities", "Renewable", "Power", "Solar", "Wind", "Energy"],
    },
    "ev": {
        "name": "Electric Vehicles & Auto",
        "description": "OEMs pushing electrification plus the EV component supply chain.",
        "tickers": [
            "TATAMOTORS", "M&M", "MARUTI", "BAJAJ-AUTO", "HEROMOTOCO", "TVSMOTOR",
            "EICHERMOT", "OLAELEC", "EXIDEIND", "ARE&M", "SONACOMS", "UNOMINDA",
            "BOSCHLTD", "MOTHERSON", "SAMVARDHANA",
        ],
        "sector_hints": ["Auto", "Automobile", "Auto Components", "Consumer Cyclical"],
    },
    "defense": {
        "name": "Defense & Aerospace",
        "description": "Defence PSUs and private players benefiting from indigenisation.",
        "tickers": [
            "HAL", "BEL", "BDL", "MAZDOCK", "COCHINSHIP", "DATAPATTNS", "PARAS",
            "ZENTEC", "BEML", "GRSE", "MTARTECH", "SOLARINDS", "ASTRAMICRO",
        ],
        "sector_hints": ["Aerospace", "Defense", "Defence", "Industrials"],
    },
    "fintech-digital": {
        "name": "Fintech & Digital Platforms",
        "description": "New-economy digital platforms, exchanges, and market-infrastructure firms.",
        "tickers": [
            "PAYTM", "POLICYBZR", "ETERNAL", "NYKAA", "ANGELONE", "CDSL", "BSE",
            "MCX", "KFINTECH", "CAMS", "PBFINTECH", "JIOFIN", "IEX",
        ],
        "sector_hints": ["Financial", "Capital Markets", "Internet", "Fintech", "Financial Services"],
    },
    "pharma-health": {
        "name": "Pharma & Healthcare",
        "description": "Pharmaceutical manufacturers and hospital/diagnostics chains.",
        "tickers": [
            "SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB", "LUPIN", "AUROPHARMA",
            "ZYDUSLIFE", "ALKEM", "TORNTPHARM", "APOLLOHOSP", "MAXHEALTH",
            "FORTIS", "LAURUSLABS", "MANKIND", "BIOCON",
        ],
        "sector_hints": ["Healthcare", "Pharmaceutical", "Pharma", "Hospital", "Drug"],
    },
    "psu-banks": {
        "name": "PSU Banks",
        "description": "Public-sector banks — a cyclical, rate-sensitive value pocket.",
        "tickers": [
            "SBIN", "BANKBARODA", "PNB", "CANBK", "UNIONBANK", "INDIANB",
            "BANKINDIA", "CENTRALBK", "IOB", "MAHABANK", "UCOBANK", "PSB",
        ],
        "sector_hints": ["Bank", "Banks", "Financial Services"],
    },
    "consumption": {
        "name": "Consumption & FMCG",
        "description": "Staples, discretionary retail, and branded-consumer plays.",
        "tickers": [
            "HINDUNILVR", "ITC", "NESTLEIND", "BRITANNIA", "TATACONSUM", "DABUR",
            "GODREJCP", "MARICO", "COLPAL", "VBL", "TITAN", "TRENT", "DMART",
            "JUBLFOOD", "UNITDSPR",
        ],
        "sector_hints": ["Consumer", "FMCG", "Retail", "Beverages", "Food", "Consumer Defensive"],
    },
    "manufacturing": {
        "name": "Manufacturing & Capital Goods",
        "description": "Capital-goods, electricals, and Make-in-India industrial names.",
        "tickers": [
            "LT", "SIEMENS", "ABB", "BHEL", "CUMMINSIND", "THERMAX", "POLYCAB",
            "HAVELLS", "DIXON", "KAYNES", "BHARATFORG", "AIAENG", "SKFINDIA", "TIMKEN",
        ],
        "sector_hints": ["Industrial", "Capital Goods", "Electrical Equipment", "Machinery", "Industrials"],
    },
    "infra-realty": {
        "name": "Infrastructure & Realty",
        "description": "Construction, ports/airports, roads, and real-estate developers.",
        "tickers": [
            "LT", "ADANIPORTS", "DLF", "GODREJPROP", "OBEROIRLTY", "PRESTIGE",
            "LODHA", "GMRAIRPORT", "IRB", "KNRCON", "PNCINFRA", "NBCC", "RVNL", "IRCON",
        ],
        "sector_hints": ["Real Estate", "Realty", "Construction", "Infrastructure", "Ports"],
    },
}


def _norm_tickers(tickers: list[str]) -> list[str]:
    """Uppercase + de-dupe, preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for t in tickers:
        u = t.strip().upper()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def list_themes() -> list[dict]:
    """Catalog of every theme (slug, name, description, ticker count)."""
    return [
        {
            "slug": slug,
            "name": t["name"],
            "description": t["description"],
            "count": len(_norm_tickers(t["tickers"])),
        }
        for slug, t in THEMES.items()
    ]


def get_theme(slug: Optional[str]) -> Optional[dict]:
    """Full theme record (with normalized tickers) or None."""
    if not slug:
        return None
    t = THEMES.get(slug.strip().lower())
    if t is None:
        return None
    return {**t, "slug": slug.strip().lower(), "tickers": _norm_tickers(t["tickers"])}


def theme_universe(slug: Optional[str]) -> set[str]:
    """Set of curated tickers for a theme (empty set if slug is unknown/None)."""
    t = get_theme(slug)
    return set(t["tickers"]) if t else set()


def sector_matches_theme(slug: Optional[str], sector: Optional[str], industry: Optional[str] = None) -> bool:
    """True if a stock's sector/industry keyword-matches the theme's hints.
    Used only to *widen* a curated basket, never to replace it."""
    t = get_theme(slug)
    if t is None:
        return False
    hay = f"{sector or ''} {industry or ''}".lower()
    return any(h.lower() in hay for h in t.get("sector_hints", []))
