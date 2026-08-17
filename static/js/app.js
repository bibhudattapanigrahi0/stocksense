/* StockSense dashboard — vanilla JS, no build step */

"use strict";

// ── helpers ──────────────────────────────────────────────────────

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
}

function fmt(n, digits = 1) {
    if (n === null || n === undefined || Number.isNaN(n)) return "—";
    return Number(n).toLocaleString("en-IN", { maximumFractionDigits: digits });
}

function scoreColor(s) {
    if (s === null || s === undefined) return "var(--text-dim)";
    return s >= 65 ? "var(--accent)" : s >= 45 ? "var(--warn)" : "var(--danger)";
}

function signalPill(signal) {
    if (!signal) return "";
    const cls = { BUY: "pill-buy", HOLD: "pill-hold", SELL: "pill-sell" }[signal] || "";
    return `<span class="pill ${cls}">${esc(signal)}</span>`;
}

function riskPill(level) {
    if (!level) return "";
    const cls = { Low: "pill-low", Medium: "pill-medium", High: "pill-high" }[level] || "";
    return `<span class="pill ${cls}">${esc(level)} risk</span>`;
}

function toast(msg, isError = false) {
    const el = $("#toast");
    el.textContent = msg;
    el.classList.toggle("error", isError);
    el.classList.remove("hidden");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.classList.add("hidden"), 4000);
}

async function api(path, opts = {}) {
    const res = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...opts,
    });
    if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try { detail = (await res.json()).detail || detail; } catch { /* non-JSON */ }
        // A 401 on a protected call means the session expired — surface the login screen.
        if (res.status === 401 && !path.startsWith("/api/v1/auth/") && typeof onUnauthorized === "function") {
            onUnauthorized();
        }
        throw new Error(detail);
    }
    return res.json();
}

// ── Theme (light / dark / system) ────────────────────────────────
// The <head> sets data-theme before first paint (no FOUC). Here we own the
// toggle, persistence, the chart repaint, and following the OS when in "system".

const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// Chart.js colors, read live from the active theme's CSS tokens.
function chartTheme() {
    return {
        brand: cssVar("--brand") || "#8b82ff",
        brand2: cssVar("--brand-2") || "#22d3ee",
        pos: cssVar("--pos") || "#34d399",
        text: cssVar("--text") || "#e8edf6",
        dim: cssVar("--text-dim") || "#93a1b8",
        grid: cssVar("--border") || "#232d43",
    };
}
// rgba fill from a hex token (for chart area fills).
function hexTint(hex, alpha) {
    const h = String(hex || "").replace("#", "");
    if (h.length !== 6) return `rgba(139,130,255,${alpha})`;
    const r = parseInt(h.slice(0, 2), 16), g = parseInt(h.slice(2, 4), 16), b = parseInt(h.slice(4, 6), 16);
    return `rgba(${r},${g},${b},${alpha})`;
}

const currentTheme = () => localStorage.getItem("ss-theme") || "system";   // 'light' | 'dark' | 'system'
const systemDark = () => !window.matchMedia || window.matchMedia("(prefers-color-scheme: dark)").matches;
const resolvedTheme = () => { const t = currentTheme(); return t === "system" ? (systemDark() ? "dark" : "light") : t; };

// ── Accent + density personalization ─────────────────────────────
// Declared before applyTheme so the boot-time apply can re-map the accent safely.
// Each accent carries a color for dark (d) / light (l) grounds + a stronger hover
// variant (sd / sl). Brand accents deliberately avoid green/red (those mean P&L).
const ACCENTS = {
    indigo:  { d: "#8b82ff", l: "#6d63f5", sd: "#6d63f5", sl: "#5a50e0" },
    violet:  { d: "#c084fc", l: "#9333ea", sd: "#a855f7", sl: "#7e22ce" },
    blue:    { d: "#60a5fa", l: "#2563eb", sd: "#3b82f6", sl: "#1d4ed8" },
    teal:    { d: "#2dd4bf", l: "#0d9488", sd: "#14b8a6", sl: "#0f766e" },
    fuchsia: { d: "#e879f9", l: "#c026d3", sd: "#d946ef", sl: "#a21caf" },
};
const APPEARANCE = { accent: "indigo", density: "comfortable" };

function _accentVars(resolved) {
    const a = ACCENTS[APPEARANCE.accent] || ACCENTS.indigo;
    const dark = resolved === "dark";
    const s = document.documentElement.style;
    s.setProperty("--brand", dark ? a.d : a.l);
    s.setProperty("--brand-strong", dark ? a.sd : a.sl);
    s.setProperty("--brand-tint", (dark ? a.d : a.l) + "26");   // ~15% alpha (8-digit hex)
}
function applyAccent(name) {
    APPEARANCE.accent = ACCENTS[name] ? name : "indigo";
    _accentVars(document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light");
    redrawCharts();
}
function applyDensity(val) {
    APPEARANCE.density = val === "compact" ? "compact" : "comfortable";
    document.documentElement.setAttribute("data-density", APPEARANCE.density);
}

// Apply a theme choice: stamp <html>, update the icon, re-map the accent, repaint charts.
function applyTheme(theme, { persist = true } = {}) {
    if (persist) localStorage.setItem("ss-theme", theme);
    const resolved = theme === "system" ? (systemDark() ? "dark" : "light") : theme;
    document.documentElement.setAttribute("data-theme", resolved);
    const icon = $("#theme-icon");
    if (icon) icon.textContent = resolved === "dark" ? "☾" : "☀";
    _accentVars(resolved);
    redrawCharts();
}
// Explicit light↔dark toggle (drops "system" once the user picks a side), persisted.
function toggleTheme() {
    applyTheme(resolvedTheme() === "dark" ? "light" : "dark");
    if (typeof persistPrefs === "function") persistPrefs();
}

// Recolor every live Chart so grid/ticks/legend/title track the theme (series
// colors are theme-appropriate already and left intact).
function redrawCharts() {
    if (!window.Chart || !Chart.instances) return;
    const t = chartTheme();
    for (const ch of Object.values(Chart.instances)) {
        try {
            const o = ch.options || {};
            if (o.scales) for (const ax of Object.values(o.scales)) {
                if (ax.grid && ax.grid.color) ax.grid.color = t.grid;
                if (ax.ticks) ax.ticks.color = t.dim;
                if (ax.title && ax.title.display) ax.title.color = t.dim;
            }
            if (o.plugins && o.plugins.legend && o.plugins.legend.labels) o.plugins.legend.labels.color = t.text;
            ch.update("none");
        } catch { /* ignore a chart that can't be recolored */ }
    }
}

{
    const tt = $("#theme-toggle");
    if (tt) tt.addEventListener("click", toggleTheme);
    applyTheme(currentTheme(), { persist: false });    // sync the icon to the head-set theme
    if (window.matchMedia) {
        window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
            if (currentTheme() === "system") applyTheme("system", { persist: false });
        });
    }
}

// ── market registry (India / US / Metals / Crypto) ───────────────

const MARKET = { key: "IN", byKey: {}, list: [] };
const mkt = () => MARKET.byKey[MARKET.key] || {};
const mcapUnit = () => mkt().mcap_unit || "";
const _CUR_SYM = { INR: "₹", USD: "$" };
const curSym = () => _CUR_SYM[mkt().currency] || "";
// Magnitude-aware price decimals — sub-cent crypto (PEPE, SHIB, BONK, …) needs many
// places or it renders as 0. Mirrors the backend utils.round_price.
const _priceDigits = (a) => (a >= 1 ? 2 : a >= 0.01 ? 4 : a >= 0.0001 ? 6 : 8);
// Price number (no symbol), correct for both a $64k coin and a $0.0000029 coin.
function pfmt(v) {
    if (v === null || v === undefined || Number.isNaN(v)) return "—";
    return fmt(v, _priceDigits(Math.abs(v)));
}
// Price with the market's currency symbol.
function priceFmt(v) {
    if (v === null || v === undefined || Number.isNaN(v)) return "—";
    return curSym() + pfmt(v);
}
const benchLabel = () => mkt().benchmark_label || "NIFTY 50";
const marketHasFundamentals = () => mkt().has_fundamentals !== false;
// Append market to a query string (?/& chosen automatically).
const withMarket = (path) => path + (path.includes("?") ? "&" : "?") + "market=" + encodeURIComponent(MARKET.key);

async function loadMarkets() {
    try {
        const { markets } = await api("/api/v1/markets");
        MARKET.list = markets;
        MARKET.byKey = Object.fromEntries(markets.map((m) => [m.key, m]));
        const sel = $("#market-select");
        sel.innerHTML = markets.map((m) => `<option value="${esc(m.key)}">${esc(m.label)}</option>`).join("");
        sel.value = MARKET.key;
        sel.addEventListener("change", onMarketChange);
    } catch { /* keep IN defaults if the registry can't load */ }
    applyMarketUI();
}

function onMarketChange() {
    setMarket($("#market-select").value, { updateHash: true });
}

// Switch the active market (from the dropdown or a shared URL). `updateHash` reflects it in the
// URL as ?m= (via replaceState, so market toggles don't pollute browser back-history).
function setMarket(key, { updateHash = false } = {}) {
    if (!key || !MARKET.byKey[key]) return;
    MARKET.key = key;
    const sel = $("#market-select");
    if (sel && sel.value !== key) sel.value = key;
    // Invalidate all market-scoped caches so the tabs refetch for the new market.
    LAB_DATA = null; IDEAS_DATA = null; TRACKED_DATA = null; IDEAS_LOADED = false; PF_LOADED = false;
    MB_DATA = null; MB_LOADED = false; MB_TRACKED_DATA = null;   // multi-bagger screen + tracker are market-scoped
    applyMarketUI();
    // Clear stale results from the previous market.
    $("#screen-results").innerHTML = "";
    $("#screen-status").textContent = "";

    // Curated strategies + the Strategy Lab are market-specific — refresh them.
    loadStrategies();
    loadLabLeaderboard();
    if ($("#lab-discover") && $("#lab-discover").style.display !== "none") loadDiscover();

    // Re-load whichever data-driven tab is currently open (others reload lazily on view).
    const active = $(".tab.active") && $(".tab.active").dataset.tab;
    if (active === "ideas") {
        if ($("#ideas-multibagger-view") && !$("#ideas-multibagger-view").classList.contains("hidden")) {
            loadMultibaggers(); loadMbTracker();   // multi-bagger sub-view is showing — re-scope to the new market
        } else { loadTradeIdeas(); loadTrackedIdeas(); IDEAS_LOADED = true; }
    }
    else if (active === "portfolio") initPortfolio();
    else if (active === "watchlist") loadWatchlist();
    else if (active === "paper") loadPaper();
    if (typeof refreshAlerts === "function") refreshAlerts();   // alerts are market-scoped

    if (updateHash) syncMarketToHash();
}

function applyMarketUI() {
    const m = mkt();
    // Data-driven universe dropdown.
    const idx = $("#f-index");
    if (m.universes && m.universes.length) {
        idx.innerHTML = m.universes.map((u) => `<option>${esc(u)}</option>`).join("");
        idx.value = m.default_universe || m.universes[0];
    }
    // Brand subtitle.
    if ($("#brand-sub")) $("#brand-sub").textContent = `${m.label || "India (NSE)"} · agentic pipeline`;
    // Fundamentals-only filter groups (valuation/growth/profitability/sector).
    const isEquity = marketHasFundamentals();
    $$("[data-mkind='equity']").forEach((el) => { el.style.display = isEquity ? "" : "none"; });
    // Market-specific groups (India promoter/pledge/delivery).
    $$("[data-market]").forEach((el) => { el.style.display = el.dataset.market === m.key ? "" : "none"; });
    // Market-cap fields — hidden when the market has no market cap (metals).
    const hasMcap = !!m.mcap_unit;
    $$("[data-mcap]").forEach((el) => { el.style.display = hasMcap ? "" : "none"; });
    // Unit labels (Cr / B).
    $$(".mcap-unit").forEach((el) => { el.textContent = m.mcap_unit || ""; });
    // Portfolio theme tilt is a curated India-sector feature — hide it off-IN.
    const themeField = $("#pf-theme") && $("#pf-theme").closest(".field");
    if (themeField) {
        themeField.style.display = m.key === "IN" ? "" : "none";
        if (m.key !== "IN") $("#pf-theme").value = "";
    }
}

// ── Tooltips (custom floating; native `title` is slow/unreliable) ─

let _tipEl = null;
function _getTip() {
    if (!_tipEl) {
        _tipEl = document.createElement("div");
        _tipEl.className = "floating-tip";
        document.body.appendChild(_tipEl);
    }
    return _tipEl;
}
function showTip(el) {
    const text = el.getAttribute("data-tip");
    if (!text) return;
    const tip = _getTip();
    tip.textContent = text;
    tip.style.display = "block";
    const r = el.getBoundingClientRect();
    const tr = tip.getBoundingClientRect();
    let top = r.top - tr.height - 8;                 // prefer above
    if (top < 6) top = r.bottom + 8;                 // flip below if no room
    let left = r.left + r.width / 2 - tr.width / 2;   // centered
    left = Math.max(8, Math.min(left, window.innerWidth - tr.width - 8));
    tip.style.top = `${top}px`;
    tip.style.left = `${left}px`;
    tip.classList.add("show");
}
function hideTip() {
    if (_tipEl) { _tipEl.classList.remove("show"); _tipEl.style.display = "none"; }
}
document.addEventListener("mouseover", (e) => {
    const el = e.target.closest("[data-tip]");
    if (el) showTip(el); else hideTip();
});
document.addEventListener("mouseleave", hideTip, true);
window.addEventListener("scroll", hideTip, true);

// ── tabs ─────────────────────────────────────────────────────────

// ── Hash router ──────────────────────────────────────────────────
// Each tab is a real URL (e.g. #/trade-ideas) so browser Back/Forward, refresh, and shared
// links all work. Clicks set the hash; a single hashchange handler applies the route.

const TAB_SLUGS = {
    home: "home",
    screener: "screener", strategies: "strategies", lab: "strategy-lab",
    ideas: "trade-ideas", watchlist: "watchlist", portfolio: "portfolio", paper: "paper", bots: "bots",
    pipeline: "pipeline", analyze: "analyze", reports: "reports", history: "history",
};
const SLUG_TABS = Object.fromEntries(Object.entries(TAB_SLUGS).map(([t, s]) => [s, t]));
const DEFAULT_TAB = "home";

// Show a tab in the DOM + run its lazy loader. No history side-effects — the router owns those.
function activateTab(tab) {
    const btn = $$(".tab").find((b) => b.dataset.tab === tab);
    if (!btn) return;
    hideTip();
    $$(".tab").forEach((b) => {
        const on = b === btn;
        b.classList.toggle("active", on);
        b.setAttribute("aria-selected", on ? "true" : "false");
    });
    $$(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${tab}`));
    btn.scrollIntoView({ inline: "center", block: "nearest" });
    window.scrollTo({ top: 0 });

    if (tab === "home") renderHome();
    if (tab === "reports") loadReports();
    if (tab === "history") loadHistory();
    if (tab === "ideas" && !IDEAS_LOADED) { loadTradeIdeas(); loadTrackedIdeas(); }
    if (tab === "portfolio" && !PF_LOADED) initPortfolio();
    if (tab === "bots" && !BOT_LOADED) runBot();
    if (tab === "watchlist") loadWatchlist();
    if (tab === "paper") loadPaper();
}

function parseHash() {
    const raw = (location.hash || "").replace(/^#\/?/, "");   // "trade-ideas?ticker=X"
    const [path, qs] = raw.split("?");
    return { slug: path, params: new URLSearchParams(qs || "") };
}

// "#/slug", carrying the current market as ?m= so a shared/refreshed link restores it.
function buildHash(tab, extra = {}) {
    const params = new URLSearchParams();
    if (MARKET.key && MARKET.key !== "IN") params.set("m", MARKET.key);
    for (const [k, v] of Object.entries(extra)) if (v != null && v !== "") params.set(k, v);
    const qs = params.toString();
    return `#/${TAB_SLUGS[tab] || tab}${qs ? "?" + qs : ""}`;
}

// Navigate to a tab. push=true (default) adds a Back-history entry; false replaces in place.
function navigate(tab, extra = {}, push = true) {
    const hash = buildHash(tab, extra);
    if (hash === location.hash) { applyRoute(); return; }     // same URL → just (re)apply
    if (push) location.hash = hash;                            // fires hashchange → applyRoute
    else { history.replaceState(null, "", hash); applyRoute(); }
}

// Keep the URL's ?m= in sync with the current market without a Back-history entry.
function syncMarketToHash() {
    const active = ($(".tab.active") && $(".tab.active").dataset.tab) || DEFAULT_TAB;
    history.replaceState(null, "", buildHash(active));
}

// Apply whatever the current URL says: market, active tab, and any deep-link params.
function applyRoute() {
    const { slug, params } = parseHash();
    const tab = SLUG_TABS[slug] || DEFAULT_TAB;

    const m = params.get("m");
    if (m && MARKET.byKey[m] && m !== MARKET.key) setMarket(m);   // shared link in another market

    activateTab(tab);

    if (tab === "analyze") {                                       // deep link: #/analyze?ticker=X
        const t = params.get("ticker");
        if (t && ($("#a-ticker").value || "").toUpperCase() !== t.toUpperCase()) {
            $("#a-ticker").value = t;
            runAnalyze();
        }
    }
}

$$(".tab").forEach((btn) => {
    btn.setAttribute("role", "tab");
    btn.addEventListener("click", () => navigate(btn.dataset.tab));
});
$$(".tab-panel").forEach((p) => { p.setAttribute("role", "tabpanel"); p.setAttribute("tabindex", "0"); });
window.addEventListener("hashchange", applyRoute);

// Brand doubles as a Home link (keyboard-accessible).
{
    const home = $("#brand-home");
    if (home) {
        home.addEventListener("click", () => navigate(DEFAULT_TAB));
        home.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") { e.preventDefault(); navigate(DEFAULT_TAB); }
        });
    }
}

// ── health ───────────────────────────────────────────────────────

async function checkHealth() {
    try {
        const h = await api("/api/v1/health");
        $("#llm-mode").textContent = `LLM: ${h.llm_mode}`;
        $("#llm-mode").className = h.llm_mode === "rules-only" ? "pill pill-muted" : "pill pill-accent";
        $("#health-dot").classList.add("ok");
    } catch {
        $("#llm-mode").textContent = "API unreachable";
        $("#llm-mode").className = "pill pill-high";
    }
}

// ── filter form ──────────────────────────────────────────────────

const FILTER_FIELDS = [
    // [elementId, schema field, transform]
    ["#f-index", "index", (v) => v || null],
    ["#f-sector", "sector", (v) => v.trim() || null],
    ["#f-mcap-min", "market_cap_min_cr", Number],
    ["#f-mcap-max", "market_cap_max_cr", Number],
    ["#f-pe-max", "pe_ratio_max", Number],
    ["#f-pb-max", "pb_ratio_max", Number],
    ["#f-ev-max", "ev_ebitda_max", Number],
    ["#f-div-min", "dividend_yield_min", (v) => Number(v) / 100],
    ["#f-rev-min", "revenue_growth_min", (v) => Number(v) / 100],
    ["#f-profit-min", "profit_growth_min", (v) => Number(v) / 100],
    ["#f-roe-min", "roe_min", (v) => Number(v) / 100],
    ["#f-roce-min", "roce_min", (v) => Number(v) / 100],
    ["#f-de-max", "debt_to_equity_max", Number],
    ["#f-ic-min", "interest_coverage_min", Number],
    ["#f-piotroski-min", "piotroski_score_min", Number],
    ["#f-prom-min", "promoter_holding_min", (v) => Number(v) / 100],
    ["#f-pledge-max", "promoter_pledge_max", (v) => Number(v) / 100],
    ["#f-delivery-min", "delivery_pct_min", (v) => Number(v) / 100],
    ["#f-rsi-min", "rsi_min", Number],
    ["#f-rsi-max", "rsi_max", Number],
    ["#f-above-200", "price_above_200dma", (v) => (v === "" ? null : v === "true")],
    ["#f-macd", "macd_bullish", (v) => (v === "" ? null : v === "true")],
    ["#f-near-high", "near_52w_high_pct", Number],
    ["#f-vol-spike", "volume_spike_min", Number],
];

function collectFilters() {
    const filters = {};
    for (const [sel, field, transform] of FILTER_FIELDS) {
        const raw = $(sel).value;
        if (raw === "" || raw === null) continue;
        if (sel === "#f-index") { filters[field] = raw; continue; }
        const value = transform(raw);
        if (value === null || Number.isNaN(value)) continue;
        filters[field] = value;
    }
    return filters;
}

function applyFilters(filters) {
    const pctFields = new Set([
        "dividend_yield_min", "revenue_growth_min", "profit_growth_min", "roe_min",
        "roce_min", "promoter_holding_min", "promoter_pledge_max", "delivery_pct_min",
    ]);
    for (const [sel, field] of FILTER_FIELDS) {
        const v = filters[field];
        if (v === undefined || v === null) { $(sel).value = sel === "#f-index" ? (mkt().default_universe || "") : ""; continue; }
        if (pctFields.has(field)) $(sel).value = v * 100;
        else $(sel).value = v;
    }
}

// ── shared renderers ─────────────────────────────────────────────

function barsHtml(s) {
    const bar = (label, val, cls) => `
        <div class="bar-item">${label}: <b>${fmt(val)}</b>
            <div class="bar-track"><div class="bar-fill ${cls}" style="width:${val ?? 0}%"></div></div>
        </div>`;
    // Price-only markets have no fundamental score — show momentum instead.
    const lead = s.fundamental_score != null
        ? bar("Fundamental", s.fundamental_score, "bar-f")
        : bar("Momentum", s.momentum_score, "bar-f");
    return `<div class="bars">
        ${lead}
        ${bar("Technical", s.technical_score, "bar-t")}
        ${bar("Sentiment", s.sentiment_score, "bar-s")}
        ${bar("Risk", s.risk_score, "bar-r")}
    </div>`;
}

function chipsHtml(explanations) {
    if (!explanations?.length) return "";
    const chips = explanations.map((e) => {
        const cls = e.indeterminate ? "chip-na" : e.passed ? "chip-pass" : "chip-fail";
        const mark = e.indeterminate ? "?" : e.passed ? "✓" : "✗";
        const val = e.actual_value !== null && e.actual_value !== undefined ? ` ${e.actual_value}` : "";
        return `<span class="chip ${cls}" title="${esc(e.filter_name)}">${mark} ${esc(e.filter_name)}${esc(val)}</span>`;
    }).join("");
    return `<details class="filter-detail"><summary>Filter evidence (${explanations.length})</summary><div class="chips">${chips}</div></details>`;
}

function scoreCardHtml(s, { showBars = false, reportFile = null } = {}) {
    const composite = s.composite_score;
    return `
    <div class="result-card">
        <div class="result-head">
            <div>
                <div class="result-title">
                    <span class="result-ticker">${esc(s.ticker)}</span>
                    ${signalPill(s.technical_signal)} ${riskPill(s.risk_level)}
                </div>
                <div class="result-name">${esc(s.name)}</div>
                <div class="result-sector">${esc(s.sector || "")}${s.industry ? " · " + esc(s.industry) : ""}</div>
            </div>
            <div class="score-hero">
                <div class="score-num" style="color:${scoreColor(composite ?? s.filter_match_pct)}">
                    ${composite != null ? fmt(composite) : fmt(s.filter_match_pct) + "%"}
                </div>
                <div class="score-cap">${composite != null ? "composite" : `filter match ${s.filters_matched}/${s.filters_total}`}</div>
            </div>
        </div>
        <div class="metric-row">
            <span>Price <b>${priceFmt(s.price)}</b></span>
            ${marketHasFundamentals() ? `
            <span>P/E <b>${fmt(s.pe_ratio)}</b></span>
            <span>ROE <b>${s.roe != null ? fmt(s.roe) + "%" : "—"}</b></span>
            <span>ROCE <b>${s.roce != null ? fmt(s.roce) + "%" : "—"}</b></span>
            <span>D/E <b>${fmt(s.debt_to_equity, 2)}</b></span>` : ""}
            ${mcapUnit() ? `<span>MCap <b>${fmt(s.market_cap_cr, 0)} ${mcapUnit()}</b></span>` : ""}
            <span>RSI <b>${fmt(s.rsi)}</b></span>
            ${s.momentum_score != null ? `<span>Momentum <b>${fmt(s.momentum_score)}</b></span>` : ""}
            ${s.promoter_holding != null ? `<span>Promoter <b>${fmt(s.promoter_holding)}%</b></span>` : ""}
        </div>
        ${showBars && s.composite_score != null ? barsHtml(s) : ""}
        ${chipsHtml(s.filter_explanations)}
        ${reportFile ? `
        <div class="report-link-row">
            <button class="btn btn-accent btn-s" onclick="viewReport('${esc(reportFile)}')">
                📄 View Analysis Report
            </button>
        </div>` : ""}
    </div>`;
}

// ── Screener tab ─────────────────────────────────────────────────

async function runScreen() {
    const btn = $("#btn-screen");
    const status = $("#screen-status");
    const out = $("#screen-results");
    btn.disabled = true;
    btn.textContent = "Screening…";
    status.className = "status-line";
    status.textContent = "Fetching universe and applying filters — first run can take a minute on cold cache…";
    out.innerHTML = "";

    try {
        const body = {
            market: MARKET.key,
            query: $("#f-query").value.trim() || null,
            filters: collectFilters(),
            max_candidates: Number($("#f-max").value) || 10,
            include_report: false,
        };
        const data = await api("/api/v1/screen", { method: "POST", body: JSON.stringify(body) });
        status.textContent = `${data.count} candidate(s) · llm_mode=${data.llm_mode}`;
        if (!data.count) {
            out.innerHTML = `<div class="card muted">No stocks matched. Loosen filters or check data availability.</div>`;
            return;
        }
        out.innerHTML = data.candidates.map((s) => scoreCardHtml(s)).join("");
    } catch (e) {
        status.className = "status-line error";
        status.textContent = `Screen failed: ${e.message}`;
    } finally {
        btn.disabled = false;
        btn.textContent = "Run Screen →";
    }
}

$("#btn-screen").addEventListener("click", runScreen);

$("#btn-clear").addEventListener("click", () => {
    $$("#tab-screener .input").forEach((el) => {
        if (el.id === "f-index") el.value = mkt().default_universe || el.options[0]?.value || "";
        else if (el.id === "f-max") el.value = "10";
        else el.value = "";
    });
    $("#screen-results").innerHTML = "";
    $("#screen-status").textContent = "";
});

$("#btn-pipeline-from-screen").addEventListener("click", () => {
    $("#p-query").value = $("#f-query").value;
    $("#p-max").value = Math.min(Number($("#f-max").value) || 5, 10);
    $$(".tab").find((b) => b.dataset.tab === "pipeline").click();
    toast("Filters copied — start the pipeline run when ready.");
});

// ── presets ──────────────────────────────────────────────────────

async function loadPresets() {
    try {
        const screens = await api("/api/v1/screens");
        const sel = $("#preset-select");
        sel.innerHTML = `<option value="">Presets…</option>` +
            screens.map((s) => `<option value="${esc(s.name)}">${esc(s.name)}</option>`).join("");
    } catch { /* presets are optional */ }
}

$("#preset-select").addEventListener("change", async (e) => {
    if (!e.target.value) return;
    const screens = await api("/api/v1/screens");
    const found = screens.find((s) => s.name === e.target.value);
    if (found) { applyFilters(found.filters); toast(`Preset "${found.name}" applied`); }
});

$("#preset-save").addEventListener("click", async () => {
    const name = prompt("Preset name:");
    if (!name) return;
    try {
        await api("/api/v1/screens", {
            method: "POST",
            body: JSON.stringify({ name, filters: collectFilters(), description: null }),
        });
        await loadPresets();
        toast(`Preset "${name}" saved`);
    } catch (e) { toast(`Save failed: ${e.message}`, true); }
});

// ── Pipeline tab ─────────────────────────────────────────────────

let activePoll = null;

function setProgress(pct, stage) {
    $("#pipeline-progress").classList.remove("hidden");
    $("#progress-fill").style.width = `${Math.min(100, pct)}%`;
    $("#progress-pct").textContent = `${Math.round(pct)}%`;
    if (stage) $("#progress-stage").textContent = stage;
}

async function watchRun(runId) {
    // Prefer WebSocket; fall back to polling
    let done = false;
    const finish = async () => {
        if (done) return;
        done = true;
        clearInterval(activePoll);
        const run = await api(`/api/v1/pipeline/${runId}`);
        renderPipelineRun(run);
        $("#btn-run-pipeline").disabled = false;
        $("#btn-run-pipeline").textContent = "Start Pipeline Run →";
    };

    try {
        const proto = location.protocol === "https:" ? "wss://" : "ws://";
        const ws = new WebSocket(`${proto}${location.host}/api/v1/pipeline/${runId}/ws`);
        ws.onmessage = (ev) => {
            const msg = JSON.parse(ev.data);
            setProgress(msg.progress_pct ?? 0, msg.stage);
            if (msg.status === "completed" || msg.status === "failed") { ws.close(); finish(); }
        };
        ws.onerror = () => { ws.close(); startPolling(); };
    } catch { startPolling(); }

    function startPolling() {
        clearInterval(activePoll);
        activePoll = setInterval(async () => {
            try {
                const run = await api(`/api/v1/pipeline/${runId}`);
                setProgress(run.progress_pct, run.current_stage);
                if (run.status === "completed" || run.status === "failed") finish();
            } catch { /* transient */ }
        }, 2500);
    }
}

function renderPipelineRun(run) {
    const status = $("#pipeline-status");
    const out = $("#pipeline-results");
    if (run.status === "failed") {
        status.className = "status-line error";
        status.textContent = `Run failed: ${run.error}`;
        return;
    }
    status.className = "status-line";

    // Map report files to their tickers: "{TICKER}_{run_id}.md"
    const reportByTicker = {};
    (run.report_files || []).forEach((f) => { reportByTicker[f.split("_")[0]] = f; });

    status.innerHTML = `Run ${esc(run.id.slice(0, 8))} · ${run.candidates_found} analyzed · llm=${esc(run.llm_mode)}`;

    let html = "";
    if (run.report_files?.length) {
        const links = run.report_files.map((f) =>
            `<button class="btn btn-accent btn-s" onclick="viewReport('${esc(f)}')">📄 ${esc(f.split("_")[0])} report</button>`
        ).join("");
        html += `
        <div class="card report-banner">
            <span>✅ Pipeline complete — analysis report${run.report_files.length > 1 ? "s" : ""} ready:</span>
            <span class="row gap-s">${links}</span>
        </div>`;
        toast(`Pipeline complete — report ready for ${run.report_files[0].split("_")[0]}`);
    } else {
        toast("Pipeline complete");
    }

    if (!run.results?.length) {
        html += `<div class="card muted">No candidates matched the filters.</div>`;
    } else {
        html += run.results.map((s) =>
            scoreCardHtml(s, { showBars: true, reportFile: reportByTicker[s.ticker] })
        ).join("");
    }
    out.innerHTML = html;
}

$("#btn-run-pipeline").addEventListener("click", async () => {
    const btn = $("#btn-run-pipeline");
    btn.disabled = true;
    btn.textContent = "Starting…";
    $("#pipeline-results").innerHTML = "";
    $("#pipeline-status").textContent = "";
    setProgress(2, "Starting run…");
    try {
        const body = {
            market: MARKET.key,
            query: $("#p-query").value.trim() || null,
            filters: collectFilters(),
            analysis_depth: $("#p-depth").value,
            max_candidates: Number($("#p-max").value) || 5,
            include_report: $("#p-report").checked,
        };
        const { run_id } = await api("/api/v1/pipeline/run", { method: "POST", body: JSON.stringify(body) });
        $("#progress-run-id").textContent = `run_id: ${run_id}`;
        btn.textContent = "Running…";
        await watchRun(run_id);
    } catch (e) {
        $("#pipeline-status").className = "status-line error";
        $("#pipeline-status").textContent = `Failed to start: ${e.message}`;
        btn.disabled = false;
        btn.textContent = "Start Pipeline Run →";
    }
});

// ── Analyze tab ──────────────────────────────────────────────────

let lastAnalyzedTicker = null;

async function runAnalyze() {
    const ticker = $("#a-ticker").value.trim().toUpperCase();
    if (!ticker) return toast("Enter a ticker first", true);
    const btn = $("#btn-analyze");
    btn.disabled = true;
    btn.textContent = "Analyzing…";
    $("#analyze-status").className = "status-line";
    $("#analyze-status").textContent = `Deep-diving ${ticker} across 4 agents…`;
    $("#analyze-results").innerHTML = "";
    $("#btn-gen-report").disabled = true;

    try {
        const a = await api(withMarket(`/api/v1/stocks/${encodeURIComponent(ticker)}/analyze?depth=${$("#a-depth").value}`));
        if (a.error && a.composite_score == null) throw new Error(a.error);
        lastAnalyzedTicker = ticker;
        $("#analyze-status").textContent = "";
        $("#analyze-results").innerHTML = analyzeHtml(a);
        $("#btn-gen-report").disabled = false;
    } catch (e) {
        $("#analyze-status").className = "status-line error";
        $("#analyze-status").textContent = `Analysis failed: ${e.message}`;
    } finally {
        btn.disabled = false;
        btn.textContent = "Analyze →";
    }
}

function gaugeHtml(score) {
    const color = scoreColor(score);
    const deg = (score / 100) * 360;
    return `
    <div class="gauge" style="background: conic-gradient(${color} ${deg}deg, var(--bg-3) ${deg}deg); border: none;">
        <div style="background: var(--bg-2); width: 88px; height: 88px; border-radius: 50%; display:flex; flex-direction:column; align-items:center; justify-content:center;">
            <span class="score-num" style="color:${color}">${fmt(score)}</span>
            <span class="score-cap">composite</span>
        </div>
    </div>`;
}

function analyzeHtml(a) {
    const ev = a.evidence || {};
    const f = ev.fundamental || {}, t = ev.technical || {}, s = ev.sentiment || {}, r = ev.risk || {};

    const list = (items, neg = false) =>
        (items || []).length
            ? `<ul>${items.map((i) => `<li class="${neg ? "neg" : ""}">${esc(i)}</li>`).join("")}</ul>`
            : `<p>None identified</p>`;

    return `
    <div class="card">
        <div class="gauge-wrap">
            ${gaugeHtml(a.composite_score ?? 0)}
            <div style="flex:1; min-width: 240px;">
                <div class="result-title">
                    <span class="result-ticker">${esc(a.ticker)}</span>
                    ${signalPill(a.technical_signal)} ${riskPill(a.risk_level)}
                    <span class="pill pill-muted">${esc(a.llm_mode || "")}</span>
                </div>
                <div class="result-name">${esc(a.name)}</div>
                ${barsHtml(a)}
            </div>
        </div>
        <div class="evi-grid">
            <div class="evi-card">
                <h3>Fundamental <span style="color:${scoreColor(f.score)}">${fmt(f.score)}</span></h3>
                <p>Valuation: <b>${esc(f.valuation_status || "—")}</b> · ROCE: <b>${f.roce != null ? fmt(f.roce * 100) + "%" : "—"}</b> · Piotroski: <b>${f.piotroski_score ?? "—"}/9</b> · Coverage: <b>${f.interest_coverage != null ? fmt(f.interest_coverage) + "x" : "—"}</b></p>
                ${list(f.strengths)}
                ${list(f.concerns, true)}
                ${f.moat_assessment ? `<p style="margin-top:8px"><b>Moat:</b> ${esc(f.moat_assessment)}</p>` : ""}
            </div>
            <div class="evi-card">
                <h3>Technical <span style="color:${scoreColor(t.score)}">${fmt(t.score)}</span></h3>
                <p>RSI: <b>${fmt(t.rsi)}</b> · 50-DMA: <b>${fmt(t.sma_50, 0)}</b> · 200-DMA: <b>${fmt(t.sma_200, 0)}</b> · MACD: <b>${t.macd_bullish ? "bullish" : "bearish"}</b></p>
                <p>52w: <b>${fmt(t.week_52_low, 0)}</b> – <b>${fmt(t.week_52_high, 0)}</b> (${fmt(t.pct_from_52w_high)}% from high)</p>
                ${list(t.triggers)}
            </div>
            <div class="evi-card">
                <h3>Sentiment <span style="color:${scoreColor(s.score)}">${fmt(s.score)}</span></h3>
                <p>Consensus: <b>${esc(s.consensus || "—")}</b> · method: ${esc(s.method || "—")}</p>
                <p>${esc(s.summary || "")}</p>
            </div>
            <div class="evi-card">
                <h3>Risk <span style="color:${scoreColor(100 - (r.score ?? 50))}">${fmt(r.score)}</span></h3>
                <p>Level: <b>${esc(r.level || "—")}</b> · Volatility: <b>${r.annual_volatility_pct != null ? fmt(r.annual_volatility_pct) + "%" : "—"}</b> · Beta: <b>${r.beta ?? "—"}</b> · D/E: <b>${r.debt_to_equity_ratio ?? "—"}x</b></p>
                ${list(r.risk_flags, true)}
            </div>
        </div>
    </div>`;
}

$("#btn-analyze").addEventListener("click", runAnalyze);

// Ticker autocomplete — typeahead over the NIFTY 500 universe (pick, don't free-type)
(function setupTickerAutocomplete() {
    const input = $("#a-ticker");
    const box = $("#a-suggest");
    let items = [], active = -1, timer = null;

    const hide = () => { box.classList.add("hidden"); box.innerHTML = ""; active = -1; };
    const render = () => {
        if (!items.length) { hide(); return; }
        box.innerHTML = items.map((s, i) =>
            `<div class="suggest-item${i === active ? " active" : ""}" data-sym="${esc(s.symbol)}">` +
            `<span class="mono b">${esc(s.symbol)}</span>${s.name ? `<span class="muted small">${esc(s.name)}</span>` : ""}</div>`
        ).join("");
        box.classList.remove("hidden");
    };
    const choose = (sym) => { input.value = sym; hide(); input.focus(); };
    const fetchSuggest = async (q) => {
        try { items = await api(withMarket(`/api/v1/stocks/suggest?q=${encodeURIComponent(q)}&limit=8`)); active = -1; render(); }
        catch { hide(); }
    };

    input.addEventListener("input", () => {
        const q = input.value.trim();
        clearTimeout(timer);
        if (!q) { hide(); return; }
        timer = setTimeout(() => fetchSuggest(q), 150);
    });
    input.addEventListener("keydown", (e) => {
        const open = !box.classList.contains("hidden");
        if (open && e.key === "ArrowDown") { e.preventDefault(); active = Math.min(active + 1, items.length - 1); render(); }
        else if (open && e.key === "ArrowUp") { e.preventDefault(); active = Math.max(active - 1, 0); render(); }
        else if (e.key === "Enter") {
            if (open && active >= 0) { e.preventDefault(); choose(items[active].symbol); }
            else runAnalyze();
        } else if (e.key === "Escape") { hide(); }
    });
    box.addEventListener("mousedown", (e) => {          // mousedown beats input blur
        const it = e.target.closest(".suggest-item");
        if (it) { e.preventDefault(); choose(it.dataset.sym); }
    });
    input.addEventListener("blur", () => setTimeout(hide, 150));
})();

$("#btn-gen-report").addEventListener("click", async () => {
    if (!lastAnalyzedTicker) return;
    const btn = $("#btn-gen-report");
    btn.disabled = true;
    btn.textContent = "Generating…";
    try {
        const r = await api(withMarket(`/api/v1/stocks/${encodeURIComponent(lastAnalyzedTicker)}/report`), { method: "POST" });
        toast(`Report saved: ${r.filename}`);
        viewReport(r.filename);
    } catch (e) { toast(`Report failed: ${e.message}`, true); }
    btn.disabled = false;
    btn.textContent = "Report ⤓";
});

// ── Reports tab ──────────────────────────────────────────────────

async function loadReports() {
    const list = $("#reports-list");
    try {
        const files = await api("/api/v1/reports");
        if (!files.length) {
            list.innerHTML = `<p class="muted">No reports yet — run a pipeline with reports enabled, or use Analyze → Report.</p>`;
            return;
        }
        list.innerHTML = files.map((f) => {
            const date = new Date(f.modified * 1000).toLocaleString();
            return `<div class="file-item" data-file="${esc(f.filename)}" onclick="viewReport('${esc(f.filename)}')">
                <span>${esc(f.filename)}</span><span class="file-date">${esc(date)}</span></div>`;
        }).join("");
    } catch (e) {
        list.innerHTML = `<p class="muted">Could not load reports: ${esc(e.message)}</p>`;
    }
}

async function viewReport(filename) {
    navigate("reports");

    const viewer = $("#report-viewer");
    viewer.innerHTML = `<p class="muted">Loading ${esc(filename)}…</p>`;
    try {
        const res = await fetch(`/api/v1/reports/${encodeURIComponent(filename)}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const md = await res.text();
        viewer.innerHTML = window.marked ? marked.parse(md) : `<pre>${esc(md)}</pre>`;
        $$(".file-item").forEach((el) => el.classList.toggle("active", el.dataset.file === filename));
    } catch (e) {
        viewer.innerHTML = `<p class="muted">Could not load report: ${esc(e.message)}</p>`;
    }
}
window.viewReport = viewReport;

$("#btn-refresh-reports").addEventListener("click", loadReports);

// ── History tab ──────────────────────────────────────────────────

async function loadHistory() {
    const list = $("#history-list");
    try {
        const runs = await api("/api/v1/pipeline/runs?limit=30");
        if (!runs.length) {
            list.innerHTML = `<p class="muted">No pipeline runs yet.</p>`;
            return;
        }
        const rows = runs.map((r) => {
            const date = new Date(r.created_at).toLocaleString();
            const statusCls = r.status === "completed" ? "pill-low" : r.status === "failed" ? "pill-high" : "pill-medium";
            const reports = (r.report_files || []).length;
            return `
            <div class="hist-row" onclick="viewRun('${esc(r.id)}')">
                <span class="muted small">${esc(date)}</span>
                <span>${esc(r.query || "(structured filters)")}</span>
                <span><span class="pill ${statusCls}">${esc(r.status)}</span></span>
                <span class="mono">${r.candidates_found} cands</span>
                <span class="mono">${reports ? reports + " 📄" : "—"}</span>
            </div>`;
        }).join("");
        list.innerHTML = `
            <div class="hist-row header"><span>When</span><span>Query</span><span>Status</span><span>Results</span><span>Reports</span></div>
            ${rows}`;
    } catch (e) {
        list.innerHTML = `<p class="muted">Could not load history: ${esc(e.message)}</p>`;
    }
}

async function viewRun(runId) {
    const detail = $("#history-detail");
    detail.innerHTML = `<p class="muted">Loading run…</p>`;
    try {
        const run = await api(`/api/v1/pipeline/${runId}`);
        const reportByTicker = {};
        (run.report_files || []).forEach((f) => { reportByTicker[f.split("_")[0]] = f; });
        const reports = (run.report_files || []).map((f) =>
            `<button class="btn btn-accent btn-s" onclick="viewReport('${esc(f)}')">📄 ${esc(f.split("_")[0])} report</button>`
        ).join(" ");
        detail.innerHTML = `
            <div class="card" style="margin-bottom:12px">
                <div class="result-title">
                    <span class="pill pill-accent">${esc(run.status)}</span>
                    <span class="muted small mono">${esc(run.id)}</span>
                </div>
                <div class="muted small" style="margin-top:6px">
                    Filters: <code>${esc(JSON.stringify(run.filters))}</code>
                </div>
                ${run.error ? `<div class="status-line error" style="margin-top:6px">${esc(run.error)}</div>` : ""}
                ${reports ? `<div class="row gap-s" style="margin-top:10px">${reports}</div>` : ""}
            </div>
            ${(run.results || []).map((s) =>
                scoreCardHtml(s, { showBars: true, reportFile: reportByTicker[s.ticker] })
            ).join("")}`;
    } catch (e) {
        detail.innerHTML = `<p class="muted">Could not load run: ${esc(e.message)}</p>`;
    }
}
window.viewRun = viewRun;

$("#btn-refresh-history").addEventListener("click", loadHistory);

// ── Strategies tab ────────────────────────────────────────────────

// Human-readable labels for filter fields (values in % where noted)
const FILTER_LABELS = {
    index: (v) => `Universe: ${v}`,
    sector: (v) => `Sector: ${v}`,
    industry: (v) => `Industry: ${v}`,
    market_cap_min_cr: (v) => `MCap ≥ ${fmt(v, 0)} ${mcapUnit()}`,
    market_cap_max_cr: (v) => `MCap ≤ ${fmt(v, 0)} ${mcapUnit()}`,
    pe_ratio_max: (v) => `P/E ≤ ${v}`,
    pe_ratio_min: (v) => `P/E ≥ ${v}`,
    pb_ratio_max: (v) => `P/B ≤ ${v}`,
    ev_ebitda_max: (v) => `EV/EBITDA ≤ ${v}`,
    peg_ratio_max: (v) => `PEG ≤ ${v}`,
    dividend_yield_min: (v) => `Div yield ≥ ${(v * 100).toFixed(1)}%`,
    revenue_growth_min: (v) => `Rev growth ≥ ${(v * 100).toFixed(0)}%`,
    profit_growth_min: (v) => `Profit growth ≥ ${(v * 100).toFixed(0)}%`,
    roe_min: (v) => `ROE ≥ ${(v * 100).toFixed(0)}%`,
    roce_min: (v) => `ROCE ≥ ${(v * 100).toFixed(0)}%`,
    opm_min: (v) => `OPM ≥ ${(v * 100).toFixed(0)}%`,
    npm_min: (v) => `NPM ≥ ${(v * 100).toFixed(0)}%`,
    debt_to_equity_max: (v) => `D/E ≤ ${v}x`,
    interest_coverage_min: (v) => `Coverage ≥ ${v}x`,
    piotroski_score_min: (v) => `Piotroski ≥ ${v}`,
    fcf_yield_min: (v) => `FCF yield ≥ ${(v * 100).toFixed(0)}%`,
    promoter_holding_min: (v) => `Promoter ≥ ${(v * 100).toFixed(0)}%`,
    promoter_pledge_max: (v) => `Pledge ≤ ${(v * 100).toFixed(1)}%`,
    delivery_pct_min: (v) => `Delivery ≥ ${(v * 100).toFixed(0)}%`,
    rsi_min: (v) => `RSI ≥ ${v}`,
    rsi_max: (v) => `RSI ≤ ${v}`,
    price_above_200dma: (v) => (v ? "Above 200-DMA" : "Below 200-DMA"),
    price_above_50dma: (v) => (v ? "Above 50-DMA" : "Below 50-DMA"),
    macd_bullish: (v) => (v ? "MACD bullish" : "MACD bearish"),
    near_52w_high_pct: (v) => `≤${v}% from 52w high`,
    near_52w_low_pct: (v) => `≤${v}% from 52w low`,
    volume_spike_min: (v) => `Volume ≥ ${v}× avg`,
    eps_growth_3y_min: (v) => `EPS 3y ≥ ${(v * 100).toFixed(0)}%`,
    sales_growth_3y_min: (v) => `Sales 3y ≥ ${(v * 100).toFixed(0)}%`,
};

const CATEGORY_PILLS = {
    Quality: "pill-accent", Value: "pill-buy", Growth: "pill-buy",
    Momentum: "pill-medium", Contrarian: "pill-hold", "India-specific": "pill-accent",
    Income: "pill-buy", Defensive: "pill-low",
};

let STRATEGIES = [];

async function loadStrategies() {
    const grid = $("#strategies-grid");
    try {
        const { strategies } = await api(withMarket("/api/v1/strategies"));
        STRATEGIES = strategies;
        grid.innerHTML = strategies.map(strategyCardHtml).join("");
    } catch (e) {
        grid.innerHTML = `<p class="muted">Could not load strategies: ${esc(e.message)}</p>`;
    }
}

function strategyCardHtml(s) {
    const chips = Object.entries(s.filters)
        .filter(([k]) => k !== "index")
        .map(([k, v]) => `<span class="chip chip-na">${esc(FILTER_LABELS[k] ? FILTER_LABELS[k](v) : `${k}: ${v}`)}</span>`)
        .join("");
    const pillCls = CATEGORY_PILLS[s.category] || "pill-muted";
    return `
    <div class="strategy-card" id="strategy-${esc(s.id)}">
        <div class="card-title-row">
            <h3>${esc(s.name)}</h3>
            <span class="pill ${pillCls}">${esc(s.category)}</span>
        </div>
        <div class="strategy-desc">${esc(s.description)}</div>
        <div class="strategy-rationale">${esc(s.rationale)}</div>
        <div class="chips">
            <span class="chip chip-na">${esc(s.filters.index || mkt().default_universe || "—")}</span>${chips}
        </div>
        <div class="card-actions">
            <button class="btn btn-primary btn-s flex-1" onclick="runStrategy('${esc(s.id)}')">
                Run Pipeline →
            </button>
            <button class="btn btn-ghost btn-s" onclick="editStrategy('${esc(s.id)}')" title="Load filters into the Screener to tweak">
                Customize
            </button>
        </div>
    </div>`;
}

async function runStrategy(id) {
    const s = STRATEGIES.find((x) => x.id === id);
    if (!s) return;
    const card = $(`#strategy-${id}`);
    const btn = card.querySelector(".btn-primary");
    if (btn.disabled) return;
    btn.disabled = true;
    btn.textContent = "Starting…";
    card.classList.add("running");

    try {
        const body = {
            market: MARKET.key,
            query: null,
            filters: s.filters,
            analysis_depth: s.defaults.analysis_depth || "standard",
            max_candidates: s.defaults.max_candidates || 10,
            include_report: true,
        };
        const { run_id } = await api("/api/v1/pipeline/run", { method: "POST", body: JSON.stringify(body) });
        toast(`"${s.name}" pipeline started — watch progress in the Pipeline tab`);

        // Jump to the Pipeline tab and attach live progress there
        $$(".tab").find((b) => b.dataset.tab === "pipeline").click();
        $("#pipeline-status").innerHTML = `<span class="pill pill-accent">strategy</span> ${esc(s.name)}`;
        $("#pipeline-results").innerHTML = "";
        setProgress(2, "Starting run…");
        $("#progress-run-id").textContent = `run_id: ${run_id}`;
        $("#btn-run-pipeline").disabled = true;
        $("#btn-run-pipeline").textContent = "Running…";
        await watchRun(run_id);
    } catch (e) {
        toast(`Failed to start "${s.name}": ${e.message}`, true);
    } finally {
        btn.disabled = false;
        btn.textContent = "Run Pipeline →";
        card.classList.remove("running");
    }
}
window.runStrategy = runStrategy;

function editStrategy(id) {
    const s = STRATEGIES.find((x) => x.id === id);
    if (!s) return;
    applyFilters(s.filters);
    $("#f-max").value = s.defaults.max_candidates || 10;
    $$(".tab").find((b) => b.dataset.tab === "screener").click();
    toast(`"${s.name}" loaded — adjust filters and run`);
}
window.editStrategy = editStrategy;

// ── Strategy Lab tab ─────────────────────────────────────────────

let LAB_DATA = null;       // leaderboard payload
let LAB_CHART = null;      // active Chart.js instance
let labStatusPoll = null;

// [key, label, tooltip] — tooltip drives the ? help badge on each column header
const LAB_COLUMNS = [
    ["name", "Strategy", ""],
    ["category", "Category", ""],
    ["trades", "Trades", "Number of completed round-trip trades this strategy took over the backtest window."],
    ["win_rate", "Win %", "Share of closed trades that ended in profit (after costs)."],
    ["profit_factor", "PF", "Profit factor — gross profit ÷ gross loss. Above 1 means winners outweigh losers; ∞ = no losing trades."],
    ["expectancy_pct", "Exp %", "Expectancy — the average profit or loss per trade after 25 bps round-trip costs."],
    ["avg_holding_days", "Hold d", "Average number of calendar days a position was held per trade."],
    ["cagr_pct", "CAGR %", "Compound annual growth rate of the strategy's equity curve over the backtest."],
    ["max_drawdown_pct", "Max DD %", "Maximum drawdown — the largest peak-to-trough equity decline. Closer to 0 is better."],
    ["sharpe", "Sharpe", "Risk-adjusted return: annualized return ÷ annualized volatility. Higher is better (>1 is good)."],
    ["calmar", "Calmar", "Calmar ratio — CAGR ÷ absolute max drawdown. Return earned per unit of worst-case loss."],
    ["alpha_vs_benchmark_pct", "α vs bench", "Alpha — annualized return above (positive) or below the benchmark over the same window."],
    ["signals_buy", "Signals", "Live signals as of the last run — B: fresh buys, S: recent sells, H: currently held."],
];

async function loadLabLeaderboard(sortBy) {
    const box = $("#lab-leaderboard");
    try {
        const sort = sortBy ?? $("#lab-sort").value;
        LAB_DATA = await api(withMarket(`/api/v1/backtest/leaderboard?sort_by=${sort}`));
        renderLabLeaderboard();
        if (LAB_DATA.run_at) {
            const dt = new Date(LAB_DATA.run_at).toLocaleString();
            const st = LAB_DATA.data_stats || {};
            const nStrat = (LAB_DATA.strategies || []).length;
            $("#lab-run-info").textContent =
                `${nStrat} strategies · Last run: ${dt} · ${LAB_DATA.index} vs ${LAB_DATA.benchmark_label || "benchmark"}, ${LAB_DATA.years}y · ` +
                `${st.loaded ?? "?"}/${st.total ?? "?"} assets loaded` +
                (st.skipped_insufficient ? ` · ${st.skipped_insufficient} skipped (short history)` : "");
        }
        $("#lab-disclaimer").textContent = LAB_DATA.disclaimer || "";
    } catch (e) {
        box.innerHTML = `<p class="muted">Could not load leaderboard: ${esc(e.message)}</p>`;
    }
}

function renderLabLeaderboard() {
    const box = $("#lab-leaderboard");
    const rows = LAB_DATA.strategies || [];
    if (!rows.length) {
        box.innerHTML = `<p class="muted">No backtest results yet — click <b>Run Backtests</b> (first run downloads 10 years of data for ~500 stocks, ~5–10 min).</p>`;
        return;
    }
    const head = LAB_COLUMNS.map(([k, label, tip]) => {
        const help = tip
            ? ` <span class="help" data-tip="${esc(tip)}" onclick="event.stopPropagation()">?</span>`
            : "";
        return `<th data-col="${k}" class="lab-sortable">${label}${help}</th>`;
    }).join("");
    const body = rows.map((r) => {
        const pf = r.profit_factor === "inf" ? "∞" : fmt(r.profit_factor, 2);
        const ddCls = r.max_drawdown_pct <= -30 ? "neg" : "";
        const alpha = r.alpha_vs_benchmark_pct;
        return `<tr class="lab-row" data-id="${esc(r.id)}">
            <td><b>${esc(r.name)}</b></td>
            <td><span class="pill pill-muted">${esc(r.category)}</span></td>
            <td class="mono">${fmt(r.trades, 0)}</td>
            <td class="mono" style="color:${scoreColor(r.win_rate)}">${fmt(r.win_rate)}</td>
            <td class="mono">${pf}</td>
            <td class="mono ${r.expectancy_pct >= 0 ? "pos" : "neg"}">${fmt(r.expectancy_pct)}</td>
            <td class="mono">${fmt(r.avg_holding_days)}</td>
            <td class="mono ${r.cagr_pct >= 0 ? "pos" : "neg"}">${fmt(r.cagr_pct)}</td>
            <td class="mono ${ddCls}">${fmt(r.max_drawdown_pct)}</td>
            <td class="mono" style="color:${scoreColor((r.sharpe + 0.5) * 40)}">${fmt(r.sharpe, 2)}</td>
            <td class="mono">${fmt(r.calmar, 2)}</td>
            <td class="mono ${alpha != null && alpha >= 0 ? "pos" : "neg"}">${alpha != null ? fmt(alpha) : "—"}</td>
            <td class="mono small">B:${r.signals_buy} S:${r.signals_sell} H:${r.signals_holding}</td>
        </tr>`;
    }).join("");
    box.innerHTML = `
        <div class="table-scroll"><table class="lab-table">
            <thead><tr>${head}</tr></thead><tbody>${body}</tbody>
        </table></div>`;

    box.querySelectorAll(".lab-row").forEach((tr) =>
        tr.addEventListener("click", () => loadLabStrategy(tr.dataset.id)));
    box.querySelectorAll(".lab-sortable").forEach((th) =>
        th.addEventListener("click", (e) => {
            e.stopPropagation();
            const colMap = { cagr_pct: "cagr", max_drawdown_pct: "max_drawdown", profit_factor: "profit_factor",
                expectancy_pct: "expectancy", win_rate: "win_rate", sharpe: "sharpe", calmar: "calmar",
                alpha_vs_benchmark_pct: "alpha" };
            const sort = colMap[th.dataset.col];
            if (sort) { $("#lab-sort").value = sort; loadLabLeaderboard(sort); }
        }));
}

$("#lab-sort").addEventListener("change", () => loadLabLeaderboard());
$("#btn-lab-discover").addEventListener("click", toggleDiscover);

$("#btn-run-backtest").addEventListener("click", async () => {
    const btn = $("#btn-run-backtest");
    btn.disabled = true;
    try {
        await api("/api/v1/backtest/run", { method: "POST", body: JSON.stringify({ market: MARKET.key, index: null, years: 10 }) });
        toast(`Backtest started — 10y × ${mkt().default_universe || "universe"} × all strategies`);
        $("#lab-progress").classList.remove("hidden");
        labStatusPoll = setInterval(pollLabStatus, 3000);
        pollLabStatus();
    } catch (e) {
        toast(e.message, true);
        btn.disabled = false;
    }
});

async function pollLabStatus() {
    try {
        const s = await api(withMarket("/api/v1/backtest/status"));
        const job = s.job;
        if (job.running) {
            $("#lab-progress-stage").textContent = `${job.stage}${job.detail ? " · " + job.detail : ""}`;
            $("#lab-progress-fill").style.width = `${job.progress_pct}%`;
            $("#lab-progress-pct").textContent = `${Math.round(job.progress_pct)}%`;
        } else {
            clearInterval(labStatusPoll);
            $("#lab-progress-fill").style.width = "100%";
            $("#lab-progress-pct").textContent = "100%";
            $("#lab-progress-stage").textContent = job.error ? `Failed: ${job.error}` : "Completed";
            $("#btn-run-backtest").disabled = false;
            if (!job.error) {
                toast(`Backtest complete in ${s.elapsed_seconds ?? "?"}s`);
                await loadLabLeaderboard();
            } else {
                toast(`Backtest failed: ${job.error}`, true);
            }
        }
    } catch { /* transient */ }
}

// ── Strategy discovery (generate → validate OOS → keep survivors, staged) ──
let DISCOVER_DATA = null, DISCOVER_POLL = null;

async function toggleDiscover() {
    const el = $("#lab-discover");
    if (el.style.display === "none") {
        el.style.display = "";
        await loadDiscover();
        el.scrollIntoView({ behavior: "smooth", block: "start" });
    } else {
        el.style.display = "none";
    }
}

async function loadDiscover() {
    $("#lab-discover").innerHTML = `<p class="muted">Loading discovered strategies…</p>`;
    try { DISCOVER_DATA = await api(withMarket("/api/v1/backtest/discovered")); }
    catch (e) { DISCOVER_DATA = { survivors: [], note: e.message }; }
    renderDiscover();
}

function discoverControls() {
    const running = !!DISCOVER_POLL;
    return `<div class="row gap-s" style="align-items:center; flex-wrap:wrap;">
        <select id="disc-n" class="input input-slim" title="How many candidate strategies to sample">
            <option value="100">100 candidates</option><option value="200" selected>200 candidates</option><option value="500">500 candidates</option></select>
        <button id="btn-run-discover" class="btn btn-primary" ${running ? "disabled" : ""}>${running ? "Discovering…" : "Discover →"}</button>
        <span id="disc-run-status" class="muted small"></span></div>`;
}

function renderDiscover() {
    const d = DISCOVER_DATA || {}, surv = d.survivors || [], cov = d.coverage || {};
    const sub = [
        d.as_of ? new Date(d.as_of).toLocaleString() : null,
        cov.split_date ? `split ${cov.split_date} · IS ${cov.is_years}y / OOS ${cov.oos_years}y` : null,
        (d.tried != null) ? `tried ${d.tried} · validated ${d.validated_oos} · kept ${d.kept}` : null,
    ].filter(Boolean).join(" · ");
    const head = `<div class="row gap-s" style="justify-content:space-between; align-items:center; flex-wrap:wrap;">
        <div><b>Discovered strategies</b> <span class="muted small">${sub ? "· " + esc(sub) : ""}</span></div>
        ${discoverControls()}</div>`;
    if (!surv.length) {
        $("#lab-discover").innerHTML = head + `<p class="muted" style="margin-top:12px;">${esc(d.note || "No discovery run yet.")} Discovery samples candidate strategies from a rule grammar, ranks them in-sample, then keeps only those whose edge holds up <b>out-of-sample</b> (with a degradation guard) — quality over quantity. Survivors stay here until you <b>Promote</b> them into the live library.</p>`;
        return wireDiscoverControls();
    }
    const cell = (m, key, pos = false) => {
        if (!m || m[key] == null) return `<td class="mono">—</td>`;
        let v = m[key];
        if (key === "profit_factor") v = (v === "inf") ? "∞" : fmt(v, 2);
        else if (key === "trades") v = fmt(v, 0);
        else v = fmt(v, 2);
        return `<td class="mono ${pos ? (m[key] >= 0 ? "pos" : "neg") : ""}">${v}</td>`;
    };
    const rows = surv.map((s) => {
        const is = s.is_metrics || {}, oos = s.oos_metrics || {};
        const badge = s.promoted ? `<span class="pill pill-buy" title="Joins the live library on the next backtest run">promoted</span>` : "";
        const act = s.promoted
            ? `<button class="btn btn-s disc-unpromote" data-id="${esc(s.id)}">Unpromote</button>`
            : `<button class="btn btn-s btn-primary disc-promote" data-id="${esc(s.id)}">Promote</button>`;
        return `<tr>
            <td>
                <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;"><b>${esc(s.name)}</b>${badge}</div>
                <div class="muted small">${esc(s.description || "")}</div></td>
            <td><span class="pill pill-muted">${esc(s.category)}</span></td>
            ${cell(is, "sharpe")}${cell(is, "expectancy_pct", true)}${cell(is, "profit_factor")}${cell(is, "trades")}
            ${cell(oos, "sharpe")}${cell(oos, "expectancy_pct", true)}${cell(oos, "profit_factor")}${cell(oos, "trades")}
            <td style="white-space:nowrap">${act} <button class="btn btn-s disc-delete" data-id="${esc(s.id)}" title="Remove from catalog">✕</button></td>
        </tr>`;
    }).join("");
    $("#lab-discover").innerHTML = head + `<div class="table-scroll" style="margin-top:12px;"><table class="lab-table">
        <thead>
          <tr><th rowspan="2">Strategy</th><th rowspan="2">Category</th>
              <th colspan="4" style="text-align:center; border-bottom:1px solid var(--border)">In-sample</th>
              <th colspan="4" style="text-align:center; border-bottom:1px solid var(--border)">Out-of-sample</th>
              <th rowspan="2"></th></tr>
          <tr><th>Sharpe</th><th>Exp%</th><th>PF</th><th>Tr</th><th>Sharpe</th><th>Exp%</th><th>PF</th><th>Tr</th></tr>
        </thead>
        <tbody>${rows}</tbody></table></div>
        <p class="muted small" style="margin-top:8px;">${esc(d.note || "")} <b>Promote</b> a survivor to include it in the live leaderboard &amp; Trade Ideas on the next <b>Run Backtests</b>. ${esc(d.disclaimer || "")}</p>`;
    wireDiscoverControls();
}

function wireDiscoverControls() {
    const btn = $("#btn-run-discover"); if (btn) btn.addEventListener("click", runDiscover);
    const box = $("#lab-discover");
    box.querySelectorAll(".disc-promote").forEach((b) => b.addEventListener("click", () => promoteDiscovered(b.dataset.id, true)));
    box.querySelectorAll(".disc-unpromote").forEach((b) => b.addEventListener("click", () => promoteDiscovered(b.dataset.id, false)));
    box.querySelectorAll(".disc-delete").forEach((b) => b.addEventListener("click", () => deleteDiscovered(b.dataset.id)));
}

async function runDiscover() {
    const n = $("#disc-n") ? $("#disc-n").value : "200";
    const st = $("#disc-run-status");
    try {
        await api(withMarket(`/api/v1/backtest/discover?n_candidates=${n}`), { method: "POST" });
    } catch (e) { if (st) st.textContent = "Could not start: " + e.message; return; }
    toast(`Discovery started — sampling ${n} strategies, validating out-of-sample…`);
    const btn = $("#btn-run-discover"); if (btn) { btn.disabled = true; btn.textContent = "Discovering…"; }
    if (DISCOVER_POLL) clearInterval(DISCOVER_POLL);
    DISCOVER_POLL = setInterval(async () => {
        let s; try { s = await api(withMarket("/api/v1/backtest/discover/status")); } catch { return; }
        const j = s.job || {};
        const stEl = $("#disc-run-status");
        if (stEl) stEl.textContent = `${j.stage || ""} ${Math.round(j.progress_pct || 0)}% ${j.detail || ""}`;
        if (!j.running) {
            clearInterval(DISCOVER_POLL); DISCOVER_POLL = null;
            const last = s.last_run || {};
            await loadDiscover();
            toast(j.error ? `Discovery failed: ${j.error}` : `Discovery done — kept ${last.kept ?? "?"} of ${last.tried ?? "?"} candidates.`, !!j.error);
        }
    }, 3000);
}

async function promoteDiscovered(id, promoted) {
    try {
        await api(withMarket(`/api/v1/backtest/discovered/${encodeURIComponent(id)}/promote?promoted=${promoted}`), { method: "POST" });
        toast(promoted ? "Promoted — Run Backtests to add it to the leaderboard & Trade Ideas." : "Unpromoted.");
        await loadDiscover();
    } catch (e) { toast(e.message, true); }
}

async function deleteDiscovered(id) {
    try {
        await api(withMarket(`/api/v1/backtest/discovered/${encodeURIComponent(id)}`), { method: "DELETE" });
        await loadDiscover();
    } catch (e) { toast(e.message, true); }
}

async function loadLabStrategy(id) {
    const detail = $("#lab-detail");
    detail.classList.remove("hidden");
    detail.innerHTML = `<div class="card"><p class="muted">Loading ${esc(id)}…</p></div>`;
    detail.scrollIntoView({ behavior: "smooth", block: "start" });
    try {
        const s = await api(withMarket(`/api/v1/backtest/strategy/${encodeURIComponent(id)}`));
        renderLabStrategy(s);
    } catch (e) {
        detail.innerHTML = `<div class="card"><p class="muted">Could not load strategy: ${esc(e.message)}</p></div>`;
    }
}

function metricCell(label, value, cls = "", tip = "") {
    const help = tip ? ` <span class="help" data-tip="${esc(tip)}">?</span>` : "";
    return `<div class="metric-cell ${cls}"><div class="metric-label">${label}${help}</div><div class="metric-value">${value}</div></div>`;
}

// Turn a strategy's fresh BUY signals into concrete trade ideas using its own
// risk rules (stop / target / trailing / time stop) from the backtest definition.
function renderTradeIdeas(s) {
    const plan = s.plan || {};
    const buys = (s.signals && s.signals.buy) || [];
    const sl = plan.stop_loss_pct, tp = plan.target_pct, tr = plan.trailing_stop_pct, hold = plan.max_holding_days;
    const pct = (v) => fmt(v * 100, 1);

    const strip = [];
    strip.push("Entry: next open after signal");
    if (sl != null) strip.push(`Stop −${pct(sl)}%`);
    if (tr != null) strip.push(`Trailing ${pct(tr)}%`);
    if (tp != null) strip.push(`Target +${pct(tp)}%`);
    if (hold != null) strip.push(`Time stop ${hold}d`);
    if (sl == null && tp == null && tr == null) strip.push("Exit on strategy signal");
    const stripHtml = strip.map((p) => `<span class="ti-tag">${esc(p)}</span>`).join("");

    if (!buys.length) {
        return `<div class="ti-panel">
            <div class="card-head"><h3>💡 Trade Ideas</h3></div>
            <div class="ti-strip">${stripHtml}</div>
            <p class="muted small mt">No fresh buy signals from this strategy right now — see <b>Holding</b> below for open positions, or run a new backtest for the latest scan.</p>
        </div>`;
    }

    const rows = buys.slice(0, 30).map((b) => {
        const entry = b.price;
        const stop = sl != null ? entry * (1 - sl) : null;
        const tgt = tp != null ? entry * (1 + tp) : null;
        const rr = (sl != null && tp != null && sl > 0) ? tp / sl : null;
        const exitCell = tgt != null
            ? `<span class="pos">₹${fmt(tgt, 2)}</span> <span class="small">(+${pct(tp)}%)</span>`
            : (tr != null ? `<span class="small muted">${pct(tr)}% trailing stop</span>` : `<span class="small muted">on exit signal</span>`);
        return `<tr>
            <td class="mono b">${esc(b.ticker)}</td>
            <td class="small">${esc(b.signal_date)}</td>
            <td class="mono">~₹${fmt(entry, 2)}</td>
            <td class="mono">${stop != null ? `<span class="neg">₹${fmt(stop, 2)}</span> <span class="small">(−${pct(sl)}%)</span>` : "—"}</td>
            <td class="mono">${exitCell}</td>
            <td class="mono">${hold != null ? hold + "d" : "—"}</td>
            <td class="mono">${rr != null ? fmt(rr, 2) + ":1" : "—"}</td>
        </tr>`;
    }).join("");
    const more = buys.length > 30 ? `<p class="muted small mt">+ ${buys.length - 30} more buy signals — see the full list in the BUY panel below.</p>` : "";
    const asOf = (s.signals && s.signals.as_of) || s.run_at || "";

    return `<div class="ti-panel">
        <div class="card-head"><h3>💡 Trade Ideas <span class="muted small">— ${buys.length} current buy setup${buys.length > 1 ? "s" : ""}</span></h3></div>
        <div class="ti-strip">${stripHtml}</div>
        <div class="table-scroll mt"><table class="ti-table">
            <thead><tr><th>Ticker</th><th>Signal</th><th>Entry</th><th>Stop</th><th>Target / Exit</th><th>Max hold</th><th>R:R</th></tr></thead>
            <tbody>${rows}</tbody>
        </table></div>${more}
        <p class="muted small mt">Entry fills at the next session's open; stop/target levels are indicative, computed from the strategy's own risk rules off the latest close${asOf ? ` (as of ${esc(asOf)})` : ""}. Research, not investment advice.</p>
    </div>`;
}

function signalList(items, kind) {
    if (!items?.length) return `<p class="muted small">None right now</p>`;
    const rows = items.slice(0, 40).map((x) => {
        if (kind === "buy") return `<div class="sig-row"><span class="mono b">${esc(x.ticker)}</span><span class="muted small">signal ${esc(x.signal_date)}</span><span class="mono small">${priceFmt(x.price)}</span></div>`;
        if (kind === "sell") return `<div class="sig-row"><span class="mono b">${esc(x.ticker)}</span><span class="muted small">exit ${esc(x.exit_date)} (${esc(x.exit_reason)})</span><span class="mono small ${x.ret_pct >= 0 ? "pos" : "neg"}">${fmt(x.ret_pct)}%</span></div>`;
        return `<div class="sig-row"><span class="mono b">${esc(x.ticker)}</span><span class="muted small">held ${x.days_held}d</span><span class="mono small ${x.unrealized_pct >= 0 ? "pos" : "neg"}">${fmt(x.unrealized_pct)}%</span></div>`;
    }).join("");
    const more = items.length > 40 ? `<p class="muted small">+ ${items.length - 40} more</p>` : "";
    return rows + more;
}

function renderLabStrategy(s) {
    const m = s.metrics;
    const detail = $("#lab-detail");
    const pf = m.profit_factor === "inf" ? "∞" : fmt(m.profit_factor, 2);

    detail.innerHTML = `
    <div class="card">
        <div class="card-head">
            <h2>${esc(s.name)} <span class="pill pill-muted">${esc(s.category)}</span></h2>
            <div class="row gap-s">
                <button class="btn btn-ghost btn-s" onclick="runRobustness('${esc(s.id)}')" title="Walk-forward across out-of-sample windows + Monte-Carlo trade bootstrap">🎲 Robustness</button>
                <button class="btn btn-ghost btn-s" onclick="$('#lab-detail').classList.add('hidden')">✕ Close</button>
            </div>
        </div>
        <p class="muted">${esc(s.description)}</p>
        <div class="metrics-grid">
            ${metricCell("Trades", fmt(m.trades, 0), "", "Completed round-trip trades over the backtest window.")}
            ${metricCell("Win rate", fmt(m.win_rate) + "%", m.win_rate >= 50 ? "pos" : "", "Share of closed trades that ended in profit.")}
            ${metricCell("Profit factor", pf, "", "Gross profit ÷ gross loss. Above 1 = winners outweigh losers; ∞ = no losing trades.")}
            ${metricCell("Expectancy", fmt(m.expectancy_pct) + "%", m.expectancy_pct >= 0 ? "pos" : "neg", "Average profit/loss per trade after 25 bps costs.")}
            ${metricCell("Avg win", "+" + fmt(m.avg_win_pct) + "%", "pos", "Average return of the winning trades.")}
            ${metricCell("Avg loss", fmt(m.avg_loss_pct) + "%", "neg", "Average return of the losing trades.")}
            ${metricCell("Avg hold", fmt(m.avg_holding_days) + "d", "", "Average calendar days a position was held.")}
            ${metricCell("CAGR", fmt(m.cagr_pct) + "%", m.cagr_pct >= 0 ? "pos" : "neg", "Compound annual growth rate of the equity curve.")}
            ${metricCell("Max drawdown", fmt(m.max_drawdown_pct) + "%", "neg", "Largest peak-to-trough equity decline. Closer to 0 is better.")}
            ${metricCell("Sharpe", fmt(m.sharpe, 2), "", "Annualized return ÷ annualized volatility. Higher is better (>1 good).")}
            ${metricCell("Sortino", fmt(m.sortino, 2), "", "Like Sharpe but only penalizes downside volatility.")}
            ${metricCell("Calmar", fmt(m.calmar, 2), "", "CAGR ÷ absolute max drawdown — return per unit of worst-case loss.")}
            ${metricCell("Volatility", fmt(m.ann_volatility_pct) + "%", "", "Annualized standard deviation of the strategy's daily returns.")}
            ${metricCell(`${esc(s.benchmark_label || "Bench")} CAGR`, m.benchmark_cagr_pct != null ? fmt(m.benchmark_cagr_pct) + "%" : "—", "", `Benchmark (${esc(s.benchmark_label || "benchmark")}) CAGR over the same window, for comparison.`)}
            ${metricCell("Alpha", m.alpha_vs_benchmark_pct != null ? fmt(m.alpha_vs_benchmark_pct) + "%" : "—", (m.alpha_vs_benchmark_pct ?? 0) >= 0 ? "pos" : "neg", `Annualized return above (or below) the ${esc(s.benchmark_label || "benchmark")}.`)}
        </div>
        <div id="lab-robustness" class="mt"></div>
        ${renderTradeIdeas(s)}
        <div class="chart-wrap"><canvas id="lab-equity-chart"></canvas></div>
        <div class="sig-grid" style="grid-template-columns: 1fr 1fr;">
            <div class="evi-card"><h3>🔴 SELL now (${s.signals.sell.length})</h3>${signalList(s.signals.sell, "sell")}</div>
            <div class="evi-card"><h3>🟡 Holding (${s.signals.holding.length})</h3>${signalList(s.signals.holding, "hold")}</div>
        </div>
        <div class="sig-grid" style="grid-template-columns: 1fr 1fr;">
            <div class="evi-card"><h3>Best trades</h3>${tradesTable(s.sample_trades)}</div>
            <div class="evi-card"><h3>Worst trades</h3>${tradesTable(s.worst_trades)}</div>
        </div>
    </div>`;

    drawEquityChart(s);
}

// Walk-forward + Monte-Carlo robustness for the open strategy (loads history server-side).
async function runRobustness(id) {
    const box = $("#lab-robustness");
    if (!box) return;
    box.innerHTML = `<div class="card"><p class="muted">🎲 Running walk-forward + Monte-Carlo… (loads history — a few seconds)</p></div>`;
    try {
        const r = await api(withMarket(`/api/v1/backtest/robustness?strategy_id=${encodeURIComponent(id)}`), { method: "POST" });
        box.innerHTML = r.error ? `<div class="card"><p class="muted">Robustness unavailable: ${esc(r.error)}</p></div>` : renderRobustness(r);
    } catch (e) {
        box.innerHTML = `<div class="card"><p class="muted">Robustness failed: ${esc(e.message)}</p></div>`;
    }
}

function renderRobustness(r) {
    const wf = r.walk_forward || {};
    const b = r.bootstrap;
    const vClass = { robust: "pill-buy", mixed: "pill-hold", fragile: "pill-sell", insufficient: "pill-muted" }[wf.verdict] || "pill-muted";
    const wfRows = (wf.windows || []).map((w) => `<tr>
        <td class="small">${esc(w.start)} → ${esc(w.end)}</td>
        <td class="mono">${w.trades}</td>
        <td class="mono ${w.total_return_pct >= 0 ? "pos" : "neg"}">${fmt(w.total_return_pct)}%</td>
        <td class="mono ${w.cagr_pct >= 0 ? "pos" : "neg"}">${fmt(w.cagr_pct)}%</td>
        <td class="mono">${fmt(w.sharpe, 2)}</td>
        <td class="mono neg">${fmt(w.max_drawdown_pct)}%</td></tr>`).join("");
    const band = (x) => x ? `${fmt(x.p5)}% / <b>${fmt(x.p50)}%</b> / ${fmt(x.p95)}%` : "—";
    const bootHtml = b ? `
        <div class="metrics-grid" style="margin-top:10px;">
            ${metricCell("Total return p5/50/95", band(b.total_return_pct), "", "Bootstrap distribution of total compounded return over resampled trade sequences.")}
            ${metricCell("Max drawdown p5/50/95", band(b.max_drawdown_pct), "neg", "Bootstrap distribution of the worst peak-to-trough decline.")}
            ${metricCell("Prob. of loss", fmt(b.prob_loss * 100) + "%", b.prob_loss > 0.3 ? "neg" : "", "Share of resampled sequences that finished underwater.")}
            ${metricCell("Prob. of ruin", fmt(b.prob_ruin * 100) + "%", b.prob_ruin > 0.05 ? "neg" : "", `Share of sequences hitting a ${fmt(b.ruin_dd_pct, 0)}% drawdown.`)}
        </div>` : `<p class="muted small">Too few closed trades to bootstrap.</p>`;
    return `<div class="card">
        <div class="card-head"><h3>🎲 Robustness</h3>
            <span class="pill ${vClass}" title="How many out-of-sample windows were profitable">${esc(wf.verdict || "—")}${wf.pct_windows_positive != null ? ` · ${fmt(wf.pct_windows_positive, 0)}% windows +` : ""}</span></div>
        <p class="muted small">Walk-forward re-tests the edge on ${wf.n_windows || 0} sequential out-of-sample windows (entries restricted per window; warm-up uses full history). Monte-Carlo resamples the ${b ? b.n_trades : 0} closed trades ${b ? b.n_boot : 0}× to bound outcomes.</p>
        <div class="table-scroll"><table class="mini-table"><thead><tr><th>Window</th><th>Trades</th><th>Return</th><th>CAGR</th><th>Sharpe</th><th>MaxDD</th></tr></thead><tbody>${wfRows || `<tr><td colspan="6" class="muted small">No windows</td></tr>`}</tbody></table></div>
        ${bootHtml}
    </div>`;
}

function tradesTable(trades) {
    if (!trades?.length) return `<p class="muted small">No trades</p>`;
    return `<table class="mini-table"><thead><tr><th>Ticker</th><th>Entry</th><th>Exit</th><th>Ret</th><th>Days</th><th>Reason</th></tr></thead>
    <tbody>${trades.map((t) => `<tr>
        <td class="mono b">${esc(t.ticker)}</td><td class="small">${esc(t.entry_date)}</td><td class="small">${esc(t.exit_date)}</td>
        <td class="mono ${t.ret_pct >= 0 ? "pos" : "neg"}">${fmt(t.ret_pct)}%</td>
        <td class="mono">${t.holding_days}</td><td class="small">${esc(t.exit_reason)}</td></tr>`).join("")}
    </tbody></table>`;
}

function drawEquityChart(s) {
    const canvas = $("#lab-equity-chart");
    if (!canvas || !window.Chart) return;
    if (LAB_CHART) { LAB_CHART.destroy(); LAB_CHART = null; }

    const toPts = (arr) => (arr || []).map(([t, v]) => ({ x: t, y: v }));
    const ct = chartTheme();
    try {
        LAB_CHART = new Chart(canvas, {
            type: "line",
            data: {
                datasets: [
                    { label: s.name, data: toPts(s.equity), borderColor: ct.brand, borderWidth: 1.6, pointRadius: 0, tension: 0.1 },
                    { label: s.benchmark_label || "Benchmark", data: toPts(s.benchmark_equity), borderColor: ct.dim, borderWidth: 1.2, pointRadius: 0, borderDash: [5, 4], tension: 0.1 },
                ],
            },
            options: {
                responsive: true, maintainAspectRatio: false, animation: false,
                interaction: { mode: "index", intersect: false },
                scales: {
                    // x-values are epoch-ms; a linear axis with a year formatter avoids
                    // needing a Chart.js date adapter (keeps the dashboard dependency-free).
                    x: {
                        type: "linear",
                        grid: { color: ct.grid },
                        ticks: { color: ct.dim, maxTicksLimit: 10, autoSkip: true, callback: (v) => new Date(v).getFullYear() },
                    },
                    y: { grid: { color: ct.grid }, ticks: { color: ct.dim }, title: { display: true, text: `Growth of ${curSym() || ""}1`, color: ct.dim } },
                },
                plugins: {
                    legend: { labels: { color: ct.text } },
                    tooltip: { callbacks: { title: (items) => (items.length ? new Date(items[0].parsed.x).toLocaleDateString("en-IN") : "") } },
                },
            },
        });
    } catch (e) {
        console.warn("equity chart failed:", e);
        const wrap = canvas.closest(".chart-wrap");
        if (wrap) wrap.innerHTML = `<p class="muted small">Equity chart unavailable.</p>`;
    }
}

// ── Trade Ideas tab (short / medium / long term) ─────────────────

let IDEAS_DATA = null;
let IDEAS_LOADED = false;
let MB_DATA = null, MB_LOADED = false, MB_POLL = null;   // multi-bagger sub-tab state
let MB_TRACKED_DATA = null;                              // multi-bagger tracker state

const HORIZON_ACCENT = { short: "var(--accent)", medium: "var(--warn)", long: "var(--accent-2)" };
const HORIZON_ICON = { short: "⚡", medium: "📈", long: "🏔️" };

async function loadTradeIdeas() {
    const box = $("#ideas-buckets");
    const n = $("#ideas-topn") ? $("#ideas-topn").value : 10;
    box.innerHTML = `<p class="muted">Loading trade ideas…</p>`;
    try {
        IDEAS_DATA = await api(withMarket(`/api/v1/backtest/ideas?max_per_bucket=${n}`));
        IDEAS_LOADED = true;
        renderTradeIdeasTab();
        loadHoldBasket();                    // committed hold book (separate live call; not on the 90s tick)
    } catch (e) {
        box.innerHTML = `<p class="muted">Could not load trade ideas: ${esc(e.message)}</p>`;
    }
}

// ── Today's Basket: the curated cross-horizon best-of, at the top of the board ──
// The daily analog of the intraday shortlist — the full 30-idea board distilled into 3–5 trades sized
// as ONE ¼-Kelly book (weights sum to ≤100%), diversified, regime-scaled, and sticky day-to-day.
function basketStat(label, value, cls, title) {
    return `<div class="bstat" title="${esc(title || "")}"><div class="bstat-val ${cls || ""}">${value}</div><div class="bstat-lbl">${esc(label)}</div></div>`;
}

function basketItemRow(it, i) {
    const alloc = it.alloc_pct != null ? fmt(it.alloc_pct, 1) : "—";
    const hz = `<span class="pill horizon-chip" title="${esc(it.horizon_label || it.horizon || "")} idea">${HORIZON_ICON[it.horizon] || ""} ${esc((it.horizon_label || it.horizon || "").replace(/ term$/i, ""))}</span>`;
    // Carried (held) and fresh (NEW) are mutually exclusive in the badge — a pick held from a prior
    // day reads as "held", a brand-new one as "NEW". Per-idea regime fit lives on the full board below;
    // the basket header already states the regime posture, so the row stays uncluttered.
    const tag = it.carried
        ? `<span class="idea-held" title="Held from a prior day — the basket is sticky, so a still-valid pick isn't churned out unless something materially better appears.">held</span>`
        : (it.is_new ? `<span class="idea-new" title="Fresh pick this run">NEW</span>` : "");
    const buy = (it.buy_low != null && it.buy_high != null) ? `${priceFmt(it.buy_low)}–${priceFmt(it.buy_high)}`
        : (it.price != null ? `~${priceFmt(it.price)}` : "—");
    const tgt = it.target_price != null ? priceFmt(it.target_price) : "—";
    const stop = it.stop_price != null ? priceFmt(it.stop_price) : "—";
    const rr = it.risk_reward != null ? ` · <span title="Reward:risk">${fmt(it.risk_reward, 2)}:1</span>` : "";
    const win = it.win_prob != null ? `${fmt(it.win_prob, 0)}% win` : "";
    const winSrc = it.win_prob_source && it.win_prob_source !== "advertised" ? ` ✓` : "";
    const exp = it.expected_return_pct != null ? `<span class="${it.expected_return_pct >= 0 ? "pos" : "neg"}" title="Expected return per trade = win%·target − loss%·stop (an estimate from measured edge).">exp ${it.expected_return_pct >= 0 ? "+" : ""}${fmt(it.expected_return_pct, 1)}%</span>` : "";
    const isShort = it.side === "short";
    const paperBtn = it.price != null
        ? `<button class="idea-paper-btn paper-btn" data-ticker="${esc(it.ticker)}" data-market="${esc(IDEAS_DATA.market || MARKET.key)}" data-side="${isShort ? "short" : "long"}" data-price="${it.live_price != null ? it.live_price : it.price}" data-stop="${it.stop_price != null ? it.stop_price : ""}" data-target="${it.target_price != null ? it.target_price : ""}" data-alloc="${it.alloc_pct != null ? it.alloc_pct : ""}" data-source="basket" title="Paper-trade ${esc(it.ticker)} at ${it.alloc_pct != null ? it.alloc_pct + "% weight" : "market"}">📝</button>`
        : "";
    return `<div class="basket-item" data-ticker="${esc(it.ticker)}" title="Click to deep-dive ${esc(it.ticker)}">
        <div class="bi-rank">${i + 1}</div>
        <div class="bi-alloc" title="Suggested portfolio weight — capped ¼-Kelly, sized across the whole basket as one book (weights sum to ≤100%).">
            <span class="bi-alloc-pct">${alloc}%</span><span class="bi-alloc-lbl">weight</span>
        </div>
        <div class="bi-main">
            <div class="bi-top"><span class="bi-ticker">${esc(it.ticker)}</span>${isShort ? `<span class="pill pill-sell" title="Short — profit if the price falls">SHORT</span>` : ""}${hz}${tag}</div>
            <div class="bi-plan mono small">${isShort ? "Short" : "Buy"} ${buy} · <span class="pos">Tgt ${tgt}</span> · <span class="neg">Stop ${stop}</span>${rr}</div>
            <div class="bi-why muted small">${win}${winSrc}${win && exp ? " · " : ""}${exp}</div>
        </div>
        <div class="bi-actions">${paperBtn}</div>
    </div>`;
}

function renderBasketCard(b) {
    if (!b || !b.enabled || !(b.items || []).length) return "";
    const s = b.summary || {};
    const exp = s.expected_return_pct;
    const expTile = basketStat("Expected return", `${exp != null && exp >= 0 ? "+" : ""}${fmt(exp, 1)}%`,
        exp != null && exp >= 0 ? "pos" : "neg",
        "Alloc-weighted expected return on capital (includes the cash drag from staying partly in cash). An estimate from measured edge — not a guarantee.");
    const winTile = basketStat("Blended win rate", s.avg_win_pct != null ? `${fmt(s.avg_win_pct, 0)}%` : "—", "",
        "Allocation-weighted average win probability across the basket (best available: learned → calibrated → advertised).");
    const expoTile = basketStat("Invested", `${fmt(s.total_alloc_pct, 0)}%`, "",
        `${fmt(s.total_alloc_pct, 0)}% of capital deployed, ${fmt(s.cash_pct, 0)}% held as cash (the book is sized to a risk budget, so it won't force full deployment).`);
    const divTile = basketStat("Diversification", `${s.n_independent || b.items.length} names`,
        "", `${s.n_independent || b.items.length} independent names (one per correlation cluster)${s.n_sectors ? ` across ${s.n_sectors} sector${s.n_sectors === 1 ? "" : "s"}` : ""}.`);
    // Horizon mix chips (⚡ short / 📈 medium / 🏔️ long).
    const mix = s.horizon_mix || {};
    const mixChips = ["short", "medium", "long"].filter((h) => mix[h])
        .map((h) => `<span class="pill pill-muted" title="${mix[h]} ${h}-term">${HORIZON_ICON[h]} ${mix[h]}</span>`).join(" ");
    const regimeState = b.regime_state;
    const regimePill = regimeState
        ? `<span class="pill" title="Basket size + risk budget are scaled by the market regime — more names in risk-on, fewer and smaller in risk-off." style="border-color:${regimeState === "risk-on" ? "#30a46c" : regimeState === "risk-off" ? "#e5484d" : "#8a8f98"}66;">${regimeState === "risk-on" ? "🟢" : regimeState === "risk-off" ? "🔴" : "⚪"} ${esc(regimeState)}</span>`
        : "";
    const w = b.weights || {};
    const carried = s.carried_count ? ` · ${s.carried_count} held from before` : "";
    const spent = s.excluded_spent ? ` · ${s.excluded_spent} skipped (already hit stop/target)` : "";
    const foot = `Balanced score = ${fmt((w.ev || 0) * 100, 0)}% expected value · ${fmt((w.confidence || 0) * 100, 0)}% confidence · ${fmt((w.quality || 0) * 100, 0)}% rank. Sticky across days (hysteresis)${carried}${spent}. ${esc(b.note || "")}`;
    return `<div class="card basket-card">
        <div class="basket-head">
            <div class="basket-title">
                <h3>🧺 Today's Basket <span class="pill">${b.items.length} trade${b.items.length === 1 ? "" : "s"}</span> ${regimePill} ${mixChips}</h3>
                <span class="muted small">Take these ${b.items.length}, at these weights — the full board distilled into one deployable book you can actually hold.</span>
            </div>
            <div class="basket-actions">
                <button id="basket-deploy-btn" class="btn btn-s" title="Deploy the basket as a NAV-tracked portfolio (P&L, rebalance, AI review) — see the Portfolio tab. Idempotent: re-deploying re-syncs the same portfolio.">📈 Deploy as portfolio</button>
                <button id="basket-bt-btn" class="btn btn-ghost btn-s" title="Backtest the basket construction method — a weekly-rebalanced held-basket equity curve vs the benchmark.">📊 Backtest</button>
                <button id="basket-track-btn" class="btn btn-ghost btn-s" title="Commit this basket and track each pick's outcome individually (also anchors tomorrow's sticky basket).">📌 Track</button>
                <button id="basket-paper-btn" class="btn btn-ghost btn-s" title="Open a paper trade for every pick at its suggested weight — see the Paper tab.">📝 Paper all</button>
            </div>
        </div>
        <div class="basket-metrics">${expTile}${winTile}${expoTile}${divTile}</div>
        <div class="basket-items">${b.items.map((it, i) => basketItemRow(it, i)).join("")}</div>
        <div class="basket-foot muted small">${foot}</div>
    </div>`;
}

// ── Best Odds: the day's fresh setups ranked PURELY by win probability (the new hero board) ──
// "Which trades are most likely to win" — a simple ranked Top-N to pick from, vs Today's Basket's
// diversified/sized/holdable book. Reuses the .basket-* card/row chrome (so click-to-analyze + the
// per-row paper button wire up for free), but leads each row with the win% instead of a weight.
const WIN_SRC_LABEL = { learned: "learned", calibrated: "calibrated", measured: "measured", advertised: "backtest" };

function winColor(p) {
    if (p == null) return "var(--text-mute)";
    if (p >= 60) return "var(--pos)";
    if (p >= 50) return "var(--text)";
    return "var(--text-mute)";
}

function bestOddsRow(it) {
    const wp = it.win_prob;
    const backed = it.win_prob_source && it.win_prob_source !== "advertised";   // learned / calibrated
    const srcTxt = it.win_prob_source ? (WIN_SRC_LABEL[it.win_prob_source] || it.win_prob_source) : "";
    const check = backed
        ? `<span class="bo-check" title="Model/calibrated win rate — learned from realized outcomes, not the raw in-sample backtest.">✓</span>` : "";
    const hz = `<span class="pill horizon-chip" title="${esc(it.horizon_label || it.horizon || "")} idea">${HORIZON_ICON[it.horizon] || ""} ${esc((it.horizon_label || it.horizon || "").replace(/ term$/i, ""))}</span>`;
    const conf = it.pillar_count != null
        ? `<span class="pill pill-muted" title="${it.pillar_count} independent signal${it.pillar_count === 1 ? "" : "s"} confirm this idea (technical, quality, regime, sector, news, smart-money).">★ ${it.pillar_count}</span>` : "";
    const buy = (it.buy_low != null && it.buy_high != null) ? `${priceFmt(it.buy_low)}–${priceFmt(it.buy_high)}`
        : (it.price != null ? `~${priceFmt(it.price)}` : "—");
    const tgt = it.target_price != null ? priceFmt(it.target_price) : "—";
    const stop = it.stop_price != null ? priceFmt(it.stop_price) : "—";
    const rr = it.risk_reward != null ? ` · <span title="Reward:risk">${fmt(it.risk_reward, 2)}:1</span>` : "";
    const exp = it.expected_return_pct != null ? `<span class="${it.expected_return_pct >= 0 ? "pos" : "neg"}" title="Expected return per trade = win%·target − loss%·stop (an estimate).">exp ${it.expected_return_pct >= 0 ? "+" : ""}${fmt(it.expected_return_pct, 1)}%</span>` : "";
    const paperBtn = it.price != null
        ? `<button class="idea-paper-btn paper-btn" data-ticker="${esc(it.ticker)}" data-market="${esc(IDEAS_DATA.market || MARKET.key)}" data-side="long" data-price="${it.live_price != null ? it.live_price : it.price}" data-stop="${it.stop_price != null ? it.stop_price : ""}" data-target="${it.target_price != null ? it.target_price : ""}" data-source="best-odds" title="Paper-trade ${esc(it.ticker)}">📝</button>`
        : "";
    return `<div class="basket-item best-odds-item" data-ticker="${esc(it.ticker)}" title="Click to deep-dive ${esc(it.ticker)}">
        <div class="bi-rank">${it.rank}</div>
        <div class="bo-win" title="Best-available win probability${srcTxt ? ` (${esc(srcTxt)})` : ""}.">
            <span class="bo-win-pct" style="color:${winColor(wp)};">${wp != null ? fmt(wp, 0) + "%" : "—"}</span>
            <span class="bo-win-lbl">win ${check}</span>
        </div>
        <div class="bi-main">
            <div class="bi-top"><span class="bi-ticker">${esc(it.ticker)}</span>${hz}${conf}</div>
            <div class="bi-plan mono small">Buy ${buy} · <span class="pos">Tgt ${tgt}</span> · <span class="neg">Stop ${stop}</span>${rr}</div>
            <div class="bi-why muted small">${srcTxt ? esc(srcTxt) + " odds" : ""}${srcTxt && exp ? " · " : ""}${exp}</div>
        </div>
        <div class="bi-actions">${paperBtn}</div>
    </div>`;
}

function bestWinRange(items) {
    const w = items.map((it) => it.win_prob).filter((x) => x != null);
    if (!w.length) return "—";
    return `${fmt(Math.max(...w), 0)}–${fmt(Math.min(...w), 0)}%`;
}

function renderBestOddsBoard(b) {
    if (!b || !b.enabled || !(b.items || []).length) return "";
    const s = b.summary || {};
    const n = b.items.length;
    const avgTile = basketStat("Avg win rate", s.avg_win_pct != null ? `${fmt(s.avg_win_pct, 0)}%` : "—", "",
        "Average best-available win probability across the picks.");
    const backedTile = basketStat("Model-backed", `${s.backed_count || 0}/${n}`, "",
        "How many picks' win rates are learned/calibrated from realized outcomes (✓) vs the raw in-sample backtest rate. Prefer the backed ones.");
    const rrTile = basketStat("Avg reward:risk", s.avg_reward_risk != null ? `${fmt(s.avg_reward_risk, 2)}:1` : "—", "",
        "Average reward-to-risk across the picks — a high win rate at poor R:R can still lose money.");
    const rangeTile = basketStat("Win range", bestWinRange(b.items), "", "Highest to lowest win probability among the picks.");
    const src = s.win_prob_sources || {};
    const srcParts = Object.keys(src).map((k) => `${src[k]} ${WIN_SRC_LABEL[k] || k}`).join(" · ");
    const foot = `Ranked purely by each setup's best-available win rate${srcParts ? ` (${esc(srcParts)})` : ""}. ${esc(b.note || "")}`;
    return `<div class="card basket-card best-odds-card">
        <div class="basket-head">
            <div class="basket-title">
                <h3>🏆 Best Odds <span class="pill">Top ${n}</span></h3>
                <span class="muted small">The open setups <b>most likely to win</b>, ranked by win probability — pick one and go. For a diversified book to hold, use the full horizon board below.</span>
            </div>
            <div class="basket-actions">
                <button id="best-odds-paper-btn" class="btn btn-ghost btn-s" title="Open a paper trade for every pick — see the Paper tab.">📝 Paper all</button>
            </div>
        </div>
        <div class="basket-metrics">${avgTile}${backedTile}${rrTile}${rangeTile}</div>
        <div class="basket-items best-odds-items">${b.items.map(bestOddsRow).join("")}</div>
        <div class="basket-foot muted small">${foot}</div>
    </div>`;
}

// Paper-trade every Best-Odds pick (fires the per-row paper buttons; scoped so it never touches other cards).
async function paperTradeBestOdds(e) {
    const btn = e.currentTarget;
    const rows = Array.from(document.querySelectorAll(".best-odds-items .paper-btn:not(.paper-done)"));
    if (!rows.length) { toast("No picks to paper-trade."); return; }
    btn.disabled = true; btn.innerHTML = "…";
    for (const b of rows) { await paperTradeIdea(b); }        // sequential: reuses the single-idea handler
    toast(`📝 Papered ${rows.length} pick${rows.length === 1 ? "" : "s"} — see the Paper tab.`);
    btn.innerHTML = "✓ papered all";
}

// ── Coattail Basket: the daily basket filtered to names the smart money is buying too ──
// Read-only sibling of Today's Basket — reuses the same row/stat renderers. Only shows when at least
// one current idea has smart-money backing (13F/gurus, COT commercials, HL top traders, NSE bulk buyers).
function renderCoattailCard(b) {
    if (!b || !b.enabled || !(b.items || []).length) return "";
    const s = b.summary || {};
    const exp = s.expected_return_pct;
    const backed = s.smart_money_backed != null ? s.smart_money_backed : b.items.length;
    const expTile = basketStat("Expected return", `${exp != null && exp >= 0 ? "+" : ""}${fmt(exp, 1)}%`,
        exp != null && exp >= 0 ? "pos" : "neg", "Alloc-weighted expected return on capital — an estimate from measured edge.");
    const investedTile = basketStat("Invested", `${fmt(s.total_alloc_pct, 0)}%`, "",
        `${fmt(s.total_alloc_pct, 0)}% deployed, ${fmt(s.cash_pct, 0)}% cash.`);
    const backedTile = basketStat("Smart-money names", `${backed}`, "",
        "How many current ideas the smart money is accumulating — the pool this basket is drawn from.");
    return `<div class="card basket-card coattail-card">
        <div class="basket-head">
            <div class="basket-title">
                <h3>🤝 Coattail Basket <span class="pill">${b.items.length} trade${b.items.length === 1 ? "" : "s"}</span></h3>
                <span class="muted small">Today's basket, kept to names the <b>smart money is accumulating</b> — SEC&nbsp;13F&nbsp;&amp;&nbsp;gurus, CFTC commercials, Hyperliquid top traders, or NSE bulk buyers. Point-in-time confirmation, not part of the backtest.</span>
            </div>
        </div>
        <div class="basket-metrics">${expTile}${investedTile}${backedTile}</div>
        <div class="basket-items">${b.items.map((it, i) => basketItemRow(it, i)).join("")}</div>
        <div class="basket-foot muted small">${esc(b.note || "")}</div>
    </div>`;
}

// ── Coattail SHORT sleeve: the sell-side mirror — SHORT ideas the smart money is DISTRIBUTING ──
// Read-only sibling of the Coattail Basket. Only shows when at least one current short has smart-money
// backing (crypto whales net-short via Hyperliquid). Empty on long-only markets / risk-on tides.
function renderCoattailShortCard(b) {
    if (!b || !b.enabled || !(b.items || []).length) return "";
    const s = b.summary || {};
    const exp = s.expected_return_pct;
    const backed = s.smart_money_backed != null ? s.smart_money_backed : b.items.length;
    const expTile = basketStat("Expected return", `${exp != null && exp >= 0 ? "+" : ""}${fmt(exp, 1)}%`,
        exp != null && exp >= 0 ? "pos" : "neg", "Alloc-weighted expected return on capital for the short book — an estimate from measured edge.");
    const investedTile = basketStat("Invested", `${fmt(s.total_alloc_pct, 0)}%`, "",
        `${fmt(s.total_alloc_pct, 0)}% deployed short, ${fmt(s.cash_pct, 0)}% cash.`);
    const backedTile = basketStat("Whale-short names", `${backed}`, "",
        "How many current shorts the smart money is net-short — the pool this sleeve is drawn from.");
    return `<div class="card basket-card coattail-card coattail-short-card">
        <div class="basket-head">
            <div class="basket-title">
                <h3>🐋↓ Coattail Shorts <span class="pill pill-sell">${b.items.length} short${b.items.length === 1 ? "" : "s"}</span></h3>
                <span class="muted small">The sell-side mirror — SHORT ideas the <b>smart money is distributing</b> (crypto whales net-short via Hyperliquid top traders). Point-in-time confirmation, not part of the backtest. Sized inverted (a risk-off tide is a tailwind for shorts).</span>
            </div>
        </div>
        <div class="basket-metrics">${expTile}${investedTile}${backedTile}</div>
        <div class="basket-items">${b.items.map((it, i) => basketItemRow(it, i)).join("")}</div>
        <div class="basket-foot muted small">${esc(b.note || "")}</div>
    </div>`;
}

// Commit + track the whole curated basket (also advances the sticky anchor).
async function trackBasket(e) {
    const btn = e.currentTarget;
    const orig = btn.innerHTML; btn.disabled = true; btn.innerHTML = "…";
    try {
        const r = await api(withMarket("/api/v1/backtest/basket/track"), { method: "POST" });
        const n = (r && r.basket && r.basket.count) || 0;
        toast(`📌 Basket tracked — ${n} pick${n === 1 ? "" : "s"} committed. Follow them in the tracker.`);
        btn.innerHTML = "✓ tracked";
        loadTrackedIdeas();
    } catch (err) {
        toast(err.message, true); btn.disabled = false; btn.innerHTML = orig;
    }
}

// Deploy the whole basket as a NAV-tracked portfolio (idempotent — re-syncs the same one).
async function deployBasket(e) {
    const btn = e.currentTarget;
    const orig = btn.innerHTML; btn.disabled = true; btn.innerHTML = "…";
    try {
        const r = await api(withMarket("/api/v1/backtest/basket/deploy"), { method: "POST" });
        if (!r || !r.deployed) { toast((r && r.note) || "Nothing to deploy", true); btn.disabled = false; btn.innerHTML = orig; return; }
        const n = (r.basket && r.basket.count) || (r.snapshot && r.snapshot.positions_total) || 0;
        toast(`📈 Deployed "${esc(r.portfolio_name || "basket")}" — ${n} positions, NAV-tracked. See the Portfolio tab.`);
        btn.innerHTML = "✓ deployed";
    } catch (err) {
        toast(err.message, true); btn.disabled = false; btn.innerHTML = orig;
    }
}

// ── Basket-method backtest panel (held-basket, weekly rebalance, vs benchmark) ──
let BASKET_BT_CHART = null;
let BASKET_BT_POLL = null;

function toggleBasketBacktest() {
    const panel = $("#basket-bt-panel");
    if (!panel) return;
    const showing = panel.style.display !== "none";
    if (showing) { panel.style.display = "none"; return; }
    panel.style.display = "";
    if (!panel.dataset.loaded) loadBasketBacktest();
}

async function loadBasketBacktest() {
    const panel = $("#basket-bt-panel");
    if (!panel) return;
    panel.innerHTML = `<p class="muted small">Loading basket backtest…</p>`;
    try {
        const r = await api(withMarket("/api/v1/backtest/basket/backtest"));
        panel.dataset.loaded = "1";
        renderBasketBacktest(r);
    } catch (e) {
        panel.innerHTML = `<p class="muted small">Could not load: ${esc(e.message)}</p>`;
    }
}

async function runBasketBacktest() {
    const panel = $("#basket-bt-panel");
    const btn = panel && panel.querySelector("#basket-bt-run");
    if (btn) { btn.disabled = true; btn.innerHTML = "…"; }
    try {
        await api(withMarket("/api/v1/backtest/basket/backtest/run"), { method: "POST" });
    } catch (e) {
        toast(e.message, true); if (btn) { btn.disabled = false; btn.innerHTML = "Run backtest"; } return;
    }
    // Poll status until the job finishes, then reload the result.
    if (BASKET_BT_POLL) clearInterval(BASKET_BT_POLL);
    BASKET_BT_POLL = setInterval(async () => {
        let s;
        try { s = await api("/api/v1/backtest/basket/backtest/status"); } catch { return; }
        const bar = panel && panel.querySelector("#basket-bt-prog");
        if (bar) bar.textContent = `${esc(s.stage)} — ${fmt(s.progress_pct, 0)}%${s.detail ? " · " + esc(s.detail) : ""}`;
        if (!s.running) {
            clearInterval(BASKET_BT_POLL); BASKET_BT_POLL = null;
            if (s.error) toast("Backtest failed: " + s.error, true);
            loadBasketBacktest();
        }
    }, 3000);
}

function renderBasketBacktest(r) {
    const panel = $("#basket-bt-panel");
    if (!panel) return;
    const runRow = `<div class="row gap-s" style="align-items:center; flex-wrap:wrap;">
        <b>📊 Basket backtest</b>
        <span class="muted small">Held-basket, weekly-rebalanced, point-in-time — validates the construction method vs the benchmark.</span>
        <span style="flex:1;"></span>
        <span id="basket-bt-prog" class="muted small"></span>
        <button id="basket-bt-run" class="btn btn-s">${r && r.ran ? "Re-run" : "Run backtest"}</button>
    </div>`;
    if (!r || !r.ran) {
        panel.innerHTML = runRow + `<p class="muted small" style="margin-top:10px;">${esc((r && r.note) || "Not run yet.")} A run takes a minute or two (loads history + replays weekly).</p>`;
        panel.querySelector("#basket-bt-run").addEventListener("click", runBasketBacktest);
        return;
    }
    const m = r.metrics || {}, e = r.extras || {};
    const alpha = m.alpha_vs_benchmark_pct;
    const tiles = [
        basketStat("CAGR", `${m.cagr_pct >= 0 ? "+" : ""}${fmt(m.cagr_pct, 1)}%`, m.cagr_pct >= 0 ? "pos" : "neg", "Compound annual growth rate of the held basket over the backtest."),
        basketStat("vs Benchmark", `${alpha >= 0 ? "+" : ""}${fmt(alpha, 1)}%`, alpha >= 0 ? "pos" : "neg", `Alpha: basket CAGR minus ${esc(r.benchmark_label || "benchmark")} CAGR (${fmt(m.benchmark_cagr_pct, 1)}%/yr).`),
        basketStat("Sharpe", fmt(m.sharpe, 2), "", "Risk-adjusted return (annualized)."),
        basketStat("Max drawdown", `${fmt(m.max_drawdown_pct, 1)}%`, "neg", "Worst peak-to-trough decline of the basket equity curve."),
    ].join("");
    const sub = `<div class="muted small" style="margin-top:10px;">
        Over <b>${esc(r.as_of_start)} → ${esc(r.as_of_end)}</b> · ${e.rebalances} weekly rebalances ·
        avg ${fmt(e.avg_basket_size, 1)} names · turnover ${fmt(e.avg_turnover_pct, 0)}%/rebalance ·
        hit rate ${fmt(e.hit_rate_pct, 0)}% · total return ${fmt(m.total_return_pct, 0)}% ·
        ${r.names} names × ${r.strategies} strategies.
        <br>Validates the construction method (conviction + diversification + regime-scaled ¼-Kelly + hysteresis); the calibration/fusion win-rate layer is excluded to avoid lookahead — an estimate, not advice.
    </div>`;
    panel.innerHTML = runRow +
        `<div class="basket-metrics" style="margin:14px 0;">${tiles}</div>` +
        `<div class="chart-wrap" style="height:240px;"><canvas id="basket-bt-chart"></canvas></div>` + sub;
    panel.querySelector("#basket-bt-run").addEventListener("click", runBasketBacktest);
    drawBasketBtEquity(r.equity_curve || []);
}

function drawBasketBtEquity(curve) {
    const canvas = $("#basket-bt-chart");
    if (!canvas || !window.Chart) return;
    if (BASKET_BT_CHART) { BASKET_BT_CHART.destroy(); BASKET_BT_CHART = null; }
    const ct = chartTheme();
    const labels = curve.map((p) => p.date);
    BASKET_BT_CHART = new Chart(canvas, {
        type: "line",
        data: {
            labels,
            datasets: [
                { label: "Basket", data: curve.map((p) => p.nav), borderColor: ct.brand, borderWidth: 1.8, pointRadius: 0, fill: true, backgroundColor: hexTint(ct.brand, 0.1), tension: 0.1 },
                { label: "Benchmark", data: curve.map((p) => p.benchmark_nav), borderColor: ct.dim, borderWidth: 1.2, pointRadius: 0, borderDash: [4, 3], fill: false, tension: 0.1 },
            ],
        },
        options: {
            responsive: true, maintainAspectRatio: false, animation: false,
            interaction: { mode: "index", intersect: false },
            scales: {
                x: { grid: { display: false }, ticks: { color: ct.dim, maxTicksLimit: 8, autoSkip: true } },
                y: { grid: { color: ct.grid }, ticks: { color: ct.dim, callback: (v) => v + "×" } },
            },
            plugins: { legend: { display: true, labels: { color: ct.dim, boxWidth: 12 } } },
        },
    });
}

// Paper-trade every pick in the basket at its suggested weight (fires the per-row paper buttons).
async function paperTradeBasket(e) {
    const btn = e.currentTarget;
    const rows = Array.from(document.querySelectorAll(".basket-items .paper-btn:not(.paper-done)"));
    if (!rows.length) { toast("No basket picks to paper-trade."); return; }
    btn.disabled = true; btn.innerHTML = "…";
    for (const b of rows) { await paperTradeIdea(b); }        // sequential: reuses the single-idea handler
    toast(`📝 Papered ${rows.length} basket pick${rows.length === 1 ? "" : "s"} — see the Paper tab.`);
    btn.innerHTML = "✓ papered all";
}

// ── My Hold Basket — the committed, conviction-held book (below Today's Basket) ──
async function loadHoldBasket() {
    const card = $("#hold-basket-card");
    if (!card) return;
    if (!card.dataset.loaded) card.innerHTML = `<p class="muted small">Loading your Hold Basket…</p>`;
    try {
        const d = await api(withMarket("/api/v1/backtest/hold-basket"));
        card.dataset.loaded = "1";
        renderHoldBasket(d);
    } catch (e) {
        card.innerHTML = `<p class="muted small">Could not load Hold Basket: ${esc(e.message)}</p>`;
    }
}

function _holdActionRow(a) {
    const cls = a.action === "EXIT" ? "neg" : (a.action === "ADD" || a.action === "BUY") ? "pos" : "muted";
    const icon = a.action === "EXIT" ? "✕" : (a.action === "ADD" || a.action === "BUY") ? "＋" : "•";
    const ret = a.return_pct != null ? ` <span class="${a.return_pct >= 0 ? "pos" : "neg"}">(${a.return_pct >= 0 ? "+" : ""}${fmt(a.return_pct)}%)</span>` : "";
    return `<div class="hb-change"><span class="hb-act ${cls}">${icon} ${a.action}</span>
        <b>${esc(a.ticker)}</b>${ret} <span class="muted small">— ${esc(a.reason || "")}</span></div>`;
}

function renderHoldBasket(d) {
    const card = $("#hold-basket-card");
    if (!card) return;
    const head = (right) => `<div class="row gap-s" style="align-items:center; flex-wrap:wrap;">
        <b>🎯 My Hold Basket</b>
        <span class="muted small">A committed, conviction-held book — it stays put until something material changes.</span>
        <span style="flex:1;"></span>${right || ""}</div>`;

    if (!d || !d.committed) {
        const prev = (d && d.preview && d.preview.items) || [];
        const pills = prev.slice(0, 6).map((it) =>
            `<span class="pill pill-muted" title="conviction ${it.conviction} · ${esc(it.horizon || "")}">${esc(it.ticker)} ${HORIZON_ICON[it.horizon] || ""}</span>`).join(" ");
        card.innerHTML = head(`<button id="hb-commit" class="btn btn-s">📌 Commit Hold Basket</button>`) +
            (prev.length
                ? `<p class="muted small" style="margin-top:10px;">Today's hold-worthy picks (higher-conviction, longer-horizon, low-churn): ${pills}. Commit them as a book you can hold — you'll only be nudged to change on a real event.</p>`
                : `<p class="muted small" style="margin-top:10px;">No hold-worthy names right now (need a backtest + signals for this market).</p>`);
        const c = card.querySelector("#hb-commit");
        if (c) c.addEventListener("click", commitHoldBasket);
        return;
    }

    const material = d.material;
    const statusPill = material
        ? `<span class="pill" style="border-color:#e5484d66; color:var(--neg);">⚠️ ${(d.counts.EXIT || 0) + (d.counts.ADD || 0) + (d.counts.BUY || 0)} change(s) to review</span>`
        : `<span class="pill" style="border-color:#30a46c66; color:var(--pos);">✓ No action needed</span>`;
    const bookRet = d.book_return_pct;
    const rows = (d.holdings || []).map((h, i) => `<tr>
        <td class="muted">${i + 1}</td>
        <td><b>${esc(h.ticker)}</b> <span class="pill pill-muted">${HORIZON_ICON[h.horizon] || ""} ${esc((h.horizon || "").slice(0, 3))}</span></td>
        <td class="mono">${priceFmt(h.entry_price)}</td>
        <td class="mono">${priceFmt(h.current_price)}</td>
        <td class="mono ${(h.return_pct || 0) >= 0 ? "pos" : "neg"}">${h.return_pct != null ? (h.return_pct >= 0 ? "+" : "") + fmt(h.return_pct) + "%" : "—"}</td>
        <td class="mono">${fmt((h.weight || 0) * 100, 1)}%</td></tr>`).join("");
    const changes = (d.actions || []).filter((a) => a.action !== "HOLD");
    const diff = material
        ? `<div class="hb-changes" style="margin-top:12px;">
             <div class="muted small" style="margin-bottom:6px;">Proposed changes — review, then apply:</div>
             ${changes.map(_holdActionRow).join("")}
             <div class="row gap-s" style="margin-top:10px;">
               <button id="hb-apply" class="btn btn-s">✓ Apply changes</button>
               <button id="hb-refresh" class="btn btn-ghost btn-s">Re-check</button>
             </div></div>`
        : `<div class="row gap-s" style="margin-top:10px;"><button id="hb-refresh" class="btn btn-ghost btn-s">Re-check</button></div>`;
    card.innerHTML = head(statusPill) +
        `<div class="muted small" style="margin-top:6px;">${d.count} names · book ${bookRet >= 0 ? "+" : ""}${fmt(bookRet)}% since entry · ${fmt(d.invested_pct, 0)}% invested · tracked as portfolio "${esc(d.portfolio_name || "")}".</div>
         <div class="table-scroll" style="margin-top:10px;"><table class="trk-table"><thead><tr>
           <th>#</th><th>Name</th><th>Entry</th><th>Now</th><th>Return</th><th>Weight</th></tr></thead>
           <tbody>${rows}</tbody></table></div>${diff}
         <div style="margin-top:10px;"><button id="hb-uncommit" class="linkbtn muted small">Uncommit this book</button></div>`;
    const ap = card.querySelector("#hb-apply"); if (ap) ap.addEventListener("click", applyHoldRebalance);
    const rf = card.querySelector("#hb-refresh"); if (rf) rf.addEventListener("click", loadHoldBasket);
    const un = card.querySelector("#hb-uncommit"); if (un) un.addEventListener("click", uncommitHoldBasket);
}

async function commitHoldBasket(e) {
    const btn = e.currentTarget; btn.disabled = true; btn.innerHTML = "…";
    try {
        const r = await api(withMarket("/api/v1/backtest/hold-basket/commit"), { method: "POST" });
        if (r && r.committed === false) { toast(r.note || "Nothing to commit", true); btn.disabled = false; btn.innerHTML = "📌 Commit Hold Basket"; return; }
        toast(`🎯 Hold Basket committed — ${r.count || 0} names. It stays put until something material changes.`);
        renderHoldBasket(r);
    } catch (err) { toast(err.message, true); btn.disabled = false; btn.innerHTML = "📌 Commit Hold Basket"; }
}

async function applyHoldRebalance(e) {
    const btn = e.currentTarget; btn.disabled = true; btn.innerHTML = "…";
    try {
        const r = await api(withMarket("/api/v1/backtest/hold-basket/apply"), { method: "POST" });
        if (r && r.applied === false) { toast(r.note || "Nothing to apply"); }
        else { toast("✓ Hold Basket rebalanced."); }
        renderHoldBasket(r.committed ? r : (await api(withMarket("/api/v1/backtest/hold-basket"))));
    } catch (err) { toast(err.message, true); btn.disabled = false; btn.innerHTML = "✓ Apply changes"; }
}

async function uncommitHoldBasket() {
    if (!confirm("Forget your committed Hold Basket? (Its NAV-tracked portfolio is kept.)")) return;
    try {
        await api(withMarket("/api/v1/backtest/hold-basket"), { method: "DELETE" });
        toast("Hold Basket uncommitted.");
        loadHoldBasket();
    } catch (err) { toast(err.message, true); }
}

function renderTradeIdeasTab() {
    const box = $("#ideas-buckets");
    const buckets = IDEAS_DATA.buckets || {};
    const horizons = IDEAS_DATA.horizons || ["short", "medium", "long"];
    const anyIdeas = horizons.some((h) => buckets[h] && (buckets[h].candidates || []).length);
    if (!anyIdeas) {
        box.innerHTML = `<div class="card"><p class="muted">No <b>${esc(mkt().label || MARKET.key)}</b> trade ideas yet — run a backtest for this market in the <b>Strategy Lab</b> tab first. It scans every strategy's current buy signals, which this tab groups by time horizon.</p></div>`;
        return;
    }
    box.innerHTML = marketConditionsSection(IDEAS_DATA.regime, IDEAS_DATA.fear_greed) + renderBestOddsBoard(IDEAS_DATA.best_odds) + renderCoattailCard(IDEAS_DATA.coattail_basket) + renderCoattailShortCard(IDEAS_DATA.coattail_short_basket) + cryptoRiskBanner(IDEAS_DATA.risk_note) + pillarsBanner(IDEAS_DATA.pillars) + rankingBanner(IDEAS_DATA.ranking) + calibrationBanner(IDEAS_DATA.calibration, IDEAS_DATA.fusion) + horizons.map((h) => renderHorizonColumn(buckets[h])).join("");
    updateIdeasRunInfo();

    box.querySelectorAll(".idea-row, .basket-item[data-ticker]").forEach((row) =>
        row.addEventListener("click", () => analyzeTicker(row.dataset.ticker)));
    box.querySelectorAll(".idea-ai-btn").forEach((btn) =>
        btn.addEventListener("click", (e) => { e.stopPropagation(); onIdeaAI(btn); }));
    box.querySelectorAll(".paper-btn").forEach((btn) =>
        btn.addEventListener("click", (e) => { e.stopPropagation(); paperTradeIdea(btn); }));
    const trackBtn = box.querySelector("#basket-track-btn");
    if (trackBtn) trackBtn.addEventListener("click", trackBasket);
    const paperAllBtn = box.querySelector("#basket-paper-btn");
    if (paperAllBtn) paperAllBtn.addEventListener("click", paperTradeBasket);
    const boPaperBtn = box.querySelector("#best-odds-paper-btn");
    if (boPaperBtn) boPaperBtn.addEventListener("click", paperTradeBestOdds);
    const deployBtn = box.querySelector("#basket-deploy-btn");
    if (deployBtn) deployBtn.addEventListener("click", deployBasket);
    const btBtn = box.querySelector("#basket-bt-btn");
    if (btBtn) btBtn.addEventListener("click", toggleBasketBacktest);
}

// Regime badge: the market's risk-on/off tide (benchmark 200-DMA + a vol/macro factor). Mirrors the
// crypto Fear & Greed pill. Regime-aligned longs rank first; risk-off trims position size.
// ── Market Conditions: the header the daily board trades with ──────────────
// Pairs the Fear & Greed *mood* (a 0–100 gauge) with the risk-on/off *direction* (the regime tide),
// at the very top of the Trade Ideas board. Both come straight off the /ideas payload; either can be
// absent (gate off / old payload) and the section still renders whatever it has.

// Fear/greed accent — fear = red, greed = green (consistent with the regime's risk-on = green).
function fngColor(v) {
    if (v == null) return "#8a8f98";
    if (v <= 24) return "#e5484d";   // extreme fear
    if (v <= 44) return "#f5a524";   // fear
    if (v <= 55) return "#8a8f98";   // neutral
    if (v <= 74) return "#46a758";   // greed
    return "#30a46c";                // extreme greed
}

const FNG_SOURCE_LABEL = { cnn: "CNN", crypto: "alternative.me", derived: "derived" };

function fearGreedGauge(fng) {
    if (!fng || fng.value == null) return "";
    const v = Math.max(0, Math.min(100, fng.value));
    const color = fngColor(v);
    const srcTag = FNG_SOURCE_LABEL[fng.source];
    const srcTitle = fng.source === "cnn" ? "CNN Fear & Greed Index (unofficial keyless feed)"
        : fng.source === "crypto" ? "alternative.me crypto Fear & Greed feed"
        : fng.source === "derived" ? "Derived market mood — benchmark momentum + inverted volatility percentile. Not an official index."
        : "";
    const src = srcTag ? `<span class="fng-source" title="${esc(srcTitle)}">${esc(srcTag)}</span>` : "";
    const stale = fng.stale ? `<span class="fng-stale muted small" title="Live feed unavailable — showing a neutral 50."> · feed unavailable</span>` : "";
    const facts = (fng.factors || []).length ? `<div class="fng-factors muted small">${esc(fng.factors.join(" · "))}</div>` : "";
    return `<div class="mc-cell fng-gauge">
        <div class="mc-head-row"><span class="mc-label">${esc(fng.label || "Fear & Greed")}</span>${src}</div>
        <div class="fng-value" style="color:${color};">${v}<span class="fng-class">${esc(fng.classification || "")}</span></div>
        <div class="fng-meter"><div class="fng-marker" style="left:${v}%;"></div></div>
        <div class="fng-scale muted small"><span>Fear</span><span>Neutral</span><span>Greed</span></div>
        ${facts}${stale}
    </div>`;
}

function marketDirectionCell(reg) {
    if (!reg) return "";
    const s = reg.state || "neutral";
    const color = s === "risk-on" ? "#30a46c" : s === "risk-off" ? "#e5484d" : "#8a8f98";
    const trend = reg.benchmark && reg.benchmark.trend;
    const arrow = trend === "up" ? "▲" : trend === "down" ? "▼" : "—";
    const tilt = reg.tilt != null ? `tilt ${reg.tilt > 0 ? "+" : ""}${reg.tilt}` : "";
    const benchLine = reg.benchmark && (reg.benchmark.label || reg.benchmark.detail)
        ? `${esc(reg.benchmark.label || "")} ${esc(reg.benchmark.detail || "")}`.trim() : "";
    const vol = reg.volatility
        ? `<span class="pill pill-muted" title="Volatility index level">${esc(reg.volatility.label)} ${reg.volatility.value} (${esc(reg.volatility.level || "")})</span>` : "";
    // Extra factors beyond the benchmark line + vol chip already shown (credit / rates / dollar, etc.).
    const benchLabel = (reg.benchmark && reg.benchmark.label) || "";
    const volLabel = (reg.volatility && reg.volatility.label) || "";
    const extras = (reg.factors || [])
        .filter((f) => (!benchLabel || !String(f).includes(benchLabel)) && (!volLabel || !String(f).includes(volLabel)))
        .slice(0, 3).map((f) => `<span class="pill pill-muted">${esc(f)}</span>`).join(" ");
    const stale = reg.stale ? `<span class="fng-stale muted small"> · live feed unavailable, treated as neutral</span>` : "";
    return `<div class="mc-cell mc-dir">
        <div class="mc-head-row"><span class="mc-label">Market direction</span></div>
        <div class="mc-dir-headline"><span class="mc-arrow" style="color:${color};">${arrow}</span>
            <span class="mc-state" style="color:${color};">${esc(s)}</span>
            <span class="muted small">${esc(tilt)}</span></div>
        ${benchLine ? `<div class="mc-dir-detail muted small">${benchLine}</div>` : ""}
        <div class="mc-factors">${vol} ${extras}</div>${stale}
    </div>`;
}

function marketConditionsSection(reg, fng) {
    const gauge = fearGreedGauge(fng);
    const dir = marketDirectionCell(reg);
    if (!gauge && !dir) return "";   // nothing to show (both gates off / empty payload)
    return `<div class="card market-conditions">
        <div class="mc-grid">${gauge}${dir}</div>
        <div class="mc-foot muted small">The tide the board trades with — regime-aligned longs rank first and risk-off trims position size; Fear &amp; Greed is a mood gauge (context, not a ranking input).</div>
    </div>`;
}

// Composite-ranking disclosure: the soft-factor weights layered on the base conviction×regime score
// (rank = conviction · regime · Π(1 + wᵢ·factorᵢ)), plus the quality-gate floor below which a
// conflicted idea is filtered out. Only rendered when at least one factor is active, so an all-zero
// (pure conviction×regime) config shows nothing — matching the regression-safe design.
function rankingBanner(rk) {
    if (!rk) return "";
    const LABEL = { earnings: "⚠️ earnings", news: "📰 news", sector: "🔄 sector", quality: "💎 quality" };
    const pills = ["earnings", "news", "sector", "quality"]
        .filter((k) => rk[k])   // weight > 0
        .map((k) => `<span class="pill pill-muted" title="Weight of the ${k} factor in the composite multiplier (each factor ∈ [−1,+1]; a supportive factor lifts an idea, a conflicting one trims it).">${LABEL[k]} ×${rk[k]}</span>`)
        .join(" ");
    if (!pills) return "";   // every factor disabled → board is pure conviction×regime, nothing to disclose
    const floor = rk.min_multiplier;
    const gate = floor > 0
        ? `<span class="pill pill-muted" title="Quality gate: an idea whose combined soft factors drag its multiplier below ${floor} — i.e. fighting multiple headwinds — is filtered off the board rather than shown. Higher = stricter; 0 disables it.">🛡️ gate &lt; ${floor}</span>`
        : "";
    return `<div class="card" style="margin-bottom:12px;"><div class="row gap-s" style="flex-wrap:wrap; align-items:center;">
        <b title="Each idea's base conviction×regime score is scaled by Π(1 + weightᵢ·factorᵢ) over these soft factors, then the quality gate drops the conflicted ones. Weights are configurable (settings.rank_w_*).">🎚️ Composite ranking</b>
        ${pills} ${gate}
        <span class="muted small">soft factors layered on conviction — supportive signals lift an idea, conflicting ones trim or filter it.</span>
    </div></div>`;
}

// Confluence-tier disclosure: explains the Actionable/Watchlist split — quality as BREADTH across
// independent families, not just how many technical strategies agree. Hidden when tiering is off.
function pillarsBanner(pl) {
    if (!pl || !pl.enabled) return "";
    return `<div class="card" style="margin-bottom:12px;"><div class="row gap-s" style="flex-wrap:wrap; align-items:center;">
        <b title="Each idea is scored on how many INDEPENDENT signal families confirm it — the technical setup itself plus fundamental quality, institutional accumulation, a regime tailwind, bullish news, and sector leadership. Breadth beats depth: one family firing loudly is a watch, several agreeing is a call to act.">🧭 Confluence tiers</b>
        <span class="pill pill-muted" title="An idea is Actionable when at least ${pl.min_actionable} independent families confirm it (clamped to those available in this market); otherwise it stays on the per-column Watchlist.">★ ≥ ${pl.min_actionable} families → actionable</span>
        <span class="muted small">Fewer, higher-conviction trades up top; thinner setups stay on watch until more signals line up.</span>
    </div></div>`;
}

// Calibration banner: how well advertised win-rates match realized outcomes (isotonic), plus the
// realized-win-rate-by-conviction table. Advertised rates are adjusted to realized before sizing.
// Crypto-only risk disclosure — its strategies backtest with deep drawdowns, so say so plainly.
function cryptoRiskBanner(note) {
    if (!note) return "";
    return `<div class="card" style="margin-bottom:12px; border-left:3px solid var(--warn);">
        <div class="row gap-s" style="align-items:flex-start;">
            <b title="Crypto ideas are generated from equity strategies; on crypto they show large backtested drawdowns.">⚠️ Crypto risk</b>
            <span class="muted small">${esc(note)}</span>
        </div></div>`;
}

function calibrationBanner(cal, fusion) {
    const fusionChip = (fusion && fusion.active)
        ? `<span class="pill pill-accent" title="A logistic model over conviction + win-rate + Sharpe beats the base rate out-of-sample (walk-forward). It now feeds sizing ahead of single-feature calibration.">🧠 learned model active${fusion.improvement != null ? ` · −${fusion.improvement} log-loss` : ""}</span>`
        : "";
    if (!cal) return "";
    if (!cal.active) {
        return `<div class="card" style="margin-bottom:12px;"><p class="muted small">📉 <b>Win-rate calibration</b>: ${cal.decided || 0}/${cal.min_samples || 20} closed trades — not enough yet. Ideas use their raw backtest win-rate until the ledger has more history. ${fusionChip}</p></div>`;
    }
    const rel = (cal.reliability_by_conviction || [])
        .map((r) => `<span class="pill pill-muted" title="${r.n} closed trades at conviction ${r.conviction}">${r.conviction}× → ${r.realized_win_pct != null ? r.realized_win_pct + "%" : "—"}</span>`)
        .join(" ");
    const better = cal.brier_cal != null && cal.brier_raw != null && cal.brier_cal <= cal.brier_raw;
    return `<div class="card" style="margin-bottom:12px;">
        <div class="row gap-s" style="flex-wrap:wrap; align-items:center;">
            <b>📈 Win-rate calibration</b>
            <span class="pill ${better ? "pill-buy" : "pill-muted"}" title="Brier score (lower is better) before → after calibration">Brier ${cal.brier_raw}→${cal.brier_cal}</span>
            ${fusionChip}
            <span class="muted small">from ${cal.n || cal.decided} closed trades — advertised win-rates adjusted to realized, then fed into sizing</span>
        </div>
        <div style="margin-top:8px;" class="small">Realized win-rate by conviction: ${rel || '<span class="muted">—</span>'}</div>
    </div>`;
}

// The "signals as of … · priced live …" status line + disclaimer. Split out so a live
// Honesty disclosure: the realistic round-trip cost the backtest was priced at, the crypto short
// funding drag, and the survivorship haircut applied to advertised win-rates — so the numbers read as
// deliberately conservative, not optimistic.
function costDisclosure(cd) {
    if (!cd) return "";
    const bits = [];
    if (cd.cost_bps != null) bits.push(`priced ${fmt(cd.cost_bps, 0)}bps`);
    if (cd.funding_bps_per_day) bits.push(`shorts +${fmt(cd.funding_bps_per_day, 0)}bps/day funding`);
    if (cd.survivorship_haircut_pp) bits.push(`win rates −${fmt(cd.survivorship_haircut_pp, 0)}pp (survivorship)`);
    if (!bits.length) return "";
    return ` <span class="idea-gate small" title="Costs & honesty adjustments applied to the board: realistic per-market round-trip cost, per-day funding/borrow on crypto shorts, and a documented survivorship haircut on advertised win-rates (the backtest universe is today's index members over history). Deliberately conservative.">⚖️ ${esc(bits.join(" · "))}</span>`;
}

// re-price can refresh the timestamp on its own, without rebuilding the board.
function updateIdeasRunInfo() {
    if (IDEAS_DATA.run_at) {
        const sigAt = IDEAS_DATA.signals_refreshed_at ? new Date(IDEAS_DATA.signals_refreshed_at).toLocaleTimeString() : null;
        const liveAt = IDEAS_DATA.live_priced_at ? new Date(IDEAS_DATA.live_priced_at).toLocaleTimeString() : null;
        const freshIso = IDEAS_DATA.live_priced_at || IDEAS_DATA.signals_refreshed_at || IDEAS_DATA.signals_as_of || IDEAS_DATA.as_of;
        $("#ideas-run-info").innerHTML =
            esc(`Signals as of ${IDEAS_DATA.signals_as_of ?? IDEAS_DATA.as_of ?? "?"}${sigAt ? ` (refreshed ${sigAt})` : ""} · `
                + `${IDEAS_DATA.index}, ${IDEAS_DATA.years}y backtest · ranked by conviction`
                + (liveAt ? ` · priced live ${liveAt}` : ""))
            + " " + freshnessBadge(freshIso, "prices")
            + costDisclosure(IDEAS_DATA.cost_disclosure);
    }
    $("#ideas-disclaimer").textContent = IDEAS_DATA.disclaimer || "";
}

function renderHorizonColumn(b) {
    if (!b) return "";
    const accent = HORIZON_ACCENT[b.horizon] || "var(--accent)";
    const cands = b.candidates || [];
    // Crypto trades both ways: split the column into a LONG section (as before) and, when present, a
    // bearish SHORT subgroup below it. Non-crypto markets only ever have longs → identical to before.
    const longs = cands.filter((c) => (c.side || "long") !== "short");
    const shorts = cands.filter((c) => c.side === "short");
    // Multi-pillar tiering: actionable ideas (clear the confluence bar) render as full cards; the thinner
    // watchlist collapses into a disclosure. Backend orders candidates actionable-first. Tiering off (no
    // `tier` field) → everything is "actionable" and the column renders exactly as before.
    const tiered = longs.some((c) => c.tier);
    const L = renderIdeaSection(longs, accent, b.horizon,
        tiered ? "No high-confluence ideas here right now — see the watchlist below."
               : "No current buy signals in this horizon.");
    const actionable = L.actionable, watch = L.watch, rows = L.rows, watchBlock = L.watchBlock;
    // Bearish setups get their own numbering, a red accent, and a labelled subgroup (crypto only).
    const S = shorts.length ? renderIdeaSection(shorts, "var(--neg)", b.horizon,
        "No high-confluence shorts right now — see the watchlist below.") : null;
    // Note when the shorts are speculative (surfaced by a bearish regime, no proven backtest edge).
    const specShorts = shorts.filter((c) => c.speculative).length;
    const shortsNote = specShorts
        ? ` <span class="pill pill-warn" title="These shorts appear only because the market tide is bearish. No crypto short strategy clears the backtest risk gate (they lose over a 10-year sample), so they're shown as lower-conviction 'potential' — ranked by bearish agreement, never sized as a proven edge.">⚠ ${specShorts} speculative</span>`
        : "";
    const shortsBlock = S
        ? `<div class="ideas-shorts"><div class="ideas-shorts-head small" title="Bearish setups — profit if the price falls (stop above entry, target below). Crypto trades both ways.">↓ Shorts <b>${shorts.length}</b>${shortsNote}</div><div class="ideas-list">${S.rows}</div>${S.watchBlock}</div>`
        : "";
    const shown = longs.length;
    const moreNote = b.total_candidates > cands.length ? `<span class="muted small"> · ${b.total_candidates} total</span>` : "";
    // Quality gate: candidates whose soft factors (bearish news + lagging sector, earnings risk, …)
    // dragged them below the min-multiplier floor were filtered out rather than padded onto the board.
    const cut = b.quality_cut || 0;
    const gateNote = cut > 0
        ? `<span class="idea-gate small" title="Quality gate: ${cut} candidate${cut === 1 ? "" : "s"} fighting multiple headwinds (e.g. bearish news + a lagging sector, or near-term earnings risk) ${cut === 1 ? "was" : "were"} filtered out rather than shown — fewer, cleaner setups instead of a padded board.">🛡️ ${cut} filtered</span>`
        : "";
    // Edge floor: candidates whose (calibrated) win probability couldn't clear their own risk/reward
    // breakeven were dropped — the coin-flips the closed ledger showed realizing ~32% win.
    const edgeCut = b.edge_cut || 0;
    const edgeNote = edgeCut > 0
        ? `<span class="idea-gate small" title="Edge floor: ${edgeCut} candidate${edgeCut === 1 ? "" : "s"} whose win probability sat below its own risk/reward breakeven ${edgeCut === 1 ? "was" : "were"} dropped — the closed ledger showed such sub-breakeven setups realizing ~32% win (net-negative after costs).">🎯 ${edgeCut} below edge</span>`
        : "";
    // Correlation de-dup: ideas that move in lockstep with a higher-ranked pick were collapsed, so the
    // shown names are independent bets — not one crowded trade wearing several tickers.
    const collapsed = (b.dedup && b.dedup.dropped) || 0;
    const dedupNote = collapsed > 0
        ? `<span class="idea-gate small" title="Correlation de-dup: ${collapsed} idea${collapsed === 1 ? "" : "s"} that move${collapsed === 1 ? "s" : ""} in lockstep with a higher-ranked pick ${collapsed === 1 ? "was" : "were"} collapsed — the shown ideas are independent setups, not the same bet under multiple tickers.">🔗 ${collapsed} collapsed</span>`
        : "";
    const notes = [gateNote, edgeNote, dedupNote].filter(Boolean).join(" · ");
    // Short note: crypto also has a bearish subgroup below — surface its count so "0 actionable" longs
    // don't read as "nothing here" when shorts are the regime-aligned trade.
    const shortNote = shorts.length ? ` · <b style="color:var(--neg)">${shorts.length}</b> short${shorts.length === 1 ? "" : "s"} ↓` : "";
    // Count line: when tiered, lead with the actionable count (the "take these" set) and note the watch tail.
    const countLine = tiered
        ? `<b style="color:${accent}">${actionable.length}</b> actionable${watch.length ? ` · <b>${watch.length}</b> on watch` : ""}${shortNote}${moreNote} · from ${b.strategy_count} strategies${notes ? " · " + notes : ""}`
        : `<b style="color:${accent}">${shown}</b> ${shown === 1 ? "idea" : "ideas"}${shortNote}${moreNote} · from ${b.strategy_count} strategies${notes ? " · " + notes : ""}`;
    return `
    <div class="ideas-col card" data-horizon="${esc(b.horizon)}" style="border-top: 3px solid ${accent};">
        <div class="ideas-col-head">
            <h3>${HORIZON_ICON[b.horizon] || ""} ${esc(b.label)}</h3>
            <span class="muted small">${esc(b.sub)}</span>
        </div>
        <p class="muted small ideas-col-detail">${esc(b.detail)}</p>
        <p class="small">${countLine}</p>
        <div class="ideas-list">${rows}</div>
        ${watchBlock}
        ${shortsBlock}
    </div>`;
}

// Split a candidate list into actionable rows + a collapsed watchlist — shared by the LONG section
// and the crypto SHORT subgroup of a horizon column. `accent` colours the row numbers.
function renderIdeaSection(cands, accent, horizon, emptyMsg) {
    const tiered = cands.some((c) => c.tier);
    const actionable = tiered ? cands.filter((c) => c.tier === "actionable") : cands;
    const watch = tiered ? cands.filter((c) => c.tier === "watch") : [];
    const rows = actionable.length
        ? actionable.map((c, i) => renderIdeaRow(c, i, accent, horizon)).join("")
        : `<p class="muted small" style="padding:10px 12px;">${emptyMsg}</p>`;
    const watchBlock = watch.length
        ? `<details class="ideas-watchlist"><summary class="small" title="Lower-confluence ideas — only the technical setup (or one confirming family) fired. Kept for context, not surfaced as a call to act.">Watchlist <b>${watch.length}</b> · fewer confirming signals</summary><div class="ideas-list">${watch.map((c, i) => renderIdeaRow(c, actionable.length + i, accent, horizon)).join("")}</div></details>`
        : "";
    return { actionable, watch, rows, watchBlock };
}

const ZONE_LABEL = {
    in_zone: "✓ in buy zone", below_zone: "↓ below zone", above_zone: "↑ extended",
    at_target: "🎯 at target", below_stop: "⛔ below stop",
};

// Top line of a row: ticker + badges + the live quote + buy-zone pill. Split out because
// these are the only cells a live re-price changes — patchIdeaRow swaps just this in place.
function ideaTopInner(c) {
    const rr = c.risk_reward != null
        ? `<span class="idea-rr mono" title="Reward-to-risk: upside ÷ downside">${fmt(c.risk_reward, 2)}:1</span>`
        : "";

    // Direction: crypto trades both ways. A SHORT badge marks bearish ideas (profit if price falls);
    // longs need no badge (the default). Only crypto ever produces shorts.
    const sideBadge = c.side === "short"
        ? `<span class="pill pill-sell idea-side-pill" title="Short idea — profit if the price falls (stop above entry, target below)">SHORT</span>`
        : "";
    // Speculative: a short surfaced only because the market tide is bearish — no backing strategy clears
    // the backtest risk gate, so it's shown as potential, never a proven edge. Always watchlist-tier.
    const specBadge = c.speculative
        ? `<span class="pill pill-warn idea-spec-pill" title="Speculative — surfaced because the market is bearish, but no strategy backing it clears the backtest risk gate (crypto shorts have no proven edge over 10y). Treat as a lower-conviction 'potential' idea, not a validated signal.">⚠ speculative</span>`
        : "";
    // Sell-side flag: the smart money is DISTRIBUTING (trimming / net-short) this name. On a LONG it's an
    // AVOID warning (argues against the idea — already lowers its rank); on a crypto SHORT it's a CONFIRMING
    // tailwind (the pros are selling too). Surfaces what is otherwise a silent rank multiplier.
    const smd = c.smart_money_distributing;
    const distBadge = smd
        ? (smd.stance === "confirm"
            ? `<span class="pill pill-sell idea-dist-pill" title="${esc(smd.detail || smd.label)} — the smart money is net-short/selling this too, confirming the short (already lifts its rank). Point-in-time, not part of the backtest.">${esc(smd.label)} ✓</span>`
            : `<span class="pill pill-warn idea-dist-pill" title="${esc(smd.detail || smd.label)} — the smart money is trimming/net-short this name, which argues AGAINST a long. This already lowers the idea's rank; shown so the sell signal isn't silent. Point-in-time, not part of the backtest.">⚠ ${esc(smd.label)}</span>`)
        : "";

    // Freshly generated ideas get a NEW tag; carried-over (still-open) ideas get a HELD tag.
    const newBadge = c.is_new ? `<span class="idea-new" title="Freshly generated in the latest run">NEW</span>` : "";
    const heldBadge = c.carried ? `<span class="idea-held" title="Open idea carried over — stays on the board until it hits its target, stop, or time-stop">held</span>` : "";
    // Soft-demote: a cohort the closed ledger shows persistently under-performing (crypto daily,
    // medium/long horizons) — win-rate haircut + smaller size, kept but flagged so the odds read honestly.
    const demoteBadge = c.demote_reason
        ? `<span class="idea-held" title="Demoted cohort (${esc(c.demote_reason)}): the closed ledger shows this cohort under-performing its advertised odds, so its win-rate is haircut and it's sized at ×${c.cohort_size_factor != null ? c.cohort_size_factor : 0.5}. Kept on the board, but the odds are marked down.">demoted</span>`
        : "";
    // Edge floor: a below-breakeven idea that survived only because its bucket would otherwise be empty.
    const belowEdgeBadge = c.below_edge
        ? `<span class="idea-held" title="Below edge: this idea's win probability sits under its own risk/reward breakeven. It shows only because the bucket would otherwise be empty — treat with caution.">below edge</span>`
        : "";
    const age = c.age_days != null
        ? `<span class="idea-age muted small" title="Days since this setup was generated${c.latest_signal_date ? ` (signal ${esc(c.latest_signal_date)})` : ""}">${c.age_days === 0 ? "today" : c.age_days + "d ago"}</span>`
        : "";

    // Live price: show the fresh quote (with % move since the signal) + a buy-zone status pill.
    const hasLive = c.live_price != null;
    const nowPrice = hasLive ? c.live_price : c.price;
    const chg = c.live_change_pct;
    const chgTxt = chg != null ? ` <span class="${chg >= 0 ? "pos" : "neg"}">${chg >= 0 ? "+" : ""}${fmt(chg)}%</span>` : "";
    const nowCell = `<span class="idea-now mono small" title="${hasLive ? "Live price" : "Backtest close"}${c.latest_signal_date ? ` · signal ${esc(c.latest_signal_date)}` : ""}">${hasLive ? "live" : "now ~"} ${priceFmt(nowPrice)}${chgTxt}</span>`;
    const zonePill = c.zone ? `<span class="zone-pill zone-${esc(c.zone)}" title="Live price vs the generated buy zone">${ZONE_LABEL[c.zone] || c.zone}</span>` : "";

    // Earnings event risk: a ⚠️ flag when a report is near — the idea is also softly de-ranked as
    // the date approaches (idea_ranking.earnings_factor), so a breakout right before a binary print
    // doesn't sit at the top of the board.
    const dte = c.days_to_earnings;
    const earn = (dte != null && dte >= 0 && dte <= 10)
        ? `<span class="idea-er" title="Earnings report in ${dte} day${dte === 1 ? "" : "s"} — binary event risk. This idea is de-prioritised on the board as the date nears.">⚠️ ER ${dte}d</span>`
        : "";

    // News sentiment: a live headline read that softly re-ranks the idea (bullish lifts, bearish
    // trims). Only surfaced when it leans one way. Live-only — not part of the backtest.
    const nwc = c.news_consensus;
    const news = (nwc && nwc !== "Neutral" && c.news_score != null)
        ? `<span class="idea-news ${nwc === "Bullish" ? "pos" : "neg"}" title="News sentiment: ${esc(nwc)} (${fmt(c.news_score, 0)}/100) — live headlines, softly re-ranks this idea; not part of the backtest.">${nwc === "Bullish" ? "📈" : "📉"} ${esc(nwc)}</span>`
        : "";

    // Sector rotation: the idea's sector leading (🔺) or lagging (🔻) the benchmark over 3 months.
    // Only shown when it leans meaningfully. Backtestable via sector-ETF history.
    const srs = c.sector_rs;
    const secChip = (srs != null && Math.abs(srs) >= 3 && c.sector)
        ? `<span class="idea-sector ${srs >= 0 ? "pos" : "neg"}" title="${esc(c.sector)} is ${srs >= 0 ? "leading" : "lagging"} the benchmark by ${fmt(Math.abs(srs), 1)}% over 3 months — sector rotation ${srs >= 0 ? "lifts" : "trims"} this idea.">${srs >= 0 ? "🔺" : "🔻"} ${esc(c.sector.split(" ")[0])}</span>`
        : "";

    // Institutional ownership: a point-in-time confirmation that rides on top of the
    // backtested signal. US names prefer real SEC 13F data (carries a quarter-over-quarter
    // change); everything else uses the yfinance snapshot. Never part of the backtest itself.
    const io = c.inst_ownership;
    let inst = "";
    if (io && io.source === "edgar-13f" && (io.qoq_change_pct != null || io.n_managers != null)) {
        const q = io.qoq_change_pct;
        const delta = q == null ? "" : ` <span class="${q >= 0 ? "pos" : "neg"}">${q >= 0 ? "▲+" : "▼"}${fmt(Math.abs(q), 1)}%</span>`;
        const pct = io.held_pct_institutions != null ? `${fmt(io.held_pct_institutions, 1)}% ` : "";
        const mgrs = io.n_managers != null ? ` · ${io.n_managers} mgr${io.n_managers === 1 ? "" : "s"}` : "";
        const title = `SEC 13F — a basket of ${io.n_managers || "major"} large institutional managers`
            + (io.held_pct_institutions != null ? ` hold ${fmt(io.held_pct_institutions, 1)}% of shares outstanding` : "")
            + (q != null ? `; ${q >= 0 ? "added" : "trimmed"} ${fmt(Math.abs(q), 1)}% vs the prior quarter` : "")
            + ` (as of ${io.quarter || "latest filing"}). Quarterly filings, ~45-day lag — point-in-time confirmation, distinct from the backtested signal.`;
        inst = `<span class="idea-inst" title="${esc(title)}">🏦 13F ${pct}${delta}${mgrs}</span>`;
    } else if (io && io.held_pct_institutions != null) {
        inst = `<span class="idea-inst" title="Institutions currently hold ${fmt(io.held_pct_institutions, 1)}% of shares${io.held_pct_insiders != null ? ` · insiders ${fmt(io.held_pct_insiders, 1)}%` : ""}. Live snapshot (yfinance) — point-in-time, not part of the backtest.">🏦 ${fmt(io.held_pct_institutions, 1)}%</span>`;
    } else if (c.smart_money && c.smart_money.family && c.smart_money.family !== "ownership") {
        // Market-native smart money (metals COT commercials; crypto flows/whales; India bulk deals).
        // Direction colours the chip; the source's own `detail` explains it. Not part of the backtest.
        const sm = c.smart_money, fam = sm.family, dir = sm.direction;
        const icon = { cot: "🏦", flow: "🐋", whale: "🐋", bulk_deal: "🇮🇳", ace: "🇮🇳", congress: "🏛" }[fam] || "🧠";
        const cls = dir === "accumulate" ? "pos" : dir === "distribute" ? "neg" : "";
        const arrow = dir === "accumulate" ? " ▲" : dir === "distribute" ? " ▼" : "";
        const title = `${sm.detail || (sm.label || "Smart money") + " — " + (dir || "neutral")}. Live smart-money confirmation — point-in-time, distinct from the backtested signal.`;
        inst = `<span class="idea-inst ${cls}" title="${esc(title)}">${icon} ${esc(sm.label || "smart")}${arrow}</span>`;
    }

    return `
                <span class="idea-ticker mono b">${esc(c.ticker)}</span>
                ${sideBadge}
                ${specBadge}
                ${distBadge}
                ${pillarChip(c)}
                ${newBadge}
                ${heldBadge}
                ${demoteBadge}
                ${belowEdgeBadge}
                ${earn}
                ${news}
                ${secChip}
                ${age}
                ${zonePill}
                ${nowCell}
                ${rr}
                ${inst}`;
}

// "Live levels": the same plan recomputed from the live price (enter-from-here). Present
// only with a live quote; also re-priced in place, so it lives in its own stable slot.
function ideaLiveLevelsHtml(c) {
    const ll = c.live_levels;
    if (!ll || c.live_price == null) return "";
    return `
            <details class="idea-live">
                <summary class="small muted">Live levels — enter from ${priceFmt(c.live_price)}</summary>
                <div class="idea-levels idea-levels-live">
                    <div class="idea-lvl"><span class="lvl-label">${c.side === "short" ? "Sell" : "Buy"}</span><span class="lvl-val mono">${ll.buy_low != null ? `${priceFmt(ll.buy_low)} – ${priceFmt(ll.buy_high)}` : "—"}</span></div>
                    <div class="idea-lvl"><span class="lvl-label">Target</span><span class="lvl-val mono pos">${ll.target_price != null ? priceFmt(ll.target_price) : "—"}</span></div>
                    <div class="idea-lvl"><span class="lvl-label">Stop</span><span class="lvl-val mono neg">${ll.stop_price != null ? priceFmt(ll.stop_price) : "—"}</span></div>
                    ${ll.risk_reward != null ? `<div class="idea-lvl"><span class="lvl-label">R:R</span><span class="lvl-val mono">${fmt(ll.risk_reward, 2)}:1</span></div>` : ""}
                </div>
            </details>`;
}

// Multi-pillar confluence: a compact ★N chip summarising how many INDEPENDENT families confirm this
// idea (technical + fundamental quality + institutional accumulation + regime tailwind + news + sector).
// The individual family chips (📈 news, 🔺 sector, 🏦 13F, 🟢 regime) already appear on the card — this
// is the at-a-glance breadth score that drives the actionable/watch split. Absent when tiering is off.
const PILLAR_LABEL = {
    technical: "📊 technical setup", quality: "💎 fundamental quality", smart_money: "🏦 smart-money buying",
    regime: "🟢 regime tailwind", sentiment: "📰 bullish news", sector: "🔄 sector leadership",
};
function pillarChip(c) {
    if (!c.pillars || c.pillar_count == null) return "";
    const supporting = Object.keys(c.pillars).filter((k) => c.pillars[k] === true);
    const list = supporting.map((k) => PILLAR_LABEL[k] || k).join(" · ") || "technical only";
    const actionable = c.tier === "actionable";
    const cls = actionable ? "pillar-chip pillar-actionable" : "pillar-chip pillar-watch";
    return `<span class="${cls}" title="Confluence — ${c.pillar_count} independent famil${c.pillar_count === 1 ? "y" : "ies"} confirm this idea: ${list}.${actionable ? " Clears the actionable bar." : " Below the bar → watchlist."}">★ ${c.pillar_count}</span>`;
}

// "Normal drawdown" band: how far a WINNING backtest trade of the backing strategies typically dipped
// before it worked out — hold-through guidance so a routine wobble doesn't shake you out. NOT a stop.
// Shown only when the backtest populated it (a fresh run after this feature shipped).
function drawdownLine(c) {
    if (c.typical_drawdown_pct == null) return "";
    const deep = c.deep_drawdown_pct != null ? ` · unusual past ~${fmt(c.deep_drawdown_pct, 0)}%` : "";
    return `<div class="idea-dd small" title="Across the backing strategies' WINNING backtest trades, the typical worst dip before the trade worked out was ~${fmt(c.typical_drawdown_pct, 1)}%${c.deep_drawdown_pct != null ? `; 4 in 5 winners stayed within ~${fmt(c.deep_drawdown_pct, 1)}%` : ""}. Hold-through context — a normal wobble, not your stop.">📉 usually dips ~${fmt(c.typical_drawdown_pct, 0)}% before working${deep}</div>`;
}

function renderIdeaRow(c, i, accent, horizon) {
    const pills = (c.strategies || []).slice(0, 3).map((s) =>
        `<span class="idea-strat" title="${esc(s.name)} · win rate ${fmt(s.win_rate)}%">${esc(s.name)}</span>`).join("");
    const more = c.strategies.length > 3
        ? `<span class="idea-strat idea-strat-more" title="${esc(c.strategies.slice(3).map((s) => s.name).join(", "))}">+${c.strategies.length - 3}</span>`
        : "";
    // How this idea sits vs the market regime (only when there's a tide): a tailwind long (e.g. a
    // dip-buy in risk-off) rises, a headwind long (e.g. a breakout in risk-off) is de-emphasised
    // and its size trimmed.
    const trimmed = c.regime_size_factor != null && c.regime_size_factor < 1;
    const regimeTag = c.regime_fit && c.regime_fit !== "neutral"
        ? `<span class="idea-strat" title="This idea vs the market regime${trimmed ? ` — size trimmed to ×${c.regime_size_factor} in risk-off` : ""}">${c.regime_fit === "tailwind" ? "🟢 tailwind" : "🔴 headwind"}${trimmed ? ` ×${c.regime_size_factor}` : ""}</span>`
        : "";

    const isShort = c.side === "short";
    const stopPct = c.stop_pct != null ? fmt(c.stop_pct * 100, 1) : null;
    const tgtPct = c.target_pct != null ? fmt(c.target_pct * 100, 1) : null;
    const stopTitle = c.stop_source === "strategy"
        ? "Stop from the flagging strategies' own risk rules"
        : "Stop = horizon default (no strategy-defined stop)";
    const tgtTitle = c.target_source === "strategy"
        ? `${isShort ? "Downside" : "Upside"} = average fixed target of the flagging strategies`
        : `${isShort ? "Downside" : "Upside"} = horizon default (trend-followers ride beyond a fixed target)`;
    // A short's entry zone is a "sell to open" band; a long's is an accumulation band.
    const entryLabel = isShort ? "Sell" : "Buy";

    // Entry zone: prefer the explicit band, fall back to the last close.
    const buy = (c.buy_low != null && c.buy_high != null)
        ? `${priceFmt(c.buy_low)} – ${priceFmt(c.buy_high)}`
        : (c.price != null ? `~${priceFmt(c.price)}` : "—");
    const target = c.target_price != null
        ? `${priceFmt(c.target_price)}${tgtPct ? ` · +${tgtPct}%` : ""}`
        : "—";
    const stop = c.stop_price != null
        ? `${priceFmt(c.stop_price)}${stopPct ? ` · −${stopPct}%` : ""}`
        : "—";

    // Paper-trade plan: fill at the live price with the live-recomputed levels when we have a
    // quote (matches the card's "Live levels — enter from here"), else the generated plan anchored
    // to the signal. Most daily ideas are long; crypto also produces shorts (data-side drives the
    // paper account's P&L sign — the backend open_idea already handles both).
    const pLive = c.live_price != null && c.live_levels;
    const pPrice = c.live_price != null ? c.live_price : c.price;
    const pStop = pLive ? c.live_levels.stop_price : c.stop_price;
    const pTgt = pLive ? c.live_levels.target_price : c.target_price;
    const paperBtn = pPrice != null
        ? `<button class="idea-paper-btn paper-btn" data-ticker="${esc(c.ticker)}" data-market="${esc(IDEAS_DATA.market || MARKET.key)}" data-side="${esc(c.side || "long")}" data-price="${pPrice}" data-stop="${pStop != null ? pStop : ""}" data-target="${pTgt != null ? pTgt : ""}" data-alloc="${c.alloc_pct != null ? c.alloc_pct : ""}" data-source="idea-${esc(horizon || "")}" title="Paper trade ${esc(c.ticker)} ${isShort ? "short" : "long"} at ${priceFmt(pPrice)}${pStop != null ? ` · stop ${priceFmt(pStop)}` : ""}${pTgt != null ? ` · target ${priceFmt(pTgt)}` : ""}">📝 Paper</button>`
        : "";

    return `
    <div class="idea-wrap">
    <div class="idea-row" data-ticker="${esc(c.ticker)}" title="Click to deep-dive ${esc(c.ticker)} in Analyze Stock">
        <div class="idea-rank">${i + 1}</div>
        <div class="idea-main">
            <div class="idea-top">${ideaTopInner(c)}</div>
            <div class="idea-levels" title="The trade plan as generated (anchored to the signal price)">
                <div class="idea-lvl"><span class="lvl-label">${entryLabel}</span><span class="lvl-val mono">${buy}</span></div>
                <div class="idea-lvl" title="${tgtTitle}"><span class="lvl-label">Target</span><span class="lvl-val mono pos">${target}</span></div>
                <div class="idea-lvl" title="${stopTitle}"><span class="lvl-label">Stop</span><span class="lvl-val mono neg">${stop}</span></div>
                ${c.risk_pct != null && c.alloc_pct != null ? `<div class="idea-lvl" title="Risk ${c.risk_pct}% of equity if stopped → ${c.alloc_pct}% position (${esc(c.size_note || "")}, ¼-Kelly capped)"><span class="lvl-label">Size</span><span class="lvl-val mono"><b>${c.risk_pct}%</b> → ${c.alloc_pct}%</span></div>` : ""}
            </div>
            ${drawdownLine(c)}
            <div class="idea-live-slot">${ideaLiveLevelsHtml(c)}</div>
            <div class="idea-strats">${pills}${more}${regimeTag}</div>
        </div>
        <div class="idea-conv" title="${c.conviction} strateg${c.conviction === 1 ? "y" : "ies"} agree · avg win rate ${fmt(c.avg_win_rate)}%${c.calibrated_win_pct != null ? ` · calibrated to ${fmt(c.calibrated_win_pct)}%` : ""}${c.weighted_conviction != null && c.weighted_conviction < c.conviction - 0.05 ? ` · edge-weighted to ${fmt(c.weighted_conviction, 1)}× for ranking (each vote scaled by its backtest win-rate, so low-win-rate momentum signals don't inflate the count)` : ""}">
            <span class="conv-num" style="color:${accent}">${c.conviction}×</span>
            <span class="muted small" title="${c.calibrated_win_pct != null ? `Calibrated to realized outcomes (advertised ${fmt(c.avg_win_rate)}%)` : "Average backtest win rate"}">${fmt(c.calibrated_win_pct ?? c.avg_win_rate)}% win${c.calibrated_win_pct != null ? " ✓" : ""}</span>
            <button class="idea-ai-btn" data-ticker="${esc(c.ticker)}" data-horizon="${esc(horizon || "")}" title="AI analyst read of this idea (thesis + news sentiment + verdict)">🧠 AI</button>
            ${paperBtn}
        </div>
    </div>
    <div class="idea-ai-panel hidden"></div>
    </div>`;
}

function analyzeTicker(ticker) {
    if (!ticker) return;
    $("#a-ticker").value = ticker;
    navigate("analyze", { ticker });   // reflects the ticker in the URL (deep-linkable)
    runAnalyze();
}

$("#ideas-topn").addEventListener("change", loadTradeIdeas);
$("#btn-refresh-ideas").addEventListener("click", loadTradeIdeas);

// ── Multi-Bagger screen (fundamentals-first sub-tab of Trade Ideas) ──

const MB_PILLARS = [
    ["growth", "Growth"], ["quality", "Quality"], ["balance", "Balance sheet"],
    ["value", "Valuation"], ["size", "Size / runway"], ["ownership", "Ownership"],
];

// Segmented toggle between the horizon board and the multi-bagger screen.
document.querySelectorAll(".ideas-seg button").forEach((btn) =>
    btn.addEventListener("click", () => setIdeasView(btn.dataset.ideasView)));

function setIdeasView(view) {
    document.querySelectorAll(".ideas-seg button").forEach((b) =>
        b.classList.toggle("active", b.dataset.ideasView === view));
    $("#ideas-horizon-view").classList.toggle("hidden", view !== "horizon");
    $("#ideas-multibagger-view").classList.toggle("hidden", view !== "multibagger");
    if (view === "multibagger" && !MB_LOADED) { loadMultibaggers(); loadMbTracker(); }
}

// Colour a 0–100 score: strong → positive, mid → brand, weak → muted.
function mbScoreColor(s) {
    if (s == null) return "var(--border)";
    if (s >= 75) return "var(--pos)";
    if (s >= 55) return "var(--brand-2)";
    if (s >= 40) return "var(--warn)";
    return "var(--text-dim)";
}

async function loadMultibaggers() {
    const box = $("#mb-list");
    if (!marketHasFundamentals()) {   // price-only markets (metals/crypto) have no fundamentals
        MB_LOADED = true;
        box.innerHTML = `<div class="card"><p class="muted">The multi-bagger screen is a <b>fundamentals</b> screen — available for equity markets. Switch to <b>India</b> or <b>US</b> from the market selector to use it.</p></div>`;
        $("#mb-disclaimer").textContent = "";
        return;
    }
    const n = $("#mb-topn") ? $("#mb-topn").value : 20;
    box.innerHTML = `<p class="muted">Loading multi-bagger candidates…</p>`;
    try {
        MB_DATA = await api(withMarket(`/api/v1/backtest/multibaggers?top_n=${n}`));
        MB_LOADED = true;
        renderMultibaggers();
    } catch (e) {
        box.innerHTML = `<p class="muted">Could not load multi-bagger candidates: ${esc(e.message)}</p>`;
    }
}

function renderMultibaggers() {
    const box = $("#mb-list");
    const cands = (MB_DATA && MB_DATA.candidates) || [];
    if (MB_DATA && MB_DATA.note && !cands.length) {
        box.innerHTML = `<div class="card"><p class="muted">${esc(MB_DATA.note)}</p></div>`;
        $("#mb-disclaimer").textContent = "";
        return;
    }
    if (!cands.length) {
        box.innerHTML = `<div class="card"><p class="muted">No multi-bagger screen yet for <b>${esc(mkt().label || MARKET.key)}</b> — hit <b>⟳ Refresh screen</b> to run it. It scores the ${esc((MB_DATA && MB_DATA.universe) || "universe")} on fundamentals, then deep-dives the top names.</p></div>`;
        $("#mb-disclaimer").textContent = "";
        return;
    }
    box.innerHTML = cands.map((c, i) => renderMultibaggerCard(c, i + 1)).join("");
    updateMbRunInfo();
    $("#mb-disclaimer").textContent = MB_DATA.disclaimer || "";

    // Click a card → analyze; buttons/AI-panel clicks are excluded.
    box.querySelectorAll(".mb-card").forEach((card) =>
        card.addEventListener("click", (e) => {
            if (!e.target.closest("button") && !e.target.closest(".mb-ai-panel")) analyzeTicker(card.dataset.ticker);
        }));
    box.querySelectorAll(".mb-ai-btn").forEach((btn) =>
        btn.addEventListener("click", (e) => { e.stopPropagation(); onMbAI(btn); }));
    box.querySelectorAll(".mb-track-btn").forEach((btn) =>
        btn.addEventListener("click", (e) => { e.stopPropagation(); trackMultibagger(btn.dataset.ticker, btn); }));
    box.querySelectorAll(".paper-btn").forEach((btn) =>
        btn.addEventListener("click", (e) => { e.stopPropagation(); paperTradeIdea(btn); }));
}

function updateMbRunInfo() {
    if (!MB_DATA) return;
    const bits = [];
    if (MB_DATA.universe) bits.push(esc(MB_DATA.universe));
    if (MB_DATA.screened) bits.push(`${MB_DATA.screened} screened`);
    if (MB_DATA.run_at) bits.push(new Date(MB_DATA.run_at).toLocaleString());
    $("#mb-run-info").innerHTML = `Fundamentals-first: growth · quality · balance sheet · valuation · size · ownership${bits.length ? " — " + bits.join(" · ") : ""}.`;
}

function renderMultibaggerCard(c, rank) {
    const score = c.score != null ? Math.round(c.score) : 0;
    const col = mbScoreColor(score);
    const mcap = c.market_cap_display != null ? `${fmt(c.market_cap_display, 0)} ${mcapUnit()}` : "";

    const meters = MB_PILLARS.map(([k, label]) => {
        const v = c.pillars ? c.pillars[k] : null;
        const w = v == null ? 0 : Math.max(0, Math.min(100, v));
        return `<div class="mb-meter" title="${esc(label)}: ${v == null ? "no data" : Math.round(v) + " / 100"}">
            <span class="mb-meter-label">${esc(label)}</span>
            <span class="mb-meter-track"><span class="mb-meter-fill" style="width:${w}%; background:${mbScoreColor(v)};"></span></span>
            <span class="mb-meter-val mono">${v == null ? "—" : Math.round(v)}</span>
        </div>`;
    }).join("");

    const reasons = (c.reasons || []).map((r) => `<li class="pos">✅ ${esc(r)}</li>`).join("");
    const concerns = (c.concerns || []).map((r) => `<li class="neg">⚠️ ${esc(r)}</li>`).join("");

    const chips = [];
    if (c.buy_signal_now) chips.push(`<span class="pill pill-buy" title="A strategy is currently signalling a buy — a timing tailwind. The ranking is fundamentals-first; this is context, not a heavy weight.">✓ buy signal now</span>`);
    if (c.pct_in_52w_range != null) {
        const p = Math.round(c.pct_in_52w_range);
        const cls = p <= 40 ? "pill-low" : (p >= 85 ? "pill-high" : "pill-muted");
        const word = p <= 40 ? "early in its range" : (p >= 85 ? "extended near highs" : "mid-range");
        chips.push(`<span class="pill ${cls}" title="Price sits ${p}% up its 52-week range — ${word}.">${p}% of 52w</span>`);
    }
    if (c.deep) chips.push(`<span class="pill pill-accent" title="Deep-dived: scored with financial-statement metrics (Piotroski / ROCE / 3-year CAGRs${MB_DATA && MB_DATA.market === "IN" ? " + promoter holding" : ""}).">deep-dived</span>`);

    const paperBtn = c.price != null
        ? `<button class="paper-btn mb-paper-btn" data-ticker="${esc(c.ticker)}" data-market="${esc((MB_DATA && MB_DATA.market) || MARKET.key)}" data-side="long" data-price="${c.price}" data-source="multibagger" title="Paper trade ${esc(c.ticker)} at ${priceFmt(c.price)}">📝 Paper</button>`
        : "";

    return `
    <div class="card mb-card" data-ticker="${esc(c.ticker)}" title="Analyze ${esc(c.ticker)}">
        <div class="mb-head">
            <div class="mb-rank">#${rank}</div>
            <div class="mb-title">
                <div class="mb-ticker">${esc(c.ticker)}${mcap ? ` <span class="muted small">· ${mcap}</span>` : ""}</div>
                <div class="muted small mb-name">${esc(c.name || "")}${c.sector ? " · " + esc(c.sector) : ""}</div>
            </div>
            <div class="mb-score" style="border-color:${col}; color:${col};">
                <span class="mb-score-num">${score}</span><span class="mb-score-cap">score</span>
            </div>
        </div>
        ${chips.length ? `<div class="mb-chips">${chips.join("")}</div>` : ""}
        <div class="mb-meters">${meters}</div>
        <div class="mb-why">
            ${reasons ? `<ul class="mb-reasons">${reasons}</ul>` : ""}
            ${concerns ? `<ul class="mb-reasons">${concerns}</ul>` : ""}
        </div>
        <div class="mb-actions">
            ${paperBtn}
            <button class="mb-track-btn" data-ticker="${esc(c.ticker)}" title="Track ${esc(c.ticker)} — follow its bagger multiple (current ÷ entry) over time in the tracker above">📌 Track</button>
            <button class="idea-ai-btn mb-ai-btn" data-ticker="${esc(c.ticker)}" title="AI analyst read of this idea (thesis + news sentiment + verdict)">🧠 AI</button>
        </div>
        <div class="mb-ai-panel idea-ai-panel hidden"></div>
    </div>`;
}

// AI read for a multi-bagger card — its own handler (these cards aren't in IDEAS_DATA).
function onMbAI(btn) {
    const card = btn.closest(".mb-card");
    const panel = card.querySelector(".mb-ai-panel");
    if (!panel.classList.contains("hidden")) { panel.classList.add("hidden"); return; }
    panel.classList.remove("hidden");
    if (panel.dataset.loaded === "1") return;
    const idea = ((MB_DATA && MB_DATA.candidates) || []).find((c) => c.ticker === btn.dataset.ticker);
    if (!idea) { panel.innerHTML = `<p class="muted small">Idea not found.</p>`; return; }
    panel.innerHTML = `<p class="muted small">🧠 Analyzing…</p>`;
    btn.disabled = true;
    fetchIdeaAI(idea)
        .then((ai) => { panel.innerHTML = renderAIPanel(ai); panel.dataset.loaded = "1"; })
        .catch((e) => { panel.innerHTML = `<p class="muted small">AI unavailable: ${esc(e.message)}</p>`; })
        .finally(() => { btn.disabled = false; });
}

// Recompute the screen on the server, then poll the job to completion.
async function refreshMultibaggers() {
    if (!marketHasFundamentals()) { toast("Multi-bagger screen is for equity markets (India / US)", true); return; }
    const btn = $("#btn-refresh-mb");
    const n = $("#mb-topn") ? $("#mb-topn").value : 20;
    btn.disabled = true;
    try {
        await api(withMarket(`/api/v1/backtest/multibaggers/refresh?top_n=${n}`), { method: "POST" });
        toast("Multi-bagger screen started — scoring the universe…");
        $("#mb-progress").classList.remove("hidden");
        MB_POLL = setInterval(pollMbStatus, 2000);
        pollMbStatus();
    } catch (e) {
        toast(e.message, true);
        btn.disabled = false;
    }
}

async function pollMbStatus() {
    try {
        const s = await api(withMarket("/api/v1/backtest/multibaggers/status"));
        const job = s.job || {};
        if (job.running) {
            $("#mb-progress-stage").textContent = `${job.stage || "…"}${job.detail ? " · " + job.detail : ""}`;
            $("#mb-progress-fill").style.width = `${job.progress_pct || 0}%`;
            $("#mb-progress-pct").textContent = `${Math.round(job.progress_pct || 0)}%`;
        } else {
            clearInterval(MB_POLL);
            $("#mb-progress-fill").style.width = "100%";
            $("#mb-progress-pct").textContent = "100%";
            $("#mb-progress-stage").textContent = job.error ? `Failed: ${job.error}` : "Completed";
            $("#btn-refresh-mb").disabled = false;
            setTimeout(() => $("#mb-progress").classList.add("hidden"), 1500);
            if (job.error) toast(`Multi-bagger screen failed: ${job.error}`, true);
            else { toast("Multi-bagger screen updated"); await loadMultibaggers(); }
        }
    } catch { /* transient — the next tick retries */ }
}

$("#btn-refresh-mb").addEventListener("click", refreshMultibaggers);
$("#mb-topn").addEventListener("change", () => { if (MB_LOADED) loadMultibaggers(); });

// ── Multi-Bagger Tracker (follow the bagger multiple over time) ──

async function loadMbTracker() {
    const board = $("#mb-scoreboard");
    if (!marketHasFundamentals()) {          // price-only markets have no tracker
        if (board) board.innerHTML = "";
        if ($("#mb-equity-wrap")) $("#mb-equity-wrap").classList.add("hidden");
        if ($("#mb-tracked-list")) $("#mb-tracked-list").innerHTML = "";
        return;
    }
    if (board) board.innerHTML = `<p class="muted small">Pricing tracked picks…</p>`;
    try {
        MB_TRACKED_DATA = await api(withMarket("/api/v1/backtest/multibaggers/tracked"));
        renderMbTracker();
    } catch (e) {
        if (board) board.innerHTML = `<p class="muted small">Could not load tracker: ${esc(e.message)}</p>`;
    }
}

function renderMbTracker() {
    const d = MB_TRACKED_DATA || {};
    const rows = d.tracked || [];
    const sb = d.scoreboard || {};
    const board = $("#mb-scoreboard");
    const eqWrap = $("#mb-equity-wrap");
    const list = $("#mb-tracked-list");

    if (!rows.length) {
        board.innerHTML = `<p class="muted small">No multi-baggers tracked yet. Hit <b>📌 Track</b> on a candidate below (or <b>＋ Track all shown</b>) to follow its bagger multiple over time.</p>`;
        if (eqWrap) eqWrap.classList.add("hidden");
        if (list) list.innerHTML = "";
        return;
    }

    const ms = sb.milestones || {};
    const chip = (val, label, cls) =>
        `<div class="score-chip"><span class="score-chip-val ${cls || ""}">${val}</span><span class="score-chip-lbl">${label}</span></div>`;
    const pct = (v) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${fmt(v)}%`);
    board.innerHTML = `
        <div class="scoreboard">
            ${chip(sb.count || 0, "Tracked", "")}
            ${chip(sb.best_multiple != null ? sb.best_multiple + "×" : "—", "Best multiple", (sb.best_multiple ?? 0) >= 2 ? "pos" : "")}
            ${chip(sb.avg_multiple != null ? sb.avg_multiple + "×" : "—", "Avg multiple", "")}
            ${chip(pct(sb.avg_return_pct), "Avg return", (sb.avg_return_pct ?? 0) >= 0 ? "pos" : "neg")}
            ${chip(pct(sb.worst_return_pct), "Worst", (sb.worst_return_pct ?? 0) >= 0 ? "pos" : "neg")}
            ${chip(ms["2"] || 0, "🎯 Doubled (2×+)", (ms["2"] || 0) > 0 ? "pos" : "")}
            ${chip(ms["5"] || 0, "5×+", (ms["5"] || 0) > 0 ? "pos" : "")}
            ${chip(ms["10"] || 0, "🚀 10×+", (ms["10"] || 0) > 0 ? "pos" : "")}
        </div>
        ${d.priced_at ? `<p class="muted small" style="margin-top:8px;">Priced ${new Date(d.priced_at).toLocaleString()} · multiple = current ÷ entry · thesis drift = fresh deep re-score − entry score. Open-ended holds — no stop/target close.</p>` : ""}`;

    const eq = d.curve || [];
    if (eq.length) { eqWrap.classList.remove("hidden"); drawMbEquity(eq); }
    else eqWrap.classList.add("hidden");

    list.innerHTML = renderMbTrackedTable(rows);
    list.querySelectorAll(".trk-row").forEach((r) =>
        r.addEventListener("click", (e) => { if (!e.target.closest("button")) analyzeTicker(r.dataset.ticker); }));
    list.querySelectorAll(".mb-untrack-btn").forEach((b) =>
        b.addEventListener("click", (e) => { e.stopPropagation(); untrackMultibagger(b.dataset.ticker); }));
}

function renderMbTrackedTable(rows) {
    const body = rows.map((t) => {
        const mult = t.multiple != null ? `${fmt(t.multiple, 2)}×` : "—";
        const multCls = t.multiple == null ? "muted" : (t.multiple >= 1 ? "pos" : "neg");
        const ret = t.return_pct != null ? `${t.return_pct >= 0 ? "+" : ""}${fmt(t.return_pct)}%` : "—";
        const retCls = t.return_pct == null ? "muted" : (t.return_pct >= 0 ? "pos" : "neg");
        const milestone = t.milestone ? `<span class="pill pill-buy mb-mult" title="Bagger milestone reached">${esc(t.milestone)}</span>` : "";
        // Thesis drift: entry score → fresh deep re-score.
        let drift = `<span class="muted">—</span>`;
        if (t.current_score != null) {
            const dd = t.score_drift;
            const arrow = dd == null ? "" : (dd > 0 ? ` <span class="pos">▲${fmt(dd, 0)}</span>` : dd < 0 ? ` <span class="neg">▼${fmt(Math.abs(dd), 0)}</span>` : "");
            drift = `<span title="Entry score → current (fresh deep re-score) — is the thesis strengthening or weakening?">${fmt(t.entry_score, 0)}→${fmt(t.current_score, 0)}${arrow}</span>`;
        } else if (t.entry_score != null) {
            drift = `<span class="muted" title="Couldn't re-score right now">${fmt(t.entry_score, 0)}→?</span>`;
        }
        return `<tr class="trk-row" data-ticker="${esc(t.ticker)}" title="Analyze ${esc(t.ticker)}">
            <td class="mono b">${esc(t.ticker)} ${milestone}</td>
            <td class="small muted">${esc(t.entry_date || "—")}</td>
            <td class="mono">${priceFmt(t.entry_price)}</td>
            <td class="mono">${priceFmt(t.current_price)}</td>
            <td class="mono ${retCls}">${ret}</td>
            <td class="mono b ${multCls}">${mult}</td>
            <td class="small">${drift}</td>
            <td><button class="mb-untrack-btn trk-ai-btn" data-ticker="${esc(t.ticker)}" title="Stop tracking ${esc(t.ticker)}">🗑</button></td>
        </tr>`;
    }).join("");
    return `<table class="lab-table trk-table">
        <thead><tr><th>Ticker</th><th>Entry date</th><th>Entry</th><th>Now</th><th>Return</th><th>Multiple</th><th>Score then→now</th><th></th></tr></thead>
        <tbody>${body}</tbody></table>`;
}

let MB_CHART = null;
function drawMbEquity(points) {
    const canvas = $("#mb-equity-chart");
    if (!canvas || !window.Chart) return;
    if (MB_CHART) { MB_CHART.destroy(); MB_CHART = null; }
    const pts = (points || []).map(([t, v]) => ({ x: t, y: v }));
    const ct = chartTheme();
    MB_CHART = new Chart(canvas, {
        type: "line",
        data: { datasets: [{ label: "Basket return (%)", data: pts, borderColor: ct.brand2, borderWidth: 1.6, pointRadius: 0, fill: true, backgroundColor: hexTint(ct.brand2, 0.1), tension: 0.1 }] },
        options: {
            responsive: true, maintainAspectRatio: false, animation: false,
            interaction: { mode: "index", intersect: false },
            scales: {
                x: { type: "linear", grid: { display: false },
                     ticks: { color: ct.dim, maxTicksLimit: 8, autoSkip: true,
                              callback: (v) => new Date(v).toLocaleDateString(undefined, { month: "short", day: "numeric" }) } },
                y: { grid: { color: ct.grid }, ticks: { color: ct.dim, callback: (v) => v + "%" } },
            },
            plugins: { legend: { display: false },
                tooltip: { callbacks: { title: (items) => (items.length ? new Date(items[0].parsed.x).toLocaleDateString() : "") } } },
        },
    });
}

// Track one candidate from its card.
async function trackMultibagger(ticker, btn) {
    if (btn) btn.disabled = true;
    try {
        const r = await api(withMarket(`/api/v1/backtest/multibaggers/track?ticker=${encodeURIComponent(ticker)}`), { method: "POST" });
        if (r && r.note && !r.added) { toast(r.note, true); if (btn) btn.disabled = false; }
        else { toast(`📌 Tracking ${String(ticker).toUpperCase()}`); if (btn) { btn.textContent = "✓ tracking"; btn.classList.add("paper-done"); } }
        await loadMbTracker();
    } catch (e) {
        toast(e.message, true);
        if (btn) btn.disabled = false;
    }
}

async function untrackMultibagger(ticker) {
    try {
        await api(withMarket(`/api/v1/backtest/multibaggers/tracked?ticker=${encodeURIComponent(ticker)}`), { method: "DELETE" });
        toast(`Stopped tracking ${String(ticker).toUpperCase()}`);
        await loadMbTracker();
    } catch (e) { toast(e.message, true); }
}

$("#btn-mb-track-all").addEventListener("click", async () => {
    const btn = $("#btn-mb-track-all");
    const n = $("#mb-topn") ? $("#mb-topn").value : 20;
    btn.disabled = true;
    try {
        const r = await api(withMarket(`/api/v1/backtest/multibaggers/track?top_n=${n}`), { method: "POST" });
        if (r && r.note && !r.added) toast(r.note, true);
        else toast(`Tracking ${r.added} new pick${r.added === 1 ? "" : "s"} · ${r.total} total`);
        await loadMbTracker();
    } catch (e) { toast(e.message, true); }
    finally { btn.disabled = false; }
});

$("#btn-mb-refresh-tracked").addEventListener("click", loadMbTracker);

$("#btn-mb-reset-tracked").addEventListener("click", async () => {
    if (!confirm("Forget all tracked multi-baggers?")) return;
    try {
        await api(withMarket("/api/v1/backtest/multibaggers/tracked"), { method: "DELETE" });
        toast("Multi-bagger tracker reset");
        await loadMbTracker();
    } catch (e) { toast(e.message, true); }
});

// ── Fast idea generation + auto-refresh ──────────────────────────

// Force an immediate fast signals re-scan on the server, then reload the board + tracker.
async function refreshSignalsNow() {
    const btn = $("#btn-refresh-signals");
    if (btn) { btn.disabled = true; btn.textContent = "⚡ Refreshing…"; }
    try {
        const r = await api(withMarket("/api/v1/backtest/signals/refresh"), { method: "POST" });
        if (r && r.updated) toast(`Signals refreshed · ${r.total_buys} buys · +${r.added_tracked} tracked · ${r.elapsed_seconds}s`);
        else toast((r && r.note) || "Nothing to refresh", true);
        await loadTradeIdeas();
        await loadTrackedIdeas();
    } catch (e) {
        toast(e.message, true);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = "⚡ Refresh signals"; }
    }
}

// Fire-and-forget server refresh (used by the auto loop when signals are stale).
async function autoRegenSignals() {
    try {
        const r = await api(withMarket("/api/v1/backtest/signals/refresh"), { method: "POST" });
        if (r && r.updated) { await loadTradeIdeas(); await loadTrackedIdeas(); }
    } catch { /* transient — the next tick retries */ }
}

const IDEAS_AUTO_MS = 90000;        // re-price cadence while viewing (matches the live-quote cache)
const IDEAS_STALE_MS = 300000;      // regenerate signals if older than 5 min

function ideasTabActive() {
    return $(".tab.active") && $(".tab.active").dataset.tab === "ideas";
}

// True when two idea payloads describe the same set of ideas (same horizons, same tickers
// in the same order) — i.e. only the prices moved. A re-price never changes the set, so
// this stays true and we can patch in place; if it's false the board is rebuilt instead.
function ideasStructureMatches(a, b) {
    const ha = a.horizons || [], hb = b.horizons || [];
    if (ha.length !== hb.length || ha.some((h, i) => h !== hb[i])) return false;
    return ha.every((h) => {
        const ca = ((a.buckets || {})[h] || {}).candidates || [];
        const cb = ((b.buckets || {})[h] || {}).candidates || [];
        return ca.length === cb.length && ca.every((c, i) => c.ticker === cb[i].ticker);
    });
}

// Swap only the price-driven cells of one row (top line + live-levels), preserving the
// generated plan, the strategy pills, any open AI panel, and the expanded/collapsed state
// of the live-levels drawer.
function patchIdeaRow(row, c) {
    row.querySelector(".idea-top").innerHTML = ideaTopInner(c);
    const slot = row.querySelector(".idea-live-slot");
    if (!slot) return;
    const prev = slot.querySelector(".idea-live");
    const wasOpen = prev && prev.open;
    slot.innerHTML = ideaLiveLevelsHtml(c);
    const next = slot.querySelector(".idea-live");
    if (next && wasOpen) next.open = true;
}

// Walk the fresh payload and patch each existing row's prices in place (matched by column
// horizon + ticker), leaving the rest of the DOM untouched.
function patchIdeaPrices(data) {
    const box = $("#ideas-buckets");
    (data.horizons || []).forEach((h) => {
        const bucket = (data.buckets || {})[h];
        const col = box.querySelector(`.ideas-col[data-horizon="${h}"]`);
        if (!bucket || !col) return;
        const rows = col.querySelectorAll(".idea-row");
        (bucket.candidates || []).forEach((c, ri) => {
            const row = rows[ri];
            if (row && row.dataset.ticker === c.ticker) patchIdeaRow(row, c);
        });
    });
}

// Live re-price: re-fetch ideas and update just the price cells, so the board doesn't flash
// through a "Loading…" rebuild every cycle. Falls back to a full render only if the idea set
// itself changed (which a re-price normally won't do).
async function repriceTradeIdeas() {
    const n = $("#ideas-topn") ? $("#ideas-topn").value : 10;
    let data;
    try {
        data = await api(withMarket(`/api/v1/backtest/ideas?max_per_bucket=${n}`));
    } catch { return; }   // transient — the next tick retries
    if (!IDEAS_DATA || !ideasStructureMatches(IDEAS_DATA, data)) {
        IDEAS_DATA = data;
        renderTradeIdeasTab();
        return;
    }
    IDEAS_DATA = data;
    patchIdeaPrices(data);
    updateIdeasRunInfo();
}

function ideasAutoTick() {
    if (!ideasTabActive() || !($("#ideas-auto") && $("#ideas-auto").checked)) return;
    const stamp = IDEAS_DATA && (IDEAS_DATA.signals_refreshed_at || IDEAS_DATA.run_at);
    const age = stamp ? Date.now() - Date.parse(stamp) : Infinity;
    if (age > IDEAS_STALE_MS) autoRegenSignals();   // stale → regenerate on fresh prices
    else repriceTradeIdeas();                        // else re-price in place (no rebuild)
}

if ($("#btn-refresh-signals")) $("#btn-refresh-signals").addEventListener("click", refreshSignalsNow);
if ($("#ideas-auto")) {
    $("#ideas-auto").checked = localStorage.getItem("ideasAuto") !== "0";   // default on
    $("#ideas-auto").addEventListener("change", (e) => {
        localStorage.setItem("ideasAuto", e.target.checked ? "1" : "0");
        if (e.target.checked) { toast("Auto-refresh on — board stays live while open"); ideasAutoTick(); }
    });
    setInterval(ideasAutoTick, IDEAS_AUTO_MS);
}

// ── AI analyst (advisory LLM layer; rules-only fallback) ─────────

const AI_VERDICT = {
    buy: { label: "Buy", cls: "ai-buy" },
    watch: { label: "Watch", cls: "ai-watch" },
    avoid: { label: "Avoid", cls: "ai-avoid" },
};

// Badge telling the user whether this came from an LLM or the deterministic template.
function aiModeBadge(ai) {
    const isLlm = ai && ai.source === "llm";
    return `<span class="ai-mode ${isLlm ? "ai-mode-llm" : ""}" title="${isLlm
        ? "Generated by " + esc(ai.llm_mode)
        : "Rules-only template — set an LLM provider in .env for narrative analysis"}">${isLlm ? "🤖 " + esc(ai.llm_mode) : "rules"}</span>`;
}

function renderSentiment(sent) {
    if (!sent) return "";
    const cls = sent.consensus === "Bullish" ? "pos" : sent.consensus === "Bearish" ? "neg" : "muted";
    const score = sent.score != null ? `${fmt(sent.score)}/100` : "—";
    return `<div class="ai-sent"><span class="ai-label">News sentiment</span>
        <span class="${cls} b">${esc(sent.consensus || "—")} · ${score}</span>
        ${sent.summary ? `<span class="muted small">${esc(sent.summary)}</span>` : ""}</div>`;
}

// Shared render for a single trade-idea AI read (idea board + tracker rows).
function renderAIPanel(ai) {
    const v = AI_VERDICT[ai.verdict] || { label: ai.verdict || "—", cls: "ai-watch" };
    return `
    <div class="ai-body">
        <div class="ai-head">
            <span class="ai-verdict ${v.cls}">${v.label}</span>
            ${aiModeBadge(ai)}
        </div>
        <p class="ai-thesis">${esc(ai.thesis || "")}</p>
        ${renderSentiment(ai.sentiment)}
        <div class="ai-lines">
            <div><span class="ai-label">Key risk</span> <span class="small">${esc(ai.key_risk || "—")}</span></div>
            <div><span class="ai-label">Invalidation</span> <span class="small">${esc(ai.invalidation || "—")}</span></div>
        </div>
    </div>`;
}

async function fetchIdeaAI(idea) {
    return api("/api/v1/ai/idea", {
        method: "POST",
        body: JSON.stringify({ idea, market: MARKET.key, with_sentiment: true }),
    });
}

// Idea-board 🧠 button: toggles an inline panel under the row (cached after first load).
function onIdeaAI(btn) {
    const panel = btn.closest(".idea-wrap").querySelector(".idea-ai-panel");
    if (!panel.classList.contains("hidden")) { panel.classList.add("hidden"); return; }
    panel.classList.remove("hidden");
    if (panel.dataset.loaded === "1") return;
    const bucket = (IDEAS_DATA && IDEAS_DATA.buckets || {})[btn.dataset.horizon] || {};
    const idea = (bucket.candidates || []).find((c) => c.ticker === btn.dataset.ticker);
    if (!idea) { panel.innerHTML = `<p class="muted small">Idea not found.</p>`; return; }
    panel.innerHTML = `<p class="muted small">🧠 Analyzing…</p>`;
    btn.disabled = true;
    fetchIdeaAI(idea)
        .then((ai) => { panel.innerHTML = renderAIPanel(ai); panel.dataset.loaded = "1"; })
        .catch((e) => { panel.innerHTML = `<p class="muted small">AI unavailable: ${esc(e.message)}</p>`; })
        .finally(() => { btn.disabled = false; });
}

// Tracker open-position 🧠 button: toggles a detail row beneath the position.
function onTrackedAI(btn) {
    const tr = btn.closest("tr");
    const next = tr.nextElementSibling;
    if (next && next.classList.contains("ai-detail-row")) { next.remove(); return; }
    const open = (TRACKED_DATA && (TRACKED_DATA.open || TRACKED_DATA.ideas)) || [];
    const idea = open.find((o) => o.ticker === btn.dataset.ticker && (o.horizon || "") === btn.dataset.horizon);
    if (!idea) return;
    const detail = document.createElement("tr");
    detail.className = "ai-detail-row";
    const td = document.createElement("td");
    td.colSpan = tr.children.length;
    td.innerHTML = `<div class="idea-ai-panel"><p class="muted small">🧠 Analyzing…</p></div>`;
    detail.appendChild(td);
    tr.after(detail);
    const panel = td.querySelector(".idea-ai-panel");
    // Give the analyst a "now" price the tracked idea already knows.
    fetchIdeaAI({ ...idea, price: idea.current_price != null ? idea.current_price : idea.entry_price })
        .then((ai) => { panel.innerHTML = renderAIPanel(ai); })
        .catch((e) => { panel.innerHTML = `<p class="muted small">AI unavailable: ${esc(e.message)}</p>`; });
}

// Past-trades 🧠 post-mortem over the realized record.
async function loadPostmortem() {
    const btn = $("#btn-postmortem"), out = $("#ai-postmortem");
    if (!out) return;
    btn.disabled = true;
    out.innerHTML = `<p class="muted small">🧠 Reviewing past trades…</p>`;
    try {
        const r = await api(withMarket("/api/v1/backtest/ideas/postmortem"));
        out.innerHTML = renderPostmortem(r.review);
    } catch (e) {
        out.innerHTML = `<p class="muted small">AI unavailable: ${esc(e.message)}</p>`;
    } finally {
        btn.disabled = false;
    }
}

function renderPostmortem(rev) {
    if (!rev) return "";
    const lessons = (rev.lessons || []).map((l) => `<li class="small">${esc(l)}</li>`).join("");
    return `<div class="ai-body ai-card">
        <div class="ai-head"><span class="ai-verdict ai-watch">Post-mortem</span>${aiModeBadge(rev)}</div>
        <p class="ai-thesis">${esc(rev.summary || "")}</p>
        ${lessons ? `<ul class="ai-lessons">${lessons}</ul>` : ""}
    </div>`;
}

// Portfolio 🧠 AI read (global — invoked from an inline onclick, like pfSnapshot).
// Uses PF_CURRENT (the last constructed/loaded Portfolio), declared in the Portfolios section.
async function pfAIRead() {
    const out = $("#pf-ai");
    if (!PF_CURRENT || !out) return;
    out.innerHTML = `<p class="muted small">🧠 Reviewing portfolio…</p>`;
    try {
        const ai = await api("/api/v1/portfolios/ai", { method: "POST", body: JSON.stringify(PF_CURRENT) });
        out.innerHTML = renderPortfolioAI(ai);
    } catch (e) {
        out.innerHTML = `<p class="muted small">AI unavailable: ${esc(e.message)}</p>`;
    }
}

function renderPortfolioAI(ai) {
    const strengths = (ai.strengths || []).map((s) => `<li class="small pos">${esc(s)}</li>`).join("");
    const risks = (ai.risks || []).map((r) =>
        `<li class="small"><span class="ai-sev ai-sev-${esc(r.severity)}">${esc(r.severity)}</span> <b>${esc(r.area)}</b> — ${esc(r.note)}</li>`).join("");
    return `<div class="ai-body ai-card">
        <div class="ai-head"><span class="ai-verdict ai-watch">Portfolio read</span>${aiModeBadge(ai)}</div>
        <p class="ai-thesis">${esc(ai.read || "")}</p>
        ${strengths ? `<div class="ai-label">Strengths</div><ul class="ai-lessons">${strengths}</ul>` : ""}
        ${risks ? `<div class="ai-label">Risks</div><ul class="ai-lessons ai-risks">${risks}</ul>` : ""}
    </div>`;
}

// ── Idea tracker (win / loss over time) ──────────────────────────

let TRACKED_DATA = null;

const STATUS_META = {
    winning: { label: "Winning", cls: "pos", icon: "🟢" },
    losing: { label: "Losing", cls: "neg", icon: "🔴" },
    stopped_out: { label: "Stopped", cls: "neg", icon: "⛔" },
    flat: { label: "Flat", cls: "muted", icon: "⚪" },
    unknown: { label: "No price", cls: "muted", icon: "❔" },
};

let ADHERENCE_DATA = null;

async function loadTrackedIdeas() {
    const board = $("#ideas-scoreboard");
    board.innerHTML = `<p class="muted small">Pricing tracked ideas…</p>`;
    try {
        TRACKED_DATA = await api(withMarket("/api/v1/backtest/ideas/tracked"));
        // Discipline scorecard ("system vs you") rides alongside — best-effort, never blocks the tracker.
        try { ADHERENCE_DATA = await api(withMarket("/api/v1/adherence")); } catch { ADHERENCE_DATA = null; }
        renderTracker();
    } catch (e) {
        board.innerHTML = `<p class="muted small">Could not load tracker: ${esc(e.message)}</p>`;
    }
}

// Discipline scorecard — did sticking to the plan beat what you actually did? Compares your real exits
// (paper trades + I'm-in/out marks) to holding each idea to its stop/target/expiry, entry held constant.
function disciplineScorecard(data) {
    const sc = data && data.scorecard;
    if (!sc || !sc.n) {
        return `<div class="disc-card muted small">🎯 <b>Discipline scorecard</b> — mark the ideas you act on with <b>I'm in / I'm out</b> below (or paper-trade them); this then shows how your real exits compared to following each idea's plan.</div>`;
    }
    const pct = (v) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${fmt(v)}%`);
    const d = sc.discipline_delta_pct;
    const verdict = d == null ? ""
        : d < -0.5 ? `<b class="neg">early exits cost you ${fmt(Math.abs(d))}%</b>`
        : d > 0.5 ? `<b class="pos">your calls added ${fmt(d)}%</b>`
        : `<b>you're tracking the plan</b>`;
    const split = (sc.early_cost_n || sc.early_saved_n)
        ? ` · ${sc.early_saved_n} early exit${sc.early_saved_n === 1 ? "" : "s"} saved you, ${sc.early_cost_n} cost you`
        : "";
    const chip = (val, label, cls) =>
        `<div class="score-chip"><span class="score-chip-val ${cls}">${val}</span><span class="score-chip-lbl">${label}</span></div>`;
    return `
    <div class="disc-card">
        <div class="disc-head">🎯 <b>Discipline scorecard</b> <span class="muted small">— your real exits vs following the plan (${sc.acted} acted of ${sc.n})</span></div>
        <div class="scoreboard" style="margin-top:8px;">
            ${chip(pct(sc.plan_return_pct), "📋 Plan book", (sc.plan_return_pct ?? 0) >= 0 ? "pos" : "neg")}
            ${chip(pct(sc.your_return_pct), "🧑 Your book", (sc.your_return_pct ?? 0) >= 0 ? "pos" : "neg")}
            ${chip(pct(d), "Discipline Δ", (d ?? 0) >= 0 ? "pos" : "neg")}
        </div>
        <p class="muted small" style="margin:6px 0 0;">${verdict}${split}. Plan = holding each idea to its stop / target / expiry; entry is held constant so this isolates your <b>exit</b> timing. Fed by paper trades + your I'm-in/out marks.</p>
    </div>`;
}

const EXIT_META = {
    target: { label: "Target", cls: "pos", icon: "🎯" },
    stop: { label: "Stopped", cls: "neg", icon: "⛔" },
    time: { label: "Expired", cls: "muted", icon: "⌛" },
};

// Rank efficiency — the north-star: does the board's RANK actually predict realized return? Shows the
// Spearman rank-IC + the mean realized return of the best-ranked quintile vs the worst. This is the
// scoreboard every ranking change (weights, meta-labeling, gates) has to move.
function rankEfficiencyCard(re) {
    if (!re) return "";
    if (!re.active) {
        return `<div class="factor-card muted small">🎯 <b>Rank efficiency</b> — populates once <b>${re.min_samples || 8}</b> ranked ideas <b>close</b> (measures whether higher-ranked ideas realized higher returns). ${re.n ? `${re.n} so far.` : ""}</div>`;
    }
    const icCls = re.ic > 0.05 ? "pos" : re.ic < -0.05 ? "neg" : "muted";
    const spCls = re.spread_pct >= 0 ? "pos" : "neg";
    const verdict = re.ic > 0.05 && re.spread_pct > 0 ? "the ranking is predictive"
        : re.ic < -0.05 ? "⚠ ranking is inverted — higher ranks did worse"
        : "no clear rank edge yet";
    return `
    <div class="factor-card">
        <div class="small"><span>🎯 <b>Rank efficiency</b></span>
            <span class="muted"> — does entry rank predict return? (${re.n} closed)</span></div>
        <div class="scoreboard" style="margin-top:6px;">
            <div class="score-chip"><span class="score-chip-val ${icCls}">${re.ic >= 0 ? "+" : ""}${fmt(re.ic, 2)}</span><span class="score-chip-lbl">Rank IC</span></div>
            <div class="score-chip"><span class="score-chip-val pos">${fmt(re.top_quintile_return_pct)}%</span><span class="score-chip-lbl">Top quintile</span></div>
            <div class="score-chip"><span class="score-chip-val neg">${fmt(re.bottom_quintile_return_pct)}%</span><span class="score-chip-lbl">Bottom quintile</span></div>
            <div class="score-chip"><span class="score-chip-val ${spCls}">${re.spread_pct >= 0 ? "+" : ""}${fmt(re.spread_pct)}%</span><span class="score-chip-lbl">Spread</span></div>
        </div>
        <p class="muted small" style="margin:6px 0 0;">Spearman IC of entry rank vs realized return, + top-vs-bottom-quintile mean return — ${verdict}. The board's report card: adopt a ranking change only if it moves these.</p>
    </div>`;
}

// Factor scorecard: does each soft factor actually earn its weight? For every factor we compare the
// realized win-rate of closed ideas where it was SUPPORTIVE at entry vs where it was ADVERSE — the
// signed "lift". Populated from the ledger (TRACKED_DATA.attribution); the gate/de-dup aren't shown
// because ideas they removed were never tracked.
function factorScorecard(attr) {
    if (!attr) return "";
    const F = attr.factors || {};
    const order = ["news", "sector", "quality", "earnings", "regime"];
    const LABEL = { news: "📰 News", sector: "🔄 Sector", quality: "💎 Quality", earnings: "⚠️ Earnings", regime: "🌊 Regime" };
    const withData = order.filter((k) => F[k] && (F[k].supportive.n + F[k].adverse.n) > 0);
    if (!attr.sample || !withData.length) {
        return `<div class="factor-card muted small">🎯 <b>Factor scorecard</b> — populates as tracked ideas <b>close</b> (measures each factor's realized win-rate lift: supportive vs adverse at entry). ${attr.sample ? `${attr.sample} closed so far, none yet carrying an active factor.` : ""}</div>`;
    }
    const cell = (c) => c.win_rate != null
        ? `${fmt(c.win_rate)}% <span class="muted">(${c.n})</span>`
        : `<span class="muted">— (${c.n})</span>`;
    const rows = withData.map((k) => {
        const f = F[k];
        const lift = f.lift != null
            ? `<span class="${f.lift >= 0 ? "pos" : "neg"} b">${f.lift >= 0 ? "+" : ""}${fmt(f.lift)}pp</span>`
            : `<span class="muted small" title="Need ≥${attr.min_samples} closed trades in BOTH the supportive and adverse cohorts before a lift is trustworthy.">need more data</span>`;
        return `<tr><td>${LABEL[k]}</td><td class="mono">${cell(f.supportive)}</td><td class="mono">${cell(f.adverse)}</td><td>${lift}</td></tr>`;
    }).join("");
    return `
    <details class="factor-card" open>
        <summary>🎯 <b>Factor scorecard</b> <span class="muted small">— realized win-rate, supportive vs adverse at entry (${attr.sample} closed with factor data)</span></summary>
        <table class="factor-table">
            <thead><tr><th>Factor</th><th>Supportive win</th><th>Adverse win</th><th>Lift</th></tr></thead>
            <tbody>${rows}</tbody>
        </table>
        <p class="muted small" style="margin:6px 0 0;">Lift = supportive − adverse win-rate (pp); positive ⇒ the factor pointed the right way. The quality gate &amp; de-dup aren't scored here — ideas they removed were never tracked.</p>
    </details>`;
}

function renderTracker() {
    const open = TRACKED_DATA.open || TRACKED_DATA.ideas || [];
    const closed = TRACKED_DATA.closed || [];
    const sb = TRACKED_DATA.scoreboard || {};             // open (unrealized)
    const cb = TRACKED_DATA.closed_scoreboard || {};      // realized
    const board = $("#ideas-scoreboard");
    const openD = $("#ideas-tracked-details");
    const pastD = $("#ideas-past-details");
    const eqWrap = $("#ideas-equity-wrap");

    if (!open.length && !closed.length) {
        board.innerHTML = `<p class="muted small">No ideas tracked yet. Click <b>＋ Track current</b> to start following the ideas below — or run a backtest, which tracks them automatically.</p>`
            + disciplineScorecard(ADHERENCE_DATA);
        [openD, pastD, eqWrap].forEach((e) => e.classList.add("hidden"));
        return;
    }

    const pct = (v) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${fmt(v)}%`);
    const chip = (val, label, cls) =>
        `<div class="score-chip"><span class="score-chip-val ${cls}">${val}</span><span class="score-chip-lbl">${label}</span></div>`;
    board.innerHTML = `
        <div class="scoreboard">
            ${chip(cb.win_rate != null ? fmt(cb.win_rate) + "%" : "—", "Win rate (closed)", (cb.win_rate ?? 0) >= 50 ? "pos" : "")}
            ${chip(cb.closed || 0, "Closed", "")}
            ${chip(cb.wins || 0, "✅ Wins", "pos")}
            ${chip(cb.losses || 0, "❌ Losses", "neg")}
            ${chip(cb.targets_hit || 0, "🎯 Targets", "pos")}
            ${chip(cb.stopped || 0, "⛔ Stopped", "neg")}
            ${chip(cb.expired || 0, "⌛ Expired", "muted")}
            ${chip(pct(cb.total_return_pct), "Realized P&L", (cb.total_return_pct ?? 0) >= 0 ? "pos" : "neg")}
            ${chip(open.length, "Open", "")}
            ${chip(pct(sb.avg_return_pct), "Open avg", (sb.avg_return_pct ?? 0) >= 0 ? "pos" : "neg")}
        </div>
        ${TRACKED_DATA.priced_at ? `<p class="muted small" style="margin-top:8px;">Priced ${new Date(TRACKED_DATA.priced_at).toLocaleString()} · win rate = wins ÷ (wins + losses) on <b>closed</b> trades; open ideas move to Past trades once they hit a target, stop, or time-stop.</p>` : ""}
        ${disciplineScorecard(ADHERENCE_DATA)}
        ${rankEfficiencyCard(TRACKED_DATA.rank_efficiency)}
        ${factorScorecard(TRACKED_DATA.attribution)}`;

    // Idea-book equity curve (cumulative realized P&L).
    const eq = TRACKED_DATA.equity_curve || [];
    if (eq.length) { eqWrap.classList.remove("hidden"); drawIdeaEquity(eq); }
    else eqWrap.classList.add("hidden");

    // Open positions.
    if (open.length) {
        openD.classList.remove("hidden");
        openD.querySelector("summary").textContent = `Open positions (${open.length})`;
        $("#ideas-tracked-list").innerHTML = renderTrackedTable(open);
        $("#ideas-tracked-list").querySelectorAll(".trk-row").forEach((r) =>
            r.addEventListener("click", () => analyzeTicker(r.dataset.ticker)));
        $("#ideas-tracked-list").querySelectorAll(".trk-ai-btn").forEach((b) =>
            b.addEventListener("click", (e) => { e.stopPropagation(); onTrackedAI(b); }));
        $("#ideas-tracked-list").querySelectorAll(".disc-btn").forEach((b) =>
            b.addEventListener("click", (e) => { e.stopPropagation(); markIdeaAction(b); }));
    } else openD.classList.add("hidden");

    // Past trades (closed) — with an AI post-mortem over the realized record.
    if (closed.length) {
        pastD.classList.remove("hidden");
        pastD.querySelector("summary").textContent = `Past trades (${closed.length})`;
        $("#ideas-past-list").innerHTML =
            `<div class="ai-postmortem-bar"><button id="btn-postmortem" class="btn btn-ghost btn-s">🧠 AI post-mortem</button></div>
             <div id="ai-postmortem" class="ai-postmortem"></div>`
            + renderPastTradesTable(closed);
        $("#btn-postmortem").addEventListener("click", loadPostmortem);
        $("#ideas-past-list").querySelectorAll(".trk-row").forEach((r) =>
            r.addEventListener("click", () => analyzeTicker(r.dataset.ticker)));
    } else pastD.classList.add("hidden");
}

// Log what you actually did with a tracked idea → feeds the discipline scorecard. Price defaults to
// the live quote server-side. Refreshes the tracker so the scorecard + button state update.
async function markIdeaAction(btn) {
    const d = btn.dataset;
    btn.disabled = true;
    try {
        const r = await api("/api/v1/backtest/ideas/tracked/action", {
            method: "POST",
            body: JSON.stringify({ ticker: d.ticker, horizon: d.horizon, market: d.market, action: d.action }),
        });
        if (r && r.ok === false) { toast(r.error || "Could not log that action", true); btn.disabled = false; return; }
        toast(d.action === "in" ? `Logged — you're in ${esc(d.ticker)}` : `Logged — you're out of ${esc(d.ticker)}`);
        loadTrackedIdeas();
    } catch (e) { toast(e.message, true); btn.disabled = false; }
}

// "Normal drawdown" check on an open position: is the live dip within, or beyond, what winning
// backtest trades of the backing strategies typically sat through? Reassurance vs an early warning —
// the anti-panic half of "stick with it". Empty when the position isn't down or carries no band.
function ddBadge(t) {
    if (!t.dd_status) return "";
    const meta = {
        within: { cls: "dd-within", txt: "normal dip", icon: "✓" },
        stretching: { cls: "dd-within", txt: "deep but normal", icon: "↓" },
        beyond: { cls: "dd-beyond", txt: "beyond typical", icon: "⚠️" },
    }[t.dd_status];
    if (!meta) return "";
    const title = t.dd_status === "beyond"
        ? `Down ${fmt(t.dd_pct, 1)}% — past the ~${fmt(t.deep_drawdown_pct, 1)}% that 4 in 5 winning backtest trades stayed within. The thesis may be breaking; worth a fresh look.`
        : `Down ${fmt(t.dd_pct, 1)}% — within the ~${fmt(t.typical_drawdown_pct, 1)}% dip winning backtest trades typically sat through. A normal wobble, not a reason to bail.`;
    return ` <span class="dd-badge ${meta.cls}" title="${esc(title)}">${meta.icon} ${meta.txt}</span>`;
}

// Sell-side EXIT nudge on an open position: the smart money has turned AGAINST it since entry (the pros
// now distributing a long you hold / accumulating a short you hold). Read-only — a "consider trimming"
// flag, never an auto-close. Empty unless the signal is against the position.
function smExitBadge(t) {
    const x = t.smart_money_exit;
    if (!x) return "";
    const title = `${x.detail ? esc(x.detail) + " — " : ""}${esc(x.message || "Smart money has turned against this position.")} Point-in-time smart-money read, not part of the backtest.`;
    return ` <span class="dd-badge dd-beyond" title="${title}">${esc(x.label || "🚩 smart money exiting")}</span>`;
}

// "You" cell: log what you actually did with this idea (I'm-in / I'm-out) — the capture that feeds the
// discipline scorecard. State-driven from the record's user_action.
function actionButtons(t) {
    const ua = t.user_action || {};
    const base = `data-ticker="${esc(t.ticker)}" data-horizon="${esc(t.horizon || "")}" data-market="${esc(t.market || MARKET.key)}"`;
    if (ua.exited) return `<span class="disc-done" title="You logged an entry and an exit — this counts toward the discipline scorecard above.">✓ logged</span>`;
    if (ua.entered) return `<button class="disc-btn disc-out" ${base} data-action="out" title="Log that you exited here — the scorecard compares your exit to the plan's (stop/target/expiry).">I'm out</button>`;
    return `<button class="disc-btn disc-in" ${base} data-action="in" title="Log that you took this trade — then hit I'm out when you exit, to score your discipline vs the plan.">I'm in</button>`;
}

function renderTrackedTable(ideas) {
    if (!ideas.length) return `<p class="muted small">None.</p>`;
    const rows = ideas.map((t) => {
        const st = STATUS_META[t.status] || STATUS_META.unknown;
        const ret = t.return_pct != null ? `${t.return_pct >= 0 ? "+" : ""}${fmt(t.return_pct)}%` : "—";
        const retCls = t.return_pct == null ? "muted" : (t.return_pct >= 0 ? "pos" : "neg");
        return `<tr class="trk-row" data-ticker="${esc(t.ticker)}" title="Analyze ${esc(t.ticker)}">
            <td class="mono b">${esc(t.ticker)}${sideTag(t.side)}</td>
            <td><span class="pill pill-muted">${esc(t.horizon)}</span></td>
            <td class="mono">${priceFmt(t.entry_price)}</td>
            <td class="mono neg">${priceFmt(t.stop_price)}</td>
            <td class="mono">${priceFmt(t.current_price)}</td>
            <td class="mono ${retCls}">${ret}</td>
            <td class="small ${st.cls}">${st.icon} ${st.label}${ddBadge(t)}${smExitBadge(t)}</td>
            <td>${actionButtons(t)}</td>
            <td><button class="trk-ai-btn" data-ticker="${esc(t.ticker)}" data-horizon="${esc(t.horizon || "")}" title="AI read of this position">🧠</button></td>
        </tr>`;
    }).join("");
    return `<table class="lab-table trk-table">
        <thead><tr><th>Ticker</th><th>Horizon</th><th>Entry</th><th>Stop</th><th>Now</th><th>P&L</th><th>Status</th><th>You</th><th>AI</th></tr></thead>
        <tbody>${rows}</tbody></table>`;
}

function renderPastTradesTable(closed) {
    if (!closed.length) return `<p class="muted small">None.</p>`;
    const rows = closed.map((t) => {
        const ex = EXIT_META[t.exit_reason] || { label: t.exit_reason || "—", cls: "muted", icon: "" };
        const r = t.realized_return_pct;
        const ret = r != null ? `${r >= 0 ? "+" : ""}${fmt(r)}%` : "—";
        const retCls = r == null ? "muted" : (r >= 0 ? "pos" : "neg");
        return `<tr class="trk-row" data-ticker="${esc(t.ticker)}" title="Analyze ${esc(t.ticker)}">
            <td class="mono b">${esc(t.ticker)}${sideTag(t.side)}</td>
            <td><span class="pill pill-muted">${esc(t.horizon)}</span></td>
            <td class="mono">${priceFmt(t.entry_price)}</td>
            <td class="mono">${priceFmt(t.exit_price)}</td>
            <td class="small ${ex.cls}">${ex.icon} ${ex.label}</td>
            <td class="mono ${retCls}">${ret}</td>
            <td class="mono">${t.bars_held != null ? t.bars_held + "d" : "—"}</td>
            <td class="small muted">${esc(t.exit_date || "—")}</td>
        </tr>`;
    }).join("");
    return `<table class="lab-table trk-table">
        <thead><tr><th>Ticker</th><th>Horizon</th><th>Entry</th><th>Exit</th><th>Reason</th><th>Realized</th><th>Held</th><th>Exit date</th></tr></thead>
        <tbody>${rows}</tbody></table>`;
}

let IDEA_CHART = null;
function drawIdeaEquity(points) {
    const canvas = $("#ideas-equity-chart");
    if (!canvas || !window.Chart) return;
    if (IDEA_CHART) { IDEA_CHART.destroy(); IDEA_CHART = null; }
    const pts = (points || []).map(([t, v]) => ({ x: t, y: v }));
    const ct = chartTheme();   // realized P&L → green is the semantic (reserved) color
    IDEA_CHART = new Chart(canvas, {
        type: "line",
        data: { datasets: [{ label: "Cumulative realized P&L (%)", data: pts, borderColor: ct.pos, borderWidth: 1.6, pointRadius: 0, fill: true, backgroundColor: hexTint(ct.pos, 0.1), tension: 0.1 }] },
        options: {
            responsive: true, maintainAspectRatio: false, animation: false,
            interaction: { mode: "index", intersect: false },
            scales: {
                // linear x over epoch-ms (no Chart.js date adapter needed).
                x: { type: "linear", grid: { display: false },
                     ticks: { color: ct.dim, maxTicksLimit: 8, autoSkip: true,
                              callback: (v) => new Date(v).toLocaleDateString(undefined, { month: "short", day: "numeric" }) } },
                y: { grid: { color: ct.grid }, ticks: { color: ct.dim, callback: (v) => v + "%" } },
            },
            plugins: {
                legend: { display: false },
                tooltip: { callbacks: { title: (items) => (items.length ? new Date(items[0].parsed.x).toLocaleDateString() : "") } },
            },
        },
    });
}

$("#btn-track-ideas").addEventListener("click", async () => {
    const btn = $("#btn-track-ideas");
    btn.disabled = true;
    try {
        const r = await api(withMarket(`/api/v1/backtest/ideas/track?max_per_bucket=${$("#ideas-topn").value}`), { method: "POST" });
        toast(`Tracking ${r.added} new idea${r.added === 1 ? "" : "s"} · ${r.total} total`);
        await loadTrackedIdeas();
    } catch (e) {
        toast(e.message, true);
    } finally {
        btn.disabled = false;
    }
});

$("#btn-refresh-tracked").addEventListener("click", loadTrackedIdeas);

$("#btn-reset-tracked").addEventListener("click", async () => {
    if (!confirm("Forget all tracked ideas and reset the scoreboard?")) return;
    try {
        await api(withMarket("/api/v1/backtest/ideas/tracked"), { method: "DELETE" });
        toast("Idea tracker reset");
        await loadTrackedIdeas();
    } catch (e) {
        toast(e.message, true);
    }
});

// ══ Portfolio tab ════════════════════════════════════════════════

let PF_LOADED = false;
let PF_CONFIG = null;       // { risk_profiles[], weighting_schemes[], bases[] }
let PF_THEMES = [];         // [{slug,name,description,count}]
let PF_FUND_STRATS = [];    // fundamental screen strategies [{id,name,category}]
let PF_TRADE_STRATS = [];   // trading strategies [{id,name,category}]
let PF_CURRENT = null;      // last constructed/loaded Portfolio

const PF_BASIS_LABEL = { fundamental: "Fundamental", systematic: "Systematic", blend: "Blend" };
const PF_WEIGHT_LABEL = { equal: "Equal (1/N)", score: "Score-weighted", conviction: "Conviction-weighted" };

const cap = (s) => String(s || "").charAt(0).toUpperCase() + String(s || "").slice(1);
const pfPct = (v) => (v == null ? "—" : fmt(v * 100, 1) + "%");

async function initPortfolio() {
    if (PF_LOADED) return;
    PF_LOADED = true;   // set early so rapid tab clicks don't double-init

    // Load each source independently so one failure doesn't blank the whole tab.
    const [cfgR, themesR, fundR, tradeR] = await Promise.allSettled([
        api("/api/v1/portfolios/config"),
        api("/api/v1/portfolios/themes"),
        api(withMarket("/api/v1/strategies")),
        api("/api/v1/backtest/strategies"),
    ]);

    // The /portfolios endpoints are the newest in the app. If config fails (typically
    // 404), the running server predates the portfolio feature — surface an actionable
    // message instead of leaving the controls silently empty.
    if (cfgR.status !== "fulfilled") {
        PF_LOADED = false;   // let a retry re-run after the server is restarted
        const msg = esc(cfgR.reason && cfgR.reason.message ? cfgR.reason.message : "unavailable");
        $("#pf-result").innerHTML = `<div class="card">
            <h3>⚠️ Portfolio API unavailable</h3>
            <p class="muted">The <code>/api/v1/portfolios</code> endpoints returned an error (<b>${msg}</b>). This almost always means the running server is an older build started before the Portfolio feature was added.</p>
            <p class="muted">Restart your dev server, then refresh this page:</p>
            <pre class="pf-code">uvicorn app:app --host 127.0.0.1 --port 8000 --reload</pre></div>`;
        $("#pf-saved").innerHTML = `<p class="muted small">Unavailable until the server is restarted.</p>`;
        toast("Portfolio API unavailable — restart the server, then refresh", true);
        return;
    }

    PF_CONFIG = cfgR.value;
    PF_THEMES = themesR.status === "fulfilled" ? (themesR.value.themes || []) : [];
    const pick = (s) => ({ id: s.id, name: s.name, category: s.category });
    PF_FUND_STRATS = fundR.status === "fulfilled" ? (fundR.value.strategies || []).map(pick) : [];
    PF_TRADE_STRATS = tradeR.status === "fulfilled" ? (tradeR.value.strategies || []).map(pick) : [];
    pfPopulateSelects();
    renderPfStrats();
    pfWireEvents();
    loadSavedPortfolios();
}

function pfPopulateSelects() {
    const opt = (v, label, sel = false) => `<option value="${esc(v)}"${sel ? " selected" : ""}>${esc(label)}</option>`;
    $("#pf-basis").innerHTML = (PF_CONFIG.bases || []).map((b) => opt(b, PF_BASIS_LABEL[b] || b, b === "blend")).join("");
    $("#pf-risk").innerHTML = (PF_CONFIG.risk_profiles || []).map((r) =>
        opt(r.appetite, `${cap(r.appetite)} · ${r.cap_tier_index} · ${r.max_positions} max`, r.appetite === "balanced")).join("");
    $("#pf-weighting").innerHTML = (PF_CONFIG.weighting_schemes || []).map((w) =>
        opt(w, PF_WEIGHT_LABEL[w] || w, w === "score")).join("");
    $("#pf-theme").innerHTML = opt("", "No tilt") + PF_THEMES.map((t) => opt(t.slug, `${t.name} (${t.count})`)).join("");
    pfSyncMaxPlaceholder();
}

function pfRiskPreset(appetite) {
    return (PF_CONFIG.risk_profiles || []).find((r) => r.appetite === appetite) || null;
}

function pfSyncMaxPlaceholder() {
    const preset = pfRiskPreset($("#pf-risk").value);
    if (preset) $("#pf-max").placeholder = String(preset.max_positions);
}

function pfStrategyPool(basis) {
    if (basis === "fundamental") return PF_FUND_STRATS;
    if (basis === "systematic") return PF_TRADE_STRATS;
    return [   // blend → both libraries, tagged
        ...PF_FUND_STRATS.map((s) => ({ ...s, lib: "Fundamental" })),
        ...PF_TRADE_STRATS.map((s) => ({ ...s, lib: "Trading" })),
    ];
}

function renderPfStrats() {
    const pool = pfStrategyPool($("#pf-basis").value);
    $("#pf-strats").innerHTML = pool.map((s) =>
        `<label class="pf-strat"><input type="checkbox" value="${esc(s.id)}" checked>
            <span>${esc(s.name)}${s.lib ? ` <span class="muted small">${esc(s.lib)}</span>` : ""}</span></label>`).join("");
    pfUpdateStratCount();
}

function pfUpdateStratCount() {
    const boxes = $$("#pf-strats input[type=checkbox]");
    const checked = boxes.filter((b) => b.checked).length;
    $("#pf-strats-count").textContent = `— ${checked}/${boxes.length} selected`;
}

function pfWireEvents() {
    $("#pf-basis").addEventListener("change", renderPfStrats);
    $("#pf-risk").addEventListener("change", pfSyncMaxPlaceholder);
    $("#pf-strats").addEventListener("change", pfUpdateStratCount);
    $("#btn-pf-preview").addEventListener("click", () => pfBuild(false));
    $("#btn-pf-save").addEventListener("click", () => pfBuild(true));
    $("#btn-pf-refresh-saved").addEventListener("click", loadSavedPortfolios);
}

function pfSpec() {
    const strategy_ids = $$("#pf-strats input[type=checkbox]").filter((b) => b.checked).map((b) => b.value);
    const maxRaw = $("#pf-max").value.trim();
    return {
        name: $("#pf-name").value.trim(),
        market: MARKET.key,
        basis: $("#pf-basis").value,
        strategy_ids,
        theme: $("#pf-theme").value || null,
        risk: $("#pf-risk").value,
        weighting: $("#pf-weighting").value,
        max_positions: maxRaw ? parseInt(maxRaw, 10) : null,
    };
}

async function pfBuild(save) {
    const spec = pfSpec();
    if (!spec.name) { toast("Give the portfolio a name", true); return; }
    if (!spec.strategy_ids.length) { toast("Select at least one strategy", true); return; }

    const status = $("#pf-status");
    const btns = [$("#btn-pf-preview"), $("#btn-pf-save")];
    status.textContent = save ? "Constructing & saving…" : "Constructing preview…";
    btns.forEach((b) => (b.disabled = true));
    try {
        const path = save ? "/api/v1/portfolios" : "/api/v1/portfolios/construct";
        const pf = await api(path, { method: "POST", body: JSON.stringify(spec) });
        PF_CURRENT = pf;
        renderPortfolio(pf);
        if (save) { toast(`Saved “${pf.name}”`); loadSavedPortfolios(); }
    } catch (e) {
        $("#pf-result").innerHTML = `<div class="card"><p class="muted">Could not construct portfolio: ${esc(e.message)}</p></div>`;
        toast(e.message, true);
    } finally {
        status.textContent = "";
        btns.forEach((b) => (b.disabled = false));
    }
}

function themeName(slug) {
    const t = PF_THEMES.find((x) => x.slug === slug);
    return t ? t.name : slug;
}

function renderPortfolio(pf) {
    const s = pf.summary || {};
    const savedPill = pf.id ? `<span class="pill pill-low">saved</span>` : `<span class="pill pill-muted">preview</span>`;

    const snapRet = pf.latest_snapshot ? pf.latest_snapshot.return_pct : null;
    const tiles = [
        ...(snapRet != null ? [metricCell("Return", `${snapRet >= 0 ? "+" : ""}${fmt(snapRet)}%`,
            snapRet >= 0 ? "pos" : "neg", "Mark-to-market return vs cost basis (weight-weighted; cash flat).")] : []),
        metricCell("Positions", fmt(s.position_count, 0), "", "Number of names in the basket."),
        metricCell("Invested", pfPct(s.invested_pct), "pos", "Share of capital deployed (rest is cash)."),
        metricCell("Cash", pfPct(pf.cash_pct), "", "Reserved cash buffer."),
        metricCell("Top-3 conc.", pfPct(s.top3_concentration), "", "Combined weight of the 3 largest positions."),
        metricCell("Sectors", fmt(s.sector_count, 0), "", "Distinct sectors represented."),
        metricCell("Avg conviction", fmt(s.avg_conviction, 2) + "×", "", "Average number of strategies flagging each name."),
        metricCell("Wtd win rate", s.weighted_win_rate != null ? fmt(s.weighted_win_rate) + "%" : "—",
            s.weighted_win_rate >= 50 ? "pos" : "", "Weight-weighted backtest win rate of the constituents."),
        metricCell("Wtd Sharpe", s.weighted_sharpe != null ? fmt(s.weighted_sharpe, 2) : "—", "",
            "Weight-weighted backtest Sharpe of the constituents."),
    ].join("");

    // Live marks (per-position P&L) from the latest mark-to-market snapshot.
    const snap = pf.latest_snapshot || null;
    const markBy = {};
    if (snap && snap.positions) snap.positions.forEach((m) => { markBy[m.ticker] = m; });

    const posRows = (pf.positions || []).map((p) => {
        const stop = p.stop_price != null
            ? `<span class="neg">${priceFmt(p.stop_price)}</span>${p.stop_pct != null ? ` <span class="small">(−${fmt(p.stop_pct * 100, 1)}%)</span>` : ""}`
            : "—";
        const tgt = p.target_pct != null ? `<span class="pos">+${fmt(p.target_pct * 100, 1)}%</span>` : "—";
        const m = markBy[p.ticker];
        const now = m && m.current_price != null ? priceFmt(m.current_price) : "—";
        const r = m ? m.return_pct : null;
        const pnl = r != null ? `<span class="${r >= 0 ? "pos" : "neg"}">${r >= 0 ? "+" : ""}${fmt(r)}%</span>` : "—";
        return `<tr title="${esc(p.rationale || "")}">
            <td class="mono b">${esc(p.ticker)}</td>
            <td class="small">${esc(p.sector || "—")}</td>
            <td class="mono b" style="color:var(--accent)">${fmt(p.weight * 100, 1)}%</td>
            <td class="mono">${p.conviction}×</td>
            <td class="mono">${p.entry_price != null ? priceFmt(p.entry_price) : "—"}</td>
            <td class="mono">${now}</td>
            <td class="mono">${pnl}</td>
            <td class="mono">${stop}</td>
            <td class="mono">${tgt}</td>
        </tr>`;
    }).join("");

    const notes = (s.notes && s.notes.length)
        ? `<div class="pf-notes mt"><b class="small">Construction notes</b><ul>${s.notes.map((n) => `<li class="muted small">${esc(n)}</li>`).join("")}</ul></div>`
        : "";

    $("#pf-result").innerHTML = `
    <div class="card">
        <div class="card-head">
            <h2>${esc(pf.name)} ${savedPill}</h2>
            <div class="row gap-s">
                <button class="btn btn-ghost btn-s" onclick="pfAIRead()" title="AI construction & risk read of this basket">🧠 AI read</button>
                ${pf.id ? `<button class="btn btn-ghost btn-s" onclick="pfSnapshot('${pf.id}')" title="Mark all positions to live price and record a NAV point">📸 Snapshot</button>` : ""}
                ${pf.id ? `<button class="btn btn-ghost btn-s" onclick="pfRebalance('${pf.id}')">⇄ Rebalance</button>` : ""}
            </div>
        </div>
        <div class="pf-pills">
            <span class="pill pill-muted">${esc(PF_BASIS_LABEL[s.basis] || s.basis || "")}</span>
            <span class="pill pill-muted">${esc(cap(s.risk))} risk</span>
            <span class="pill pill-muted">${esc(s.universe_index || "")}</span>
            <span class="pill pill-muted">${esc(PF_WEIGHT_LABEL[s.weighting] || s.weighting || "")}</span>
            ${s.theme ? `<span class="pill pill-accent">${esc(themeName(s.theme))}</span>` : ""}
        </div>
        <div class="metrics-grid mt">${tiles}</div>
        <div id="pf-ai" class="ai-postmortem" style="margin-top:12px;"></div>
        <div id="pf-nav-wrap" class="hidden" style="margin-top:14px;">
            <div class="muted small" style="margin-bottom:6px;">Portfolio NAV — mark-to-market value vs cost basis (1.00 = break-even).</div>
            <div class="chart-wrap" style="height:180px;"><canvas id="pf-nav-chart"></canvas></div>
        </div>
        ${renderAllocBars(s.allocation_by_sector)}
        <div class="table-scroll mt"><table class="ti-table">
            <thead><tr><th>Ticker</th><th>Sector</th><th>Weight</th><th>Conv</th><th>Entry</th><th>Now</th><th>P&L</th><th>Stop</th><th>Target</th></tr></thead>
            <tbody>${posRows}</tbody>
        </table></div>
        ${notes}
    </div>`;

    if (pf.disclaimer) $("#pf-disclaimer").textContent = pf.disclaimer;
    if (pf.id) loadPortfolioNav(pf.id);
}

let PF_NAV_CHART = null;
async function loadPortfolioNav(id) {
    try {
        const h = await api(`/api/v1/portfolios/${encodeURIComponent(id)}/history`);
        const snaps = h.snapshots || [];
        if (snaps.length < 2) return;                       // need ≥2 points for a curve
        const wrap = $("#pf-nav-wrap");
        if (!wrap) return;
        wrap.classList.remove("hidden");
        const pts = snaps.map((s) => ({ x: new Date(s.ts).getTime(), y: s.nav }));
        const canvas = $("#pf-nav-chart");
        if (!canvas || !window.Chart) return;
        if (PF_NAV_CHART) { PF_NAV_CHART.destroy(); PF_NAV_CHART = null; }
        const ct = chartTheme();
        PF_NAV_CHART = new Chart(canvas, {
            type: "line",
            data: { datasets: [{ label: "NAV", data: pts, borderColor: ct.brand, borderWidth: 1.6, pointRadius: 2, fill: true, backgroundColor: hexTint(ct.brand, 0.1), tension: 0.1 }] },
            options: {
                responsive: true, maintainAspectRatio: false, animation: false,
                interaction: { mode: "index", intersect: false },
                scales: {
                    x: { type: "linear", grid: { display: false },
                         ticks: { color: ct.dim, maxTicksLimit: 8, autoSkip: true,
                                  callback: (v) => new Date(v).toLocaleDateString(undefined, { month: "short", day: "numeric" }) } },
                    y: { grid: { color: ct.grid }, ticks: { color: ct.dim } },
                },
                plugins: { legend: { display: false }, tooltip: { callbacks: { title: (items) => (items.length ? new Date(items[0].parsed.x).toLocaleDateString() : "") } } },
            },
        });
    } catch { /* history is optional */ }
}

async function pfSnapshot(id) {
    try {
        const snap = await api(`/api/v1/portfolios/${encodeURIComponent(id)}/snapshot`, { method: "POST" });
        toast(`Snapshot recorded · NAV ${fmt(snap.nav, 3)} (${snap.return_pct >= 0 ? "+" : ""}${fmt(snap.return_pct)}%)`);
        pfLoad(id);                                         // reload to show the new point
    } catch (e) {
        toast(e.message, true);
    }
}

function renderAllocBars(alloc) {
    const entries = Object.entries(alloc || {});
    if (!entries.length) return "";
    const max = Math.max(...entries.map(([, v]) => v));
    const bars = entries.map(([sector, w]) => {
        const width = max > 0 ? (w / max) * 100 : 0;
        return `<div class="pf-bar">
            <span class="pf-bar-label small">${esc(sector)}</span>
            <span class="pf-bar-track"><span class="pf-bar-fill" style="width:${width.toFixed(1)}%"></span></span>
            <span class="pf-bar-val mono small">${fmt(w * 100, 1)}%</span>
        </div>`;
    }).join("");
    return `<div class="pf-alloc mt"><div class="small muted b">Allocation by sector</div>${bars}</div>`;
}

async function loadSavedPortfolios() {
    const box = $("#pf-saved");
    try {
        const list = await api("/api/v1/portfolios");
        if (!list.length) {
            box.innerHTML = `<p class="muted small">No saved portfolios yet — build one above and hit <b>Save</b>.</p>`;
            return;
        }
        box.innerHTML = list.map(renderSavedRow).join("");
        if (!$("#pf-disclaimer").textContent) $("#pf-disclaimer").textContent = list[0].disclaimer || "";
    } catch (e) {
        box.innerHTML = `<p class="muted small">Could not load saved portfolios: ${esc(e.message)}</p>`;
    }
}

function renderSavedRow(pf) {
    const s = pf.summary || {};
    const created = pf.created_at ? new Date(pf.created_at).toLocaleDateString() : "";
    return `
    <div class="pf-saved-row">
        <div class="pf-saved-main">
            <div class="pf-saved-title"><span class="b">${esc(pf.name)}</span>
                <span class="pill pill-muted">${esc(PF_BASIS_LABEL[s.basis] || s.basis || "")}</span>
                <span class="pill pill-muted">${esc(cap(s.risk || ""))}</span>
                ${s.theme ? `<span class="pill pill-accent">${esc(themeName(s.theme))}</span>` : ""}
            </div>
            <div class="muted small">${fmt(s.position_count, 0)} positions · ${fmt((s.invested_pct || 0) * 100, 0)}% invested · created ${esc(created)}</div>
        </div>
        <div class="pf-saved-actions row gap-s">
            <button class="btn btn-s" onclick="pfLoad('${pf.id}')">Load</button>
            <button class="btn btn-s" onclick="pfRebalance('${pf.id}')">⇄ Rebalance</button>
            <button class="btn btn-ghost btn-s" onclick="pfDelete('${pf.id}')" title="Delete">✕</button>
        </div>
    </div>
    <div class="pf-rebal" id="pf-rebal-${pf.id}"></div>`;
}

async function pfLoad(id) {
    $$(".tab").find((b) => b.dataset.tab === "portfolio").click();
    try {
        const pf = await api(`/api/v1/portfolios/${encodeURIComponent(id)}`);
        PF_CURRENT = pf;
        renderPortfolio(pf);
        $("#pf-result").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (e) {
        toast(e.message, true);
    }
}

async function pfRebalance(id) {
    let target = $(`#pf-rebal-${id}`);
    if (target) target.innerHTML = `<p class="muted small" style="padding:8px 4px;">Rebalancing… (fetching live prices)</p>`;
    else toast("Rebalancing…");
    try {
        const res = await api(`/api/v1/portfolios/${encodeURIComponent(id)}/rebalance`, { method: "POST" });
        target = $(`#pf-rebal-${id}`);
        if (target) {
            target.innerHTML = renderRebalance(res);
            target.scrollIntoView({ behavior: "smooth", block: "nearest" });
        }
    } catch (e) {
        target = $(`#pf-rebal-${id}`);
        if (target) target.innerHTML = "";
        toast(e.message, true);
    }
}

const PF_ACT_CLS = { BUY: "pf-act-buy", ADD: "pf-act-buy", HOLD: "pf-act-hold", TRIM: "pf-act-trim", EXIT: "pf-act-exit" };

function renderRebalance(res) {
    const actions = res.actions || [];
    if (!actions.length) return `<div class="pf-rebal-card"><p class="muted small">No actions — nothing to rebalance.</p></div>`;
    const rows = actions.map((a) => {
        const dr = a.drift_pct || 0;
        const ret = a.return_pct;
        return `<tr>
            <td class="mono b">${esc(a.ticker)}</td>
            <td><span class="pf-act ${PF_ACT_CLS[a.action] || ""}">${esc(a.action)}</span></td>
            <td class="mono">${fmt(a.held_weight * 100, 1)}% → ${fmt(a.target_weight * 100, 1)}%</td>
            <td class="mono ${dr > 0 ? "pos" : dr < 0 ? "neg" : ""}">${dr > 0 ? "+" : ""}${fmt(dr, 1)}pp</td>
            <td class="mono">${priceFmt(a.current_price)}</td>
            <td class="mono ${ret != null && ret >= 0 ? "pos" : ret != null ? "neg" : ""}">${ret != null ? (ret >= 0 ? "+" : "") + fmt(ret, 1) + "%" : "—"}</td>
        </tr>`;
    }).join("");
    const counts = (res.summary && res.summary.counts) || {};
    const meta = ["BUY", "ADD", "TRIM", "HOLD", "EXIT"].filter((k) => counts[k]).map((k) => `${counts[k]} ${k.toLowerCase()}`).join(" · ");
    return `<div class="pf-rebal-card">
        <div class="row gap-s" style="justify-content:space-between; align-items:baseline;">
            <b class="small">⇄ Rebalance — ${esc(res.name)}</b>
            <span class="muted small">${esc(meta)}${res.as_of ? ` · ${new Date(res.as_of).toLocaleDateString()}` : ""}</span>
        </div>
        <div class="table-scroll mt"><table class="ti-table">
            <thead><tr><th>Ticker</th><th>Action</th><th>Weight (held→target)</th><th>Drift</th><th>Price</th><th>Return</th></tr></thead>
            <tbody>${rows}</tbody>
        </table></div>
        <p class="muted small mt">${esc(res.disclaimer || "")}</p>
    </div>`;
}

async function pfDelete(id) {
    if (!window.confirm("Delete this portfolio? This cannot be undone.")) return;
    try {
        await api(`/api/v1/portfolios/${encodeURIComponent(id)}`, { method: "DELETE" });
        toast("Portfolio deleted");
        loadSavedPortfolios();
    } catch (e) {
        toast(e.message, true);
    }
}

// ── Bots ─────────────────────────────────────────────────────────

let BOT_LOADED = false;
let BOT_DATA = null;

const botPrice = (v) => (v == null ? "—" : "$" + pfmt(v));   // bot is crypto/USD
const sidePill = (side) => `<span class="pill ${side === "short" ? "pill-sell" : "pill-buy"}">${side === "short" ? "SHORT" : "LONG"}</span>`;
// Compact SHORT-only tag for tables/cards — longs render nothing (long is the default; avoids noise).
const sideTag = (side) => side === "short" ? ` <span class="pill pill-sell idea-side-pill">SHORT</span>` : "";
const convPill = (n) => `<span class="pill ${n >= 3 ? "pill-buy" : n >= 2 ? "pill-hold" : "pill-low"}">${n}×</span>`;
// A synthetic "bot" (kind:"ideas") that aggregates the whole signal-bot family by conviction.
const IDEAS_BOT = {
    id: "__ideas__", name: "★ Intraday Trade Ideas", kind: "ideas",
    description: "Every signal bot aggregated by conviction into ranked long & short ideas on your chosen timeframe.",
    default_timeframe: "4h", timeframes: ["5m", "10m", "15m", "30m", "1h", "4h"],
};
const CONVICTION_PILL = { proceed: "pill-buy", caution: "pill-hold", reduce: "pill-sell" };
const SEVERITY_PILL = { high: "pill-high", medium: "pill-medium", low: "pill-low" };

function botParams() {
    const p = new URLSearchParams({ top_n: $("#bot-topn").value, universe: $("#bot-universe").value });
    if ($("#bot-ai").checked || $("#bot-apply").checked) p.set("ai", "true");
    if ($("#bot-apply").checked) p.set("apply_ai_risk", "true");
    return p.toString();
}

let BOTS_META = [];
const currentBot = () => BOTS_META.find((b) => b.id === $("#bot-select").value);

async function loadBotsList() {
    try {
        const { bots } = await api("/api/v1/bots");
        BOTS_META = [IDEAS_BOT, ...(bots || [])];   // Trade Ideas first (default)
        const sel = $("#bot-select");
        sel.innerHTML = BOTS_META.map((b) => `<option value="${esc(b.id)}">${esc(b.name)}</option>`).join("");
        sel.addEventListener("change", onBotChange);
        applyBotKindUI();
    } catch { /* leave empty */ }
}

function applyBotKindUI() {
    const bot = currentBot();
    if (!bot) return;
    $("#bot-desc").textContent = bot.description || "";
    const kind = bot.kind;                        // "ideas" | "signal" | "basket"
    const showTf = kind === "ideas" || kind === "signal";
    $$(".bot-basket-only").forEach((e) => { e.style.display = kind === "basket" ? "" : "none"; });
    $("#bot-tf").style.display = showTf ? "" : "none";
    $("#bot-conviction").style.display = kind === "ideas" ? "" : "none";
    $("#bot-strict-wrap").style.display = kind === "ideas" ? "" : "none";
    $("#btn-bot-run").textContent = kind === "ideas" ? "Find ideas →" : (kind === "signal" ? "Scan →" : "Run →");
    if (showTf && bot.timeframes && bot.timeframes.length) {
        const tf = $("#bot-tf");
        const cur = tf.value;
        tf.innerHTML = bot.timeframes.map((t) => `<option>${t}</option>`).join("");
        tf.value = bot.timeframes.includes(cur) ? cur : (bot.default_timeframe || bot.timeframes[0]);
    }
}

function onBotChange() {
    applyBotKindUI();
    $("#bot-result").innerHTML = "";
    $("#bot-status").textContent = "";
    runBot();
}

async function runBot() {
    BOT_LOADED = true;
    if (!BOTS_META.length) await loadBotsList();
    const bot = currentBot();
    if (!bot) return;
    if (bot.kind === "ideas") return runIdeas();
    return bot.kind === "signal" ? scanBot(bot) : runMomentum();
}

async function runIdeas() {
    const tf = $("#bot-tf").value, uni = $("#bot-universe").value, conv = $("#bot-conviction").value;
    const strict = $("#bot-strict").checked;
    const status = $("#bot-status");
    status.className = "status-line";
    status.textContent = `Scanning all bots on ${tf} across ${uni} for long & short ideas — this can take a few seconds…`;
    $("#bot-result").innerHTML = "";
    try {
        const p = new URLSearchParams({ timeframe: tf, universe: uni, limit: "25", min_conviction: conv, strict: String(strict) });
        renderIdeas(await api(`/api/v1/bots/ideas?${p}`));
        status.textContent = "";
    } catch (e) {
        status.className = "status-line error";
        status.textContent = `Ideas scan failed: ${e.message}`;
    }
}

// Green/amber/red "N min old" badge from an ISO timestamp — so a signal's data age is visible.
function freshnessBadge(iso, label = "data") {
    if (!iso) return "";
    const ms = Date.now() - Date.parse(iso);
    if (!isFinite(ms) || ms < 0) return "";
    const min = ms / 60000;
    const cls = min < 5 ? "fresh-ok" : min < 30 ? "fresh-warn" : "fresh-stale";
    const age = min < 1 ? "just now" : min < 60 ? `${Math.round(min)}m old` : `${Math.round(min / 60)}h old`;
    return `<span class="fresh-badge ${cls}" title="${esc(label)} as of ${new Date(iso).toLocaleTimeString()}">● ${age}</span>`;
}

// Inline SVG sparkline of recent closes with entry/stop/target guide lines drawn over it.
function sparkSvg(spark, entry, stop, target, side) {
    if (!spark || spark.length < 2) return `<div class="muted small">no chart data</div>`;
    const w = 260, h = 60, pad = 4;
    const extra = [entry, stop, target].filter((v) => v != null);
    const min = Math.min(...spark, ...extra), max = Math.max(...spark, ...extra), rng = (max - min) || 1;
    const X = (i) => pad + i * (w - 2 * pad) / (spark.length - 1);
    const Y = (v) => h - pad - (v - min) / rng * (h - 2 * pad);
    const pts = spark.map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(" ");
    const hline = (v, cls) => v == null ? "" : `<line x1="0" x2="${w}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}" class="${cls}"/>`;
    return `<svg class="spark" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" preserveAspectRatio="none">
        ${hline(target, "spark-target")}${hline(stop, "spark-stop")}${hline(entry, "spark-entry")}
        <polyline points="${pts}" class="spark-line ${side === "long" ? "up" : "down"}"/></svg>`;
}

// The expandable "why now" panel for one intraday idea: sparkline + agreeing-bot reasons + what
// invalidates it + a one-click "paper trade" that opens the exact plan as a paper position.
function intradayWhyCard(i, source) {
    const reasons = (i.signals || []).map((s) => `<li><b>${esc(s.bot)}</b>: ${esc(s.reason || "")}${s.r_multiple != null ? ` <span class="muted">(${s.r_multiple}R)</span>` : ""}</li>`).join("");
    const inval = i.side === "long"
        ? `Invalidated below <b class="neg">${botPrice(i.stop)}</b> (stop)`
        : `Invalidated above <b class="neg">${botPrice(i.stop)}</b> (stop)`;
    const sz = (i.risk_pct != null && i.alloc_pct != null) ? ` · size <b>${i.risk_pct}%</b> risk → ${i.alloc_pct}%` : "";
    // Prefer the sample-size-aware (Wilson) win rate when the shortlist scored this idea.
    const win = i.conf_win_pct != null ? `, ~${i.conf_win_pct}% conf. win`
        : i.est_win_pct != null ? `, ~${i.est_win_pct}% measured win` : "";
    const sv = i.sentiment;
    const sentLine = sv ? `<div class="small muted">Live sentiment — <span class="${sv.stance === "supportive" ? "pos" : sv.stance === "contradicting" ? "neg" : ""}">${esc(sv.stance || "neutral")}</span>${sv.consensus ? ` · news ${esc(sv.consensus)}${sv.news_score != null ? ` (${sv.news_score})` : ""}` : ""}${sv.alignment != null ? ` · align ${sv.alignment > 0 ? "+" : ""}${sv.alignment}` : ""} <span title="How much live news + the market Fear & Greed gauge support THIS direction; contradicting sentiment softly down-ranks an idea.">ⓘ</span></div>` : "";
    // Intraday ideas come from the crypto bot universe → paper-trade against the CRYPTO account,
    // filled at the idea's own entry with its stop/target so the position matches the card exactly.
    const paper = i.entry != null
        ? `<button class="idea-paper-btn paper-btn" data-ticker="${esc(i.ticker)}" data-market="CRYPTO" data-side="${esc(i.side || "long")}" data-price="${i.entry}" data-stop="${i.stop != null ? i.stop : ""}" data-target="${i.target != null ? i.target : ""}" data-alloc="${i.alloc_pct != null ? i.alloc_pct : ""}" data-source="${esc(source || "intraday")}" title="Open ${esc(i.ticker)} as a paper position at ${botPrice(i.entry)} (stop ${botPrice(i.stop)}, target ${botPrice(i.target)})">📝 Paper trade</button>`
        : "";
    return `<div class="why-card">
        <div class="why-chart">${sparkSvg(i.spark, i.entry, i.stop, i.target, i.side)}
            <div class="small muted">entry ${botPrice(i.entry)} · <span class="pos">tgt ${botPrice(i.target)}</span> · <span class="neg">stop ${botPrice(i.stop)}</span></div></div>
        <div class="why-text">
            <div class="small"><b>Why now</b> — ${i.conviction} bot${i.conviction === 1 ? "" : "s"} agree ${esc(i.side)}${i.regime ? `, ${esc(i.regime)} regime` : ""}${win}${sz}</div>
            <ul class="why-list small">${reasons || `<li class="muted">${esc(i.reason || "")}</li>`}</ul>
            ${sentLine}
            <div class="small muted">${inval}</div>
            ${paper ? `<div style="margin-top:8px;">${paper}</div>` : ""}
        </div>
    </div>`;
}

// Market-wide crypto Fear & Greed gauge (0 fear … 100 greed) — the macro mood the shortlist
// leans into (long in greed / short in fear, contrarian at the extremes).
function fngGauge(fng) {
    if (!fng || fng.value == null) return "";
    const v = fng.value;
    const color = v <= 24 ? "#e5484d" : v <= 44 ? "#e08c1a" : v <= 55 ? "#8a8f98" : v <= 74 ? "#46a758" : "#30a46c";
    const stale = fng.stale ? " — live feed unavailable, treated as neutral" : "";
    return `<span class="pill" style="border-color:${color}66;" title="Crypto Fear &amp; Greed Index: ${v}/100 — ${esc(fng.classification || "")}${stale}. Market-wide risk mood; the shortlist leans long into greed / short into fear, with a contrarian nudge at the extremes.">
        <span style="color:${color};">●</span> F&amp;G ${v} · ${esc(fng.classification || "")}</span>`;
}

// ＋/－/• chip: does live sentiment (news + Fear & Greed) support THIS idea's side?
function newsChip(s) {
    if (!s) return `<span class="muted small">—</span>`;
    const stance = s.stance || "neutral";
    const sym = stance === "supportive" ? "＋" : stance === "contradicting" ? "－" : "•";
    const cls = stance === "supportive" ? "pos" : stance === "contradicting" ? "neg" : "muted";
    const cons = s.consensus || "no news";
    const tip = `Live sentiment for this side: ${stance}${s.alignment != null ? ` (alignment ${s.alignment > 0 ? "+" : ""}${s.alignment})` : ""}. News: ${esc(cons)}${s.news_score != null ? ` ${s.news_score}/100` : ""}. Blends per-coin news + market Fear & Greed; contradicting sentiment softly down-ranks.`;
    return `<span class="${cls} small" title="${tip}">${sym} ${esc(cons)}</span>`;
}

// Composite selection score (0–1) with a term-by-term tooltip.
function scoreCell(i) {
    const s = i.selection_score;
    if (s == null) return `<span class="muted">—</span>`;
    const t = i.terms || {};
    const tip = `Composite 0–1 — edge ${t.edge ?? "—"} · confidence ${t.confidence ?? "—"} · agreement ${t.agreement ?? "—"} · sentiment ${t.sentiment != null ? (t.sentiment > 0 ? "+" : "") + t.sentiment : "—"}`;
    const cls = s >= 0.66 ? "pos" : s >= 0.5 ? "" : "muted";
    return `<b class="${cls}" title="${esc(tip)}">${s.toFixed(2)}</b>`;
}

function renderIdeas(d) {
    const head = `<div class="row gap-s" style="justify-content:space-between; flex-wrap:wrap; align-items:center;">
        <div><b>Intraday Trade Ideas</b> <span class="muted small">· ${esc(d.timeframe || "")}${d.htf ? ` · HTF ${esc(d.htf)}` : ""} · ${esc(d.universe || "")} · ${(d.bots || []).length} bots${d.strict ? " · edge-backed only" : ""}</span> ${fngGauge(d.fear_greed)} ${freshnessBadge(d.as_of, "scan")}</div>
        <span class="muted small">${esc(d.note || "")}</span></div>`;
    // Edge badge: green "edge" when a positive-measured-edge bot agrees, muted "exp" otherwise.
    const edgeBadge = (i) => {
        if (!d.edge_weighted) return `<span class="muted small">—</span>`;
        const q = i.quality != null ? ` · q ${i.quality > 0 ? "+" : ""}${i.quality}%` : "";
        const wr = i.est_win_pct != null ? ` · ~${i.est_win_pct}% win` : "";
        const title = `${i.regime || ""} regime${q}${wr}`;
        return i.edge_backed
            ? `<span class="pill pill-buy" title="${esc(title)}">edge</span>`
            : `<span class="pill pill-muted" title="${esc(title)} · no positive measured edge — exploratory">exp</span>`;
    };
    const caption = d.edge_weighted
        ? `ranked by measured quality${(d.edge_backed && (d.edge_backed.long + d.edge_backed.short)) ? "" : " · none edge-backed yet"}`
        : "ranked by conviction, then reward:risk";
    // Size cell: capped ¼-Kelly risk-per-trade % → resulting portfolio allocation %.
    const sizeCell = (i) => {
        if (i.risk_pct == null || i.alloc_pct == null) return `<span class="muted">—</span>`;
        return `<span title="Risk ${i.risk_pct}% of equity if stopped → ${i.alloc_pct}% position (${esc(i.size_note || "")})"><b>${i.risk_pct}%</b> <span class="muted">→</span> ${i.alloc_pct}%</span>`;
    };
    const szParams = (d.sizing && d.sizing.params) || {};
    // ★ Top Ideas — the curated best-of shortlist (long + short mixed), the headline output.
    const topSection = () => {
        const arr = d.shortlist || [], sel = d.selection || {}, sz = d.shortlist_sizing || {};
        const sub = `top ${arr.length} of ${sel.considered != null ? sel.considered : "—"} candidates`
            + (sel.sentiment ? " · sentiment-weighted" : " · sentiment off")
            + (sz && sz.sized ? ` · <b title="Total ¼-Kelly-capped risk across the shortlist">${sz.total_risk_pct}% risk</b> / ${sz.total_alloc_pct}% allocated` : "");
        if (!arr.length)
            return `<div class="muted small" style="margin-top:6px;">★ <b>Top Ideas</b> — nothing cleared the bar right now${d.strict ? " (strict / stand aside)" : ""}.</div>`;
        const rows = arr.map((i, idx) => {
            const wid = `why-top-${idx}`;
            return `<tr class="trk-row idea-top-row" data-ticker="${esc(i.ticker)}" title="Analyze ${esc(i.ticker)}">
                <td class="why-toggle" data-why="${wid}" title="Why now?">▸</td>
                <td class="mono b">${esc(i.ticker)}</td>
                <td>${sidePill(i.side)}</td>
                <td class="mono">${scoreCell(i)}</td>
                <td class="mono small">${i.conf_win_pct != null ? "~" + i.conf_win_pct + "%" : "—"}</td>
                <td class="small">${newsChip(i.sentiment)}</td>
                <td class="mono">${botPrice(i.entry)}</td>
                <td class="mono neg">${botPrice(i.stop)}</td>
                <td class="mono pos">${botPrice(i.target)}</td>
                <td class="mono b">${i.r_multiple != null ? i.r_multiple + "R" : "—"}</td>
                <td class="mono small">${sizeCell(i)}</td>
            </tr>
            <tr id="${wid}" class="idea-why-row hidden"><td colspan="11">${intradayWhyCard(i, "intraday-top-" + (d.timeframe || ""))}</td></tr>`;
        }).join("");
        return `<div style="margin-top:12px;">
            <div class="row gap-s" style="align-items:center; margin-bottom:6px;">
                <b>★ Top Ideas</b><span class="muted small">${sub}</span></div>
            <div class="table-scroll"><table class="lab-table">
                <thead><tr><th title="Expand: why now?"></th><th>Coin</th><th>Side</th>
                    <th title="Composite selection score 0–1: Wilson-adjusted win rate + measured edge + agreement + live sentiment">Score</th>
                    <th title="Sample-size-aware (Wilson) lower-bound win rate — a thin edge is trusted less than a well-measured one">Conf win</th>
                    <th title="Live news + market Fear & Greed alignment with this side">Sentiment</th>
                    <th>Entry</th><th>Stop</th><th>Target</th><th>R</th><th>Size</th></tr></thead>
                <tbody>${rows}</tbody></table></div></div>`;
    };
    const section = (arr, side) => {
        if (!arr || !arr.length)
            return `<div class="muted small" style="margin-top:14px;">${sidePill(side)} <span style="margin-left:6px;">No ${side} ideas ${d.strict ? "with measured edge" : "at this conviction"} on ${esc(d.timeframe || "")}.</span></div>`;
        const sz = (d.sizing || {})[side];
        const bookSize = sz && sz.sized
            ? ` · <b title="Total risk budget across this book, ¼-Kelly capped">${sz.total_risk_pct}% risk</b> / ${sz.total_alloc_pct}% allocated`
            : "";
        const showSize = szParams.enabled !== false;
        const cols = showSize ? 10 : 9;
        const rows = arr.map((i, idx) => {
            const wid = `why-${side}-${idx}`;
            return `<tr class="trk-row" data-ticker="${esc(i.ticker)}" title="Analyze ${esc(i.ticker)}">
            <td class="why-toggle" data-why="${wid}" title="Why now?">▸</td>
            <td class="mono b">${esc(i.ticker)}${i.cluster_peers > 1 ? ` <span class="muted small" title="Best of ${i.cluster_peers} correlated coins in this book — correlated duplicates collapsed so size reflects one independent bet">+${i.cluster_peers - 1}⋯</span>` : ""}</td>
            <td>${convPill(i.conviction)}</td>
            <td>${edgeBadge(i)}</td>
            <td class="mono">${botPrice(i.entry)}</td>
            <td class="mono neg">${botPrice(i.stop)}</td>
            <td class="mono pos">${botPrice(i.target)}</td>
            <td class="mono b">${i.r_multiple != null ? i.r_multiple + "R" : "—"}</td>
            ${showSize ? `<td class="mono small">${sizeCell(i)}</td>` : ""}
            <td class="small muted">${esc((i.bots || []).join(", "))}</td>
        </tr>
        <tr id="${wid}" class="idea-why-row hidden"><td colspan="${cols}">${intradayWhyCard(i, "intraday-" + (d.timeframe || ""))}</td></tr>`;
        }).join("");
        return `<div style="margin-top:14px;">
            <div class="row gap-s" style="align-items:center; margin-bottom:6px;">
                ${sidePill(side)}<span class="muted small">${arr.length} ${side} ${arr.length === 1 ? "idea" : "ideas"} · ${caption}${bookSize}</span></div>
            <div class="table-scroll"><table class="lab-table">
                <thead><tr><th title="Expand: why now?"></th><th>Coin</th><th>Conv</th><th>Edge</th><th>Entry</th><th>Stop</th><th>Target</th><th>R</th>${showSize ? `<th title="Risk-per-trade % → portfolio allocation %, from ${szParams.kelly_fraction != null ? szParams.kelly_fraction + "×" : "¼-"}Kelly (max ${szParams.risk_per_trade_pct != null ? szParams.risk_per_trade_pct + "% risk" : ""}${szParams.max_position_pct != null ? " / " + szParams.max_position_pct + "% position" : ""})">Size</th>` : ""}<th>Agreeing bots</th></tr></thead>
                <tbody>${rows}</tbody></table></div></div>`;
    };
    const nLong = (d.counts && d.counts.long) || 0, nShort = (d.counts && d.counts.short) || 0;
    $("#bot-result").innerHTML = `<div class="card">${head}${topSection()}
        <details style="margin-top:16px;"><summary class="muted small" style="cursor:pointer;">All ideas — full long &amp; short books (${nLong} long / ${nShort} short)</summary>
            ${section(d.long, "long")}${section(d.short, "short")}</details></div>`;
    $("#bot-result").querySelectorAll(".trk-row").forEach((r) => r.addEventListener("click", () => analyzeTicker(r.dataset.ticker)));
    $("#bot-result").querySelectorAll(".why-toggle").forEach((cell) => cell.addEventListener("click", (e) => {
        e.stopPropagation();                                     // don't trigger the row's Analyze
        const why = document.getElementById(cell.dataset.why);
        if (why) cell.textContent = why.classList.toggle("hidden") ? "▸" : "▾";
    }));
    $("#bot-result").querySelectorAll(".paper-btn").forEach((b) => b.addEventListener("click", (e) => {
        e.stopPropagation(); paperTradeIdea(b);                  // open this idea as a paper position
    }));
    $("#bot-disclaimer").textContent = d.disclaimer || "";
}

async function scanBot(bot) {
    const tf = $("#bot-tf").value, uni = $("#bot-universe").value;
    const status = $("#bot-status");
    status.className = "status-line";
    status.textContent = `Scanning ${bot.name} on ${tf} across ${uni} — this can take a few seconds…`;
    $("#bot-result").innerHTML = "";
    try {
        const p = new URLSearchParams({ timeframe: tf, universe: uni, limit: "25" });
        renderScan(await api(`/api/v1/bots/${encodeURIComponent(bot.id)}/scan?${p}`));
        status.textContent = "";
    } catch (e) {
        status.className = "status-line error";
        status.textContent = `Scan failed: ${e.message}`;
    }
}

function renderScan(d) {
    const sig = d.signaling || [];
    const head = `<div class="row gap-s" style="justify-content:space-between; flex-wrap:wrap; align-items:center;">
        <div><b>${esc(d.bot_name || "")}</b> <span class="muted small">· ${esc(d.timeframe || "")}${d.htf ? ` · HTF ${esc(d.htf)}` : ""} · ${esc(d.universe || "")}</span></div>
        <span class="muted small">${esc(d.note || "")}</span></div>`;
    if (!sig.length) {
        $("#bot-result").innerHTML = `<div class="card">${head}<p class="muted" style="margin-top:10px;">No coins signaling long or short right now on ${esc(d.timeframe || "")}. Try another timeframe or bot.</p></div>`;
        $("#bot-disclaimer").textContent = d.disclaimer || "";
        return;
    }
    const rows = sig.map((s) => {
        const ind = s.indicators || {};
        const extra = Object.entries(ind).slice(0, 3).map(([k, v]) => `${esc(k)} ${esc(v)}`).join(" · ");
        return `<tr class="trk-row" data-ticker="${esc(s.ticker)}" title="Analyze ${esc(s.ticker)}">
            <td class="mono b">${esc(s.ticker)}</td>
            <td>${sidePill(s.side)}</td>
            <td class="mono">${botPrice(s.entry)}</td>
            <td class="mono neg">${botPrice(s.stop)}</td>
            <td class="mono pos">${botPrice(s.target)}</td>
            <td class="mono b">${s.r_multiple != null ? s.r_multiple + "R" : "—"}</td>
            <td class="small">${esc(s.reason)}</td>
            <td class="small muted">${extra}</td>
        </tr>`;
    }).join("");
    $("#bot-result").innerHTML = `<div class="card">${head}
        <div class="table-scroll" style="margin-top:10px;"><table class="lab-table">
            <thead><tr><th>Coin</th><th>Side</th><th>Entry</th><th>Stop</th><th>Target</th><th>R</th><th>Signal</th><th>Indicators</th></tr></thead>
            <tbody>${rows}</tbody></table></div></div>`;
    $("#bot-result").querySelectorAll(".trk-row").forEach((r) => r.addEventListener("click", () => analyzeTicker(r.dataset.ticker)));
    $("#bot-disclaimer").textContent = d.disclaimer || "";
}

async function runMomentum() {
    const status = $("#bot-status");
    status.className = "status-line";
    status.textContent = "Running the momentum bot — pricing the universe and checking the BTC regime…";
    $("#bot-result").innerHTML = "";
    try {
        BOT_DATA = await api(`/api/v1/bots/crypto-momentum?${botParams()}`);
        renderBot(BOT_DATA);
        status.textContent = "";
    } catch (e) {
        status.className = "status-line error";
        status.textContent = `Bot failed: ${e.message}`;
    }
}

function renderBot(sig) {
    const on = sig.regime === "risk-on";
    const regimeHtml = `
        <div class="card" style="border-left:3px solid ${on ? "var(--accent)" : "var(--warn)"};">
            <div class="row gap-s" style="align-items:center; justify-content:space-between; flex-wrap:wrap;">
                <div><span class="pill ${on ? "pill-buy" : "pill-hold"}">${on ? "RISK-ON" : "RISK-OFF"}</span>
                    <span class="muted small" style="margin-left:8px;">${esc(sig.regime_detail || "")}</span></div>
                <span class="muted small">Universe: ${esc(sig.universe || "")} · Cash: <b>${fmt((sig.cash_pct || 0) * 100, 0)}%</b></span>
            </div>
        </div>`;

    let aiHtml = "";
    if (sig.ai) {
        const ai = sig.ai;
        const flags = (ai.risks || []).map((f) =>
            `<li class="small"><span class="pill ${SEVERITY_PILL[f.severity] || "pill-muted"}">${esc(f.severity)}</span> <b class="mono">${esc(f.ticker)}</b> — ${esc(f.note)}</li>`).join("");
        aiHtml = `
        <div class="card" style="margin-top:12px;">
            <div class="card-head"><h3 style="font-size:15px;">🧠 AI risk review <span class="muted small">(${esc(ai.llm_mode || "")})</span></h3>
                <span class="pill ${CONVICTION_PILL[ai.conviction] || "pill-muted"}">${esc((ai.conviction || "").toUpperCase())}</span></div>
            <p class="small" style="margin:6px 0;">${esc(ai.briefing || "")}</p>
            ${flags ? `<ul style="margin:8px 0 0; padding-left:18px;">${flags}</ul>` : `<p class="muted small">No specific risk flags.</p>`}
        </div>`;
    }

    const applied = sig.ai_applied
        ? `<p class="muted small" style="margin-top:8px;">⚠️ ${esc(sig.ai_applied.note)} Trimmed: ${(sig.ai_applied.trimmed || []).map(esc).join(", ")}.</p>` : "";

    let basketHtml;
    if (!sig.basket || !sig.basket.length) {
        basketHtml = `<div class="card" style="margin-top:12px;"><p class="muted">${esc(sig.note || "No holdings — the bot is in cash.")}</p></div>`;
    } else {
        const maxW = Math.max(...sig.basket.map((b) => b.weight));
        const rows = sig.basket.map((b) => {
            const bar = maxW > 0 ? (b.weight / maxW) * 100 : 0;
            return `<tr>
                <td class="mono b">${esc(b.ticker)}</td>
                <td class="mono b" style="color:var(--accent)">${fmt(b.weight * 100, 1)}%
                    <span class="pf-bar-track" style="display:inline-block;width:56px;vertical-align:middle;margin-left:6px;"><span class="pf-bar-fill" style="width:${bar.toFixed(0)}%"></span></span></td>
                <td class="mono">${fmt(b.momentum_score)}</td>
                <td class="mono ${b.return_30d_pct >= 0 ? "pos" : "neg"}">${b.return_30d_pct >= 0 ? "+" : ""}${fmt(b.return_30d_pct)}%</td>
                <td class="mono ${b.return_90d_pct >= 0 ? "pos" : "neg"}">${b.return_90d_pct >= 0 ? "+" : ""}${fmt(b.return_90d_pct)}%</td>
                <td class="mono">${fmt(b.volatility_pct)}%</td>
                <td class="mono">${botPrice(b.price)}</td>
                <td class="mono neg">${botPrice(b.stop_price)}</td>
            </tr>`;
        }).join("");
        basketHtml = `<div class="card" style="margin-top:12px;">
            <div class="muted small" style="margin-bottom:8px;">${esc(sig.note || "")}</div>
            <div class="table-scroll"><table class="lab-table">
                <thead><tr><th>Coin</th><th>Weight</th><th>Momentum</th><th>30d</th><th>90d</th><th>Vol</th><th>Entry</th><th>Stop</th></tr></thead>
                <tbody>${rows}</tbody></table></div>${applied}
        </div>`;
    }

    $("#bot-result").innerHTML = regimeHtml + aiHtml + basketHtml;
    $("#bot-disclaimer").textContent = sig.disclaimer || "";
}

async function deployBot() {
    const btn = $("#btn-bot-deploy");
    btn.disabled = true;
    btn.textContent = "Deploying…";
    try {
        const r = await api(`/api/v1/bots/crypto-momentum/deploy?${botParams()}`, { method: "POST" });
        const nav = r.snapshot ? ` · NAV ${fmt(r.snapshot.nav, 3)}` : "";
        toast(`Deployed "Crypto Momentum Bot" as a portfolio${nav} — open it in the Portfolio tab.`);
    } catch (e) {
        toast(e.message, true);
    } finally {
        btn.disabled = false;
        btn.textContent = "Deploy ⤓";
    }
}

// ── Intraday bot edge (backtest) ─────────────────────────────────
let EDGE_DATA = null, EDGE_REGIME = "all", EDGE_POLL = null;

async function toggleEdge() {
    const el = $("#bot-edge");
    if (el.style.display === "none") { el.style.display = ""; await loadEdge(); }
    else { el.style.display = "none"; }
}

async function loadEdge() {
    try { EDGE_DATA = await api("/api/v1/bots/backtest/edge"); }
    catch (e) { EDGE_DATA = { bots: {}, note: e.message }; }
    renderEdge();
}

function edgeControls() {
    const running = !!EDGE_POLL;
    return `<div class="row gap-s" style="align-items:center; flex-wrap:wrap;">
        <button id="btn-run-edge" class="btn btn-primary" ${running ? "disabled" : ""}>${running ? "Running…" : "Run backtest →"}</button>
        <select id="edge-regime" class="input input-slim" title="Regime slice"><option value="all">All regimes</option><option value="trend">Trend</option><option value="range">Range</option></select>
        <span id="edge-run-status" class="muted small"></span></div>`;
}

function renderEdge() {
    const d = EDGE_DATA || {}, bots = d.bots || {}, cov = d.coverage || {};
    const keys = Object.keys(bots);
    const head = `<div class="card"><div class="row gap-s" style="justify-content:space-between; align-items:center; flex-wrap:wrap;">
        <div><b>Bot edge — backtested</b> <span class="muted small">${d.as_of ? "· " + new Date(d.as_of).toLocaleString() : ""}${cov.coins ? ` · ${cov.coins} coins × ${(cov.timeframes || []).join("/")} × ${cov.bars} bars` : ""}</span></div>
        ${edgeControls()}</div>`;
    if (!keys.length) {
        $("#bot-edge").innerHTML = head + `<p class="muted" style="margin-top:12px;">${esc(d.note || "No intraday backtest has been run yet.")} Running it measures each bot's real win-rate & expectancy on closed bars, then re-weights Trade Ideas conviction by that edge.</p></div>`;
        return wireEdgeControls();
    }
    const rows = keys.map((k) => {
        const [name, tf] = k.split("|");
        let s = bots[k];
        if (EDGE_REGIME !== "all") s = (s.by_regime || {})[EDGE_REGIME] || { trades: 0 };
        return { name, tf, s, exp: s.expectancy_pct == null ? -999 : s.expectancy_pct };
    }).sort((a, b) => b.exp - a.exp).map(({ name, tf, s }) => {
        const cls = s.expectancy_pct > 0 ? "pos" : s.expectancy_pct < 0 ? "neg" : "";
        const mix = s.trades ? `${s.target_exits}/${s.stop_exits}/${s.time_exits}` : "—";
        return `<tr><td class="b">${esc(name)}</td><td class="mono">${esc(tf)}</td>
            <td class="mono">${s.trades || 0}${s.trades && !s.enough ? '<span class="muted" title="small sample">*</span>' : ""}</td>
            <td class="mono">${s.trades ? s.win_rate + "%" : "—"}</td>
            <td class="mono b ${cls}">${s.trades ? (s.expectancy_pct > 0 ? "+" : "") + s.expectancy_pct + "%" : "—"}</td>
            <td class="mono">${s.trades ? s.profit_factor : "—"}</td>
            <td class="mono small muted" title="target / stop / time exits">${mix}</td></tr>`;
    }).join("");
    $("#bot-edge").innerHTML = head + `<div class="table-scroll" style="margin-top:12px;"><table class="lab-table">
        <thead><tr><th>Bot</th><th>TF</th><th>Trades</th><th>Win</th><th>Expectancy</th><th>PF</th><th>Exits t/s/⏱</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
        <p class="muted small" style="margin-top:8px;">Expectancy = avg net return per trade after ${d.params ? d.params.cost_bps : 20}bps costs; triple-barrier (target/stop/time) on closed bars. * = small sample. Trade Ideas now rank by this edge.</p></div>`;
    wireEdgeControls();
}

function wireEdgeControls() {
    const btn = $("#btn-run-edge"); if (btn) btn.addEventListener("click", runBacktest);
    const sel = $("#edge-regime"); if (sel) { sel.value = EDGE_REGIME; sel.addEventListener("change", (e) => { EDGE_REGIME = e.target.value; renderEdge(); }); }
}

async function runBacktest() {
    const tf = $("#bot-tf").value || "15m", uni = $("#bot-universe").value || "Top 15";
    const st = $("#edge-run-status");
    try {
        await api(`/api/v1/bots/backtest/run?timeframes=${encodeURIComponent(tf)}&universe=${encodeURIComponent(uni)}`, { method: "POST" });
    } catch (e) { if (st) st.textContent = "Could not start: " + e.message; return; }
    const btn = $("#btn-run-edge"); if (btn) { btn.disabled = true; btn.textContent = "Running…"; }
    if (EDGE_POLL) clearInterval(EDGE_POLL);
    EDGE_POLL = setInterval(async () => {
        let s; try { s = await api("/api/v1/bots/backtest/status"); } catch { return; }
        const j = s.job || {};
        const stEl = $("#edge-run-status");
        if (stEl) stEl.textContent = `${j.stage || ""} ${Math.round(j.progress_pct || 0)}% ${j.detail || ""}`;
        if (!j.running) {
            clearInterval(EDGE_POLL); EDGE_POLL = null;
            await loadEdge();
            toast("Intraday backtest complete — Trade Ideas now weighted by measured edge.");
        }
    }, 3000);
}

$("#btn-bot-run").addEventListener("click", runBot);
$("#btn-bot-deploy").addEventListener("click", deployBot);
$("#btn-bot-edge").addEventListener("click", toggleEdge);

// ── Alerts (in-app notifications) + Watchlist + Digest ───────────────

let ALERTS_SEEN = new Set();     // alert ids already surfaced as a browser notification
let NOTIF_OPEN = false;
let ALERTS_POLL = null;

function timeAgo(iso) {
    const t = Date.parse(iso);
    if (!t) return "";
    const s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    return Math.floor(s / 86400) + "d ago";
}

async function refreshAlerts() {
    try {
        const d = await api(withMarket("/api/v1/alerts?limit=30"));
        renderAlerts(d);
        maybeNotify(d.alerts || []);
    } catch { /* best-effort — never break the page over alerts */ }
}

function renderAlerts(d) {
    const n = d.unread || 0;
    const badge = $("#notif-badge");
    if (badge) { badge.textContent = n > 99 ? "99+" : String(n); badge.classList.toggle("hidden", n === 0); }
    const list = $("#notif-list");
    if (!list) return;
    const alerts = d.alerts || [];
    if (!alerts.length) { list.innerHTML = `<p class="muted small">No alerts yet.</p>`; return; }
    list.innerHTML = alerts.map((a) => `
        <div class="notif-item ${a.read ? "" : "unread"} sev-${esc(a.severity || "info")}" ${a.ticker ? `data-ticker="${esc(a.ticker)}"` : ""}>
            <div class="ni-title">${esc(a.title || "")}</div>
            <div class="ni-body">${esc(a.body || "")}</div>
            <div class="ni-time">${esc(timeAgo(a.created_at))}</div>
        </div>`).join("");
    list.querySelectorAll(".notif-item[data-ticker]").forEach((el) =>
        el.addEventListener("click", () => { closeNotif(); analyzeTicker(el.dataset.ticker); }));
}

function maybeNotify(alerts) {
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    for (const a of alerts) {
        if (a.read || ALERTS_SEEN.has(a.id)) continue;
        ALERTS_SEEN.add(a.id);
        try { new Notification(a.title || "StockSense alert", { body: a.body || "" }); } catch { /* ignore */ }
    }
}

function openNotif() {
    $("#notif-panel").classList.remove("hidden");
    $("#notif-bell").setAttribute("aria-expanded", "true");
    NOTIF_OPEN = true;
    if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
}
function closeNotif() {
    $("#notif-panel").classList.add("hidden");
    $("#notif-bell").setAttribute("aria-expanded", "false");
    NOTIF_OPEN = false;
}

function startAlertsPolling() {
    refreshAlerts();
    if (ALERTS_POLL) clearInterval(ALERTS_POLL);
    ALERTS_POLL = setInterval(refreshAlerts, 60000);   // in-app poll; server evaluates on its own loop
}

{
    const bell = $("#notif-bell");
    if (bell) bell.addEventListener("click", (e) => { e.stopPropagation(); NOTIF_OPEN ? closeNotif() : openNotif(); });
    document.addEventListener("click", (e) => { if (NOTIF_OPEN && !e.target.closest("#notif")) closeNotif(); });
    const readBtn = $("#notif-read");
    if (readBtn) readBtn.addEventListener("click", async () => {
        try { await api("/api/v1/alerts/read", { method: "POST", body: JSON.stringify({ market: MARKET.key }) }); } catch { /* ignore */ }
        refreshAlerts();
    });
    const clearBtn = $("#notif-clear");
    if (clearBtn) clearBtn.addEventListener("click", async () => {
        try { await api(withMarket("/api/v1/alerts"), { method: "DELETE" }); } catch { /* ignore */ }
        ALERTS_SEEN = new Set();
        refreshAlerts();
    });
}

async function loadWatchlist() {
    await Promise.all([renderWatchlist(), loadDigest()]);
}

async function renderWatchlist() {
    const box = $("#wl-list");
    if (!box) return;
    box.innerHTML = `<p class="muted">Loading…</p>`;
    try {
        const { items } = await api(withMarket("/api/v1/watchlist"));
        if (!items.length) { box.innerHTML = `<p class="muted">No tickers yet — add one above.</p>`; return; }
        const rows = items.map((w) => `<tr>
            <td class="mono b trk-row" data-ticker="${esc(w.ticker)}" title="Analyze ${esc(w.ticker)}">${esc(w.ticker)}</td>
            <td class="mono">${w.level_above != null ? "≥ " + esc(w.level_above) : "—"}</td>
            <td class="mono">${w.level_below != null ? "≤ " + esc(w.level_below) : "—"}</td>
            <td class="small muted">${esc(w.note || "")}</td>
            <td><button class="btn btn-ghost btn-s wl-del" data-id="${esc(w.id)}">Remove</button></td>
        </tr>`).join("");
        box.innerHTML = `<table class="lab-table"><thead><tr><th>Ticker</th><th>Above</th><th>Below</th><th>Note</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
        box.querySelectorAll(".wl-del").forEach((b) => b.addEventListener("click", async (e) => {
            e.stopPropagation();
            try { await api(`/api/v1/watchlist/${encodeURIComponent(b.dataset.id)}`, { method: "DELETE" }); } catch (err) { toast(err.message, true); }
            renderWatchlist();
        }));
        box.querySelectorAll(".trk-row").forEach((el) => el.addEventListener("click", () => analyzeTicker(el.dataset.ticker)));
    } catch (e) {
        box.innerHTML = `<p class="muted">Could not load watchlist: ${esc(e.message)}</p>`;
    }
}

async function addWatch() {
    const ticker = ($("#wl-ticker").value || "").trim();
    if (!ticker) { toast("Enter a ticker", true); return; }
    const body = {
        ticker, market: MARKET.key,
        note: ($("#wl-note").value || "").trim(),
        level_above: $("#wl-above").value ? Number($("#wl-above").value) : null,
        level_below: $("#wl-below").value ? Number($("#wl-below").value) : null,
    };
    try {
        await api("/api/v1/watchlist", { method: "POST", body: JSON.stringify(body) });
        $("#wl-ticker").value = $("#wl-above").value = $("#wl-below").value = $("#wl-note").value = "";
        toast(`Added ${ticker.toUpperCase()} to watchlist`);
        renderWatchlist();
    } catch (e) { toast(e.message, true); }
}

async function loadDigest() {
    const box = $("#digest-body");
    if (!box) return;
    try {
        const d = await api(withMarket("/api/v1/digest"));
        const c = d.closed || {};
        const hrs = d.since ? Math.round((Date.now() - Date.parse(d.since)) / 3600000) : 24;
        const chips = Object.entries(d.by_type || {}).map(([k, v]) => `<span class="pill pill-muted">${esc(k)}: ${v}</span>`).join(" ")
            || `<span class="muted small">no alerts in the window</span>`;
        box.innerHTML = `
            <div class="row gap-s" style="flex-wrap:wrap;">
                <span class="pill pill-accent">${d.alert_count || 0} alerts / ${hrs}h</span>
                <span class="pill ${(c.wins || 0) > (c.losses || 0) ? "pill-buy" : "pill-muted"}">${c.total || 0} closed · ${c.wins || 0}W / ${c.losses || 0}L</span>
                <span class="pill pill-muted">${d.open_ideas || 0} open ideas</span>
            </div>
            <div style="margin-top:8px;">${chips}</div>`;
    } catch (e) {
        box.innerHTML = `<p class="muted">No digest: ${esc(e.message)}</p>`;
    }
}

{
    const add = $("#wl-add"); if (add) add.addEventListener("click", addWatch);
    const t = $("#wl-ticker"); if (t) t.addEventListener("keydown", (e) => { if (e.key === "Enter") addWatch(); });
    const r = $("#wl-refresh"); if (r) r.addEventListener("click", loadWatchlist);
    const dr = $("#digest-refresh"); if (dr) dr.addEventListener("click", loadDigest);
}

// ── Paper trading tab ────────────────────────────────────────────
const _pnlCls = (v) => v == null ? "" : v >= 0 ? "pos" : "neg";
const _money = (v) => v == null ? "—" : (v < 0 ? "-" : "") + (curSym() || "") + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 2 });

async function loadPaper() {
    const box = $("#paper-summary");
    if (!box) return;
    box.innerHTML = `<p class="muted">Loading…</p>`;
    try {
        try { ADHERENCE_DATA = await api(withMarket("/api/v1/adherence")); } catch { /* mirror is optional */ }
        renderPaper(await api(withMarket("/api/v1/paper")));
    }
    catch (e) { box.innerHTML = `<p class="muted">Could not load paper account: ${esc(e.message)}</p>`; }
}

// Compact discipline line for the Paper tab — paper trades feed the scorecard; the full one is in the Tracker.
function disciplineMirror() {
    const sc = ADHERENCE_DATA && ADHERENCE_DATA.scorecard;
    if (!sc || !sc.n) return "";
    const p = (v) => `${v >= 0 ? "+" : ""}${fmt(v)}%`;
    const d = sc.discipline_delta_pct;
    const msg = d == null ? "" : d < -0.5 ? `<span class="neg">early exits cost ${fmt(Math.abs(d))}%</span>`
        : d > 0.5 ? `<span class="pos">your calls added ${fmt(d)}%</span>` : "on plan";
    return `<p class="muted small" style="margin-top:4px;">🎯 Discipline: plan ${p(sc.plan_return_pct)} vs you ${p(sc.your_return_pct)} — ${msg}. <a href="#/trade-ideas">Full scorecard in the Tracker →</a></p>`;
}

function renderPaper(a) {
    if (!a || a.error) { $("#paper-summary").innerHTML = `<p class="muted">${esc((a && a.error) || "No account")}</p>`; return; }
    const rp = a.return_pct;
    $("#paper-summary").innerHTML = `
        <div class="metrics-grid">
            ${metricCell("Equity", _money(a.equity), "", "Starting balance + realized + unrealized P&L.")}
            ${metricCell("Return", (rp >= 0 ? "+" : "") + fmt(rp) + "%", rp >= 0 ? "pos" : "neg", "Equity vs starting balance.")}
            ${metricCell("Realized P&L", _money(a.realized_pnl), _pnlCls(a.realized_pnl), "Booked profit/loss on closed trades.")}
            ${metricCell("Unrealized P&L", _money(a.unrealized_pnl), _pnlCls(a.unrealized_pnl), "Mark-to-market on open positions.")}
            ${metricCell("Open", fmt(a.open_count, 0), "", "Open positions.")}
            ${metricCell("Win rate", a.win_rate != null ? fmt(a.win_rate) + "%" : "—", "", `${a.wins || 0}W / ${a.losses || 0}L on closed trades.`)}
        </div>
        <p class="muted small" style="margin-top:6px;">Start ${_money(a.start_balance)} · deployed ${_money(a.deployed_notional)} · updated ${a.updated_at ? new Date(a.updated_at).toLocaleString() : "—"}</p>
        ${disciplineMirror()}`;

    const pos = a.open_positions || [];
    $("#paper-positions").innerHTML = pos.length ? `<div class="table-scroll"><table class="lab-table">
        <thead><tr><th>Ticker</th><th>Side</th><th>Qty</th><th>Entry</th><th>Price</th><th>Stop</th><th>Target</th><th>P&L</th><th>Ret</th><th>Source</th><th></th></tr></thead>
        <tbody>${pos.map((p) => `<tr>
            <td class="mono b trk-row" data-ticker="${esc(p.ticker)}" title="Analyze ${esc(p.ticker)}">${esc(p.ticker)}</td>
            <td><span class="pill ${p.side === "long" ? "pill-buy" : "pill-sell"}">${esc(p.side)}</span></td>
            <td class="mono">${fmt(p.qty, 4)}</td><td class="mono">${_money(p.entry_price)}</td>
            <td class="mono">${p.price != null ? _money(p.price) : "—"}</td>
            <td class="mono neg">${p.stop != null ? _money(p.stop) : "—"}</td><td class="mono pos">${p.target != null ? _money(p.target) : "—"}</td>
            <td class="mono ${_pnlCls(p.unrealized_pnl)}">${p.unrealized_pnl != null ? _money(p.unrealized_pnl) : "—"}</td>
            <td class="mono ${_pnlCls(p.return_pct)}">${p.return_pct != null ? fmt(p.return_pct) + "%" : "—"}</td>
            <td class="small muted">${esc(p.source || "")}</td>
            <td><button class="btn btn-ghost btn-s pp-close" data-id="${esc(p.id)}">Close</button></td></tr>`).join("")}</tbody></table></div>`
        : `<p class="muted">No open positions.</p>`;
    $("#paper-positions").querySelectorAll(".pp-close").forEach((b) => b.addEventListener("click", async () => {
        b.disabled = true;
        try { await api(withMarket(`/api/v1/paper/close?position_id=${encodeURIComponent(b.dataset.id)}`), { method: "POST" }); toast("Position closed"); loadPaper(); }
        catch (e) { toast(e.message, true); b.disabled = false; }
    }));
    $("#paper-positions").querySelectorAll(".trk-row").forEach((el) => el.addEventListener("click", () => analyzeTicker(el.dataset.ticker)));

    const attr = a.attribution || [];
    $("#paper-attr").innerHTML = attr.length ? `<div class="table-scroll"><table class="lab-table">
        <thead><tr><th>Source</th><th>Trades</th><th>Win rate</th><th>P&L</th></tr></thead>
        <tbody>${attr.map((x) => `<tr><td>${esc(x.source)}</td><td class="mono">${x.trades}</td><td class="mono">${x.win_rate != null ? fmt(x.win_rate) + "%" : "—"}</td><td class="mono ${_pnlCls(x.pnl)}">${_money(x.pnl)}</td></tr>`).join("")}</tbody></table></div>`
        : `<p class="muted">No closed trades yet.</p>`;

    const closed = (a.closed || []).slice().reverse();
    $("#paper-closed").innerHTML = closed.length ? `<div class="table-scroll"><table class="lab-table">
        <thead><tr><th>Ticker</th><th>Side</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Ret</th><th>Reason</th><th>Source</th></tr></thead>
        <tbody>${closed.map((c) => `<tr><td class="mono b">${esc(c.ticker)}</td><td>${esc(c.side)}</td><td class="mono">${_money(c.entry_price)}</td><td class="mono">${_money(c.exit_price)}</td>
            <td class="mono ${_pnlCls(c.pnl)}">${_money(c.pnl)}</td><td class="mono ${_pnlCls(c.return_pct)}">${fmt(c.return_pct)}%</td><td class="small">${esc(c.reason)}</td><td class="small muted">${esc(c.source || "")}</td></tr>`).join("")}</tbody></table></div>`
        : `<p class="muted">No closed trades yet.</p>`;
}

async function openPaper() {
    const ticker = ($("#pp-ticker").value || "").trim();
    if (!ticker) { toast("Enter a ticker", true); return; }
    const body = {
        ticker, market: MARKET.key, side: $("#pp-side").value,
        alloc_pct: $("#pp-alloc").value ? Number($("#pp-alloc").value) : null,
        stop: $("#pp-stop").value ? Number($("#pp-stop").value) : null,
        target: $("#pp-target").value ? Number($("#pp-target").value) : null, source: "manual",
    };
    try {
        const r = await api("/api/v1/paper/open", { method: "POST", body: JSON.stringify(body) });
        if (r.error) { toast(r.error, true); return; }
        $("#pp-ticker").value = $("#pp-stop").value = $("#pp-target").value = "";
        toast(`Opened ${ticker.toUpperCase()}`);
        renderPaper(r);
    } catch (e) { toast(e.message, true); }
}

// Open a paper position straight from a generated idea card. Reads the plan off the button's
// data-* attributes (ticker/side/price/stop/target/alloc/source/market) so the paper trade
// mirrors exactly what the card shows. Shared by the daily Trade Ideas and intraday Bots cards.
async function paperTradeIdea(btn) {
    const d = btn.dataset;
    const num = (v) => (v != null && v !== "") ? Number(v) : null;
    const orig = btn.innerHTML;
    btn.disabled = true; btn.innerHTML = "…";
    const body = {
        ticker: d.ticker, market: d.market || MARKET.key, side: d.side || "long",
        price: num(d.price), stop: num(d.stop), target: num(d.target),
        alloc_pct: num(d.alloc), source: d.source || "idea",
    };
    try {
        const r = await api("/api/v1/paper/open", { method: "POST", body: JSON.stringify(body) });
        if (r && r.error) { toast(r.error, true); btn.disabled = false; btn.innerHTML = orig; return; }
        btn.innerHTML = "✓ papered"; btn.classList.add("paper-done");
        toast(`📝 Paper: opened ${String(d.ticker).toUpperCase()} ${body.side}${d.market ? " · " + d.market : ""} — see Paper tab`);
    } catch (e) {
        toast(e.message, true); btn.disabled = false; btn.innerHTML = orig;
    }
}

{
    const o = $("#pp-open"); if (o) o.addEventListener("click", openPaper);
    const t = $("#pp-ticker"); if (t) t.addEventListener("keydown", (e) => { if (e.key === "Enter") openPaper(); });
    const r = $("#paper-refresh"); if (r) r.addEventListener("click", loadPaper);
    const rs = $("#paper-reset"); if (rs) rs.addEventListener("click", async () => {
        if (!confirm("Reset the paper account for this market? All positions and history will be cleared.")) return;
        try { await api(withMarket("/api/v1/paper/reset"), { method: "POST" }); toast("Paper account reset"); loadPaper(); }
        catch (e) { toast(e.message, true); }
    });
}

// ── Authentication (frontend gate) ───────────────────────────────
// The whole dashboard sits behind a login. Boot checks the session first and only
// initializes the app once we have a user (or right after a successful login).

const AUTH = { user: null, allowSignup: true };
let AUTH_MODE = "login";   // "login" | "signup"

async function checkAuth() {
    try {
        const { user, allow_signup } = await api("/api/v1/auth/me");
        AUTH.user = user;
        AUTH.allowSignup = allow_signup !== false;
        return true;
    } catch { AUTH.user = null; return false; }
}

function showAuthScreen() {
    const el = $("#auth-screen");
    if (el) el.classList.remove("hidden");
    document.body.style.overflow = "hidden";
    setAuthMode(AUTH.allowSignup ? AUTH_MODE : "login");
    const em = $("#auth-email"); if (em) setTimeout(() => em.focus(), 50);
}
function hideAuthScreen() {
    const el = $("#auth-screen");
    if (el) el.classList.add("hidden");
    document.body.style.overflow = "";
}
function onUnauthorized() {           // session expired mid-session
    AUTH.user = null;
    showAuthScreen();
}

function authError(msg) {
    const el = $("#auth-error");
    if (!el) return;
    el.textContent = msg || "";
    el.classList.toggle("hidden", !msg);
}
function setAuthMode(mode) {
    AUTH_MODE = mode;
    const signup = mode === "signup";
    $("#auth-title").textContent = signup ? "Create your account" : "Welcome back";
    $("#auth-sub").textContent = signup
        ? "Track ideas, portfolios, and paper trades — all yours."
        : "Log in to your StockSense account.";
    $("#auth-submit").textContent = signup ? "Create account" : "Log in";
    $(".auth-name").classList.toggle("hidden", !signup);
    $("#auth-toggle-text").textContent = signup ? "Already have an account?" : "New to StockSense?";
    $("#auth-toggle-btn").textContent = signup ? "Log in" : "Create an account";
    $("#auth-password").setAttribute("autocomplete", signup ? "new-password" : "current-password");
    // Hide the sign-up affordance entirely when the server has registrations closed.
    $(".auth-toggle").style.display = (AUTH.allowSignup || signup) ? "" : "none";
    authError("");
}

async function submitAuth(e) {
    e.preventDefault();
    const email = $("#auth-email").value.trim();
    const password = $("#auth-password").value;
    const name = $("#auth-name-input").value.trim();
    if (!email || !password) { authError("Enter your email and password."); return; }
    if (AUTH_MODE === "signup" && password.length < 8) { authError("Password must be at least 8 characters."); return; }
    const btn = $("#auth-submit"); const orig = btn.textContent;
    btn.disabled = true; btn.textContent = "…";
    try {
        const signup = AUTH_MODE === "signup";
        const { user } = await api(signup ? "/api/v1/auth/signup" : "/api/v1/auth/login", {
            method: "POST",
            body: JSON.stringify(signup ? { email, password, display_name: name } : { email, password }),
        });
        AUTH.user = user;
        $("#auth-password").value = "";
        if (_appStarted) { location.reload(); return; }   // switched account → clean slate
        hideAuthScreen();
        startApp();
    } catch (err) {
        authError(err.message || "Something went wrong. Please try again.");
    } finally {
        btn.disabled = false; btn.textContent = orig;
    }
}

function renderUserMenu() {
    const u = AUTH.user;
    if (!u) return;
    const label = (u.display_name || u.email || "?").trim();
    const initials = label.split(/\s+/).map((s) => s[0]).slice(0, 2).join("").toUpperCase() || "?";
    const av = $("#user-avatar"); if (av) { av.textContent = initials; av.title = label; }
    const nm = $("#um-name"); if (nm) nm.textContent = u.display_name || "Account";
    const ml = $("#um-mail"); if (ml) ml.textContent = u.email || "";
}

// Apply the account's saved personalization (server is source of truth across devices).
function applyUserPrefs() {
    const p = (AUTH.user && AUTH.user.prefs) || {};
    APPEARANCE.accent = ACCENTS[p.accent] ? p.accent : "indigo";
    applyDensity(p.density === "compact" ? "compact" : "comfortable");
    if (p.theme && ["light", "dark", "system"].includes(p.theme)) applyTheme(p.theme, { persist: true });
    else _accentVars(resolvedTheme());   // apply the accent even when the theme is unchanged
}

// ── app start (only after auth) ──────────────────────────────────
let _appStarted = false;
function startApp() {
    if (_appStarted) return;
    _appStarted = true;
    renderUserMenu();
    applyUserPrefs();
    checkHealth();
    startAlertsPolling();
    loadPresets();
    loadMarkets().then(() => {
        // Pick the starting market: a URL ?m= (shared link) wins, else the saved default.
        const m0 = parseHash().params.get("m");
        const pm = AUTH.user && AUTH.user.prefs && AUTH.user.prefs.default_market;
        const startMkt = (m0 && MARKET.byKey[m0]) ? m0 : (MARKET.byKey[pm] ? pm : null);
        if (startMkt) { MARKET.key = startMkt; const s = $("#market-select"); if (s) s.value = startMkt; applyMarketUI(); }
        if (!location.hash) history.replaceState(null, "", buildHash(landingTab()));  // personalized landing
        loadStrategies();
        loadLabLeaderboard();
        applyRoute();     // activate the tab from the URL (defaults to Home) + any deep-links
    });
}

// Wire the auth form + user menu (elements exist — this script is at end of body).
{
    const form = $("#auth-form");
    if (form) form.addEventListener("submit", submitAuth);
    const tog = $("#auth-toggle-btn");
    if (tog) tog.addEventListener("click", () => setAuthMode(AUTH_MODE === "login" ? "signup" : "login"));

    const av = $("#user-avatar"), dd = $("#user-dropdown");
    if (av && dd) {
        av.addEventListener("click", (e) => {
            e.stopPropagation();
            const open = dd.classList.toggle("hidden");
            av.setAttribute("aria-expanded", open ? "false" : "true");
        });
        document.addEventListener("click", (e) => { if (!e.target.closest("#user-menu")) dd.classList.add("hidden"); });
    }
    const lo = $("#um-logout");
    if (lo) lo.addEventListener("click", async () => {
        try { await api("/api/v1/auth/logout", { method: "POST" }); } catch { /* ignore */ }
        location.reload();
    });
    const settings = $("#um-settings");
    if (settings) settings.addEventListener("click", () => {
        if (dd) dd.classList.add("hidden");
        if (typeof openSettings === "function") openSettings();
    });
}

// ── Preferences persistence (server = source of truth across devices) ──
let _prefsTimer = null;
function savePrefs(patch) {
    if (!AUTH.user) return;
    AUTH.user.prefs = { ...(AUTH.user.prefs || {}), ...patch };
    clearTimeout(_prefsTimer);
    _prefsTimer = setTimeout(() => {
        api("/api/v1/auth/me", { method: "PATCH", body: JSON.stringify({ prefs: AUTH.user.prefs }) }).catch(() => { /* offline is fine — localStorage still holds theme */ });
    }, 400);
}
function persistPrefs() { savePrefs({ theme: currentTheme(), accent: APPEARANCE.accent, density: APPEARANCE.density }); }
function landingTab() {
    const t = AUTH.user && AUTH.user.prefs && AUTH.user.prefs.landing_tab;
    return t && TAB_SLUGS[t] ? t : DEFAULT_TAB;
}

// ── Home dashboard ───────────────────────────────────────────────
function homeAction(tab, ico, label) {
    return `<button class="home-action" data-go="${tab}"><span class="ha-ico">${ico}</span>${esc(label)}</button>`;
}
function homeCard(id, title, go, link) {
    return `<div class="home-card" id="${id}"><div class="home-card-head"><h3>${title}</h3>` +
        `<button class="viewlink" data-go="${go}">${link}</button></div>` +
        `<div class="hw-body"><p class="home-empty">Loading…</p></div></div>`;
}
const _hwBody = (id) => $(`#${id} .hw-body`);

function renderHome() {
    const el = $("#home-content");
    if (!el || !AUTH.user) return;
    const name = AUTH.user.display_name || (AUTH.user.email || "").split("@")[0] || "there";
    const hr = new Date().getHours();
    const greet = hr < 12 ? "Good morning" : hr < 18 ? "Good afternoon" : "Good evening";
    const dateStr = new Date().toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
    el.innerHTML = `
      <div class="home-hero">
        <div class="home-greet">${esc(greet)}, <span class="accentname">${esc(name)}</span></div>
        <div class="home-sub">${esc(mkt().label || "Market")} · ${esc(dateStr)}</div>
        <div class="home-actions">
          ${homeAction("screener", "🔎", "Screen stocks")}
          ${homeAction("ideas", "💡", "Trade ideas")}
          ${homeAction("portfolio", "💼", "Portfolios")}
          ${homeAction("paper", "📝", "Paper trade")}
          ${homeAction("analyze", "🔬", "Analyze a stock")}
        </div>
      </div>
      <div class="home-grid">
        ${homeCard("hw-ideas", "💡 Top trade ideas", "ideas", "View all →")}
        ${homeCard("hw-watch", "👁️ Watchlist", "watchlist", "Open →")}
        ${homeCard("hw-paper", "📝 Paper account", "paper", "Open →")}
        ${homeCard("hw-pf", "💼 Portfolios", "portfolio", "Open →")}
      </div>`;
    el.querySelectorAll("[data-go]").forEach((b) => b.addEventListener("click", () => navigate(b.dataset.go)));
    fillHomeIdeas(); fillHomeWatch(); fillHomePaper(); fillHomePortfolios();
}

function _wireGo(box, tab) { box.querySelectorAll("[data-go]").forEach((r) => r.addEventListener("click", () => navigate(tab))); }

async function fillHomeIdeas() {
    const b = _hwBody("hw-ideas"); if (!b) return;
    try {
        const d = await api(withMarket("/api/v1/backtest/ideas"));
        const horizons = d.horizons || ["short", "medium", "long"];
        const all = [];
        for (const h of horizons) for (const c of (d.buckets && d.buckets[h] && d.buckets[h].candidates) || [])
            all.push({ ticker: c.ticker, conviction: c.conviction || 0, win: c.avg_win_rate, horizon: h });
        all.sort((x, y) => (y.conviction || 0) - (x.conviction || 0));
        const top = all.slice(0, 5);
        if (!top.length) { b.innerHTML = `<p class="home-empty">No live ideas yet — run the Strategy Lab to generate some.</p>`; return; }
        b.innerHTML = `<div class="home-list">${top.map((x, i) => `
          <div class="home-li" data-go="ideas"><span class="hli-rank">${i + 1}</span>
          <span class="hli-tk">${esc(x.ticker)}</span>
          <span class="hli-meta">${x.conviction}× · ${x.win != null ? fmt(x.win) + "% win" : esc(x.horizon)}</span></div>`).join("")}</div>`;
        _wireGo(b, "ideas");
    } catch { b.innerHTML = `<p class="home-empty">Ideas unavailable right now.</p>`; }
}

async function fillHomeWatch() {
    const b = _hwBody("hw-watch"); if (!b) return;
    try {
        const items = (await api(withMarket("/api/v1/watchlist"))).items || [];
        if (!items.length) {
            b.innerHTML = `<p class="home-empty">Nothing watched yet. <button class="viewlink" data-go="watchlist">Add a ticker →</button></p>`;
            _wireGo(b, "watchlist"); return;
        }
        b.innerHTML = `<div class="home-list">${items.slice(0, 6).map((w) => {
            const lvl = w.level_above ? "↑ " + pfmt(w.level_above) : w.level_below ? "↓ " + pfmt(w.level_below) : "";
            return `<div class="home-li" data-go="watchlist"><span class="hli-tk">${esc(w.ticker)}</span>
              <span class="hli-meta">${w.note ? esc(w.note) : lvl}</span></div>`;
        }).join("")}</div>`;
        _wireGo(b, "watchlist");
    } catch { b.innerHTML = `<p class="home-empty">Watchlist unavailable.</p>`; }
}

async function fillHomePaper() {
    const b = _hwBody("hw-paper"); if (!b) return;
    try {
        const s = await api(withMarket("/api/v1/paper"));
        const ret = s.return_pct || 0;
        const cls = ret > 0 ? "pos" : ret < 0 ? "neg" : "";
        b.innerHTML = `<div class="home-stat-row">
          <div class="home-stat"><span class="hs-val">${priceFmt(s.equity)}</span><span class="hs-lbl">Equity</span></div>
          <div class="home-stat"><span class="hs-val ${cls}">${ret > 0 ? "+" : ""}${fmt(ret)}%</span><span class="hs-lbl">Return</span></div>
          <div class="home-stat"><span class="hs-val">${s.open_count || 0}</span><span class="hs-lbl">Open</span></div>
          ${s.win_rate != null ? `<div class="home-stat"><span class="hs-val">${fmt(s.win_rate)}%</span><span class="hs-lbl">Win rate</span></div>` : ""}
        </div>`;
    } catch { b.innerHTML = `<p class="home-empty">Paper account unavailable.</p>`; }
}

async function fillHomePortfolios() {
    const b = _hwBody("hw-pf"); if (!b) return;
    try {
        const list = await api("/api/v1/portfolios");
        if (!list.length) {
            b.innerHTML = `<p class="home-empty">No portfolios yet. <button class="viewlink" data-go="portfolio">Build one →</button></p>`;
            _wireGo(b, "portfolio"); return;
        }
        b.innerHTML = `<div class="home-list">${list.slice(0, 5).map((p) => `
          <div class="home-li" data-go="portfolio"><span class="hli-tk" style="font-family:inherit">${esc(p.name)}</span>
          <span class="hli-meta">${(p.positions || []).length} holdings</span></div>`).join("")}</div>`;
        _wireGo(b, "portfolio");
    } catch { b.innerHTML = `<p class="home-empty">Portfolios unavailable.</p>`; }
}

// ── Settings & preferences modal ─────────────────────────────────
function openSettings() {
    if (!AUTH.user) return;
    renderSettings();
    $("#settings-modal").classList.remove("hidden");
    document.body.style.overflow = "hidden";
}
function closeSettings() {
    const m = $("#settings-modal"); if (m) m.classList.add("hidden");
    document.body.style.overflow = "";
}
function renderSettings() {
    const p = AUTH.user.prefs || {};
    $$("#set-theme button").forEach((b) => b.classList.toggle("active", b.dataset.val === currentTheme()));
    $$("#set-density button").forEach((b) => b.classList.toggle("active", b.dataset.val === APPEARANCE.density));
    // Accent swatches (rebuilt each open so the active ring is fresh).
    const ar = $("#set-accent");
    ar.innerHTML = Object.keys(ACCENTS).map((k) =>
        `<span class="accent-dot ${k === APPEARANCE.accent ? "active" : ""}" data-accent="${k}" title="${k}" style="background:${ACCENTS[k].d}"></span>`).join("");
    ar.querySelectorAll(".accent-dot").forEach((dot) => dot.addEventListener("click", () => {
        applyAccent(dot.dataset.accent); persistPrefs();
        ar.querySelectorAll(".accent-dot").forEach((x) => x.classList.toggle("active", x === dot));
    }));
    const sm = $("#set-market");
    sm.innerHTML = (MARKET.list || []).map((m) => `<option value="${esc(m.key)}">${esc(m.label)}</option>`).join("");
    sm.value = p.default_market && MARKET.byKey[p.default_market] ? p.default_market : MARKET.key;
    const sl = $("#set-landing");
    const labels = { home: "Home", screener: "Screener", ideas: "Trade Ideas", watchlist: "Watchlist", portfolio: "Portfolio", paper: "Paper", lab: "Strategy Lab", bots: "Bots", analyze: "Analyze Stock" };
    sl.innerHTML = Object.entries(labels).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
    sl.value = landingTab();
    $("#set-name").value = AUTH.user.display_name || "";
    $("#set-password").value = "";
    $("#set-profile-msg").textContent = "";
}
async function saveProfile() {
    const name = $("#set-name").value.trim();
    const pw = $("#set-password").value;
    const msg = $("#set-profile-msg");
    if (pw && pw.length < 8) { msg.textContent = "Password must be at least 8 characters."; msg.style.color = "var(--neg)"; return; }
    const btn = $("#set-save-profile"); btn.disabled = true;
    try {
        const body = { display_name: name };
        if (pw) body.password = pw;
        const { user } = await api("/api/v1/auth/me", { method: "PATCH", body: JSON.stringify(body) });
        AUTH.user = { ...AUTH.user, ...user };
        renderUserMenu();
        $("#set-password").value = "";
        msg.textContent = "Saved."; msg.style.color = "var(--pos)";
    } catch (e) {
        msg.textContent = e.message || "Could not save."; msg.style.color = "var(--neg)";
    } finally { btn.disabled = false; }
}

// Static settings wiring (elements exist at load; accent dots wired per-open above).
{
    const close = $("#settings-close"); if (close) close.addEventListener("click", closeSettings);
    const modal = $("#settings-modal"); if (modal) modal.addEventListener("click", (e) => { if (e.target === modal) closeSettings(); });
    $$("#set-theme button").forEach((b) => b.addEventListener("click", () => {
        applyTheme(b.dataset.val); persistPrefs();
        $$("#set-theme button").forEach((x) => x.classList.toggle("active", x === b));
    }));
    $$("#set-density button").forEach((b) => b.addEventListener("click", () => {
        applyDensity(b.dataset.val); persistPrefs();
        $$("#set-density button").forEach((x) => x.classList.toggle("active", x === b));
    }));
    const sm = $("#set-market"); if (sm) sm.addEventListener("change", () => savePrefs({ default_market: sm.value }));
    const sl = $("#set-landing"); if (sl) sl.addEventListener("change", () => savePrefs({ landing_tab: sl.value }));
    const sp = $("#set-save-profile"); if (sp) sp.addEventListener("click", saveProfile);
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && $("#settings-modal") && !$("#settings-modal").classList.contains("hidden")) closeSettings();
    });
}

// ── boot ─────────────────────────────────────────────────────────
async function boot() {
    if (await checkAuth()) { hideAuthScreen(); startApp(); }
    else showAuthScreen();
}
boot();
