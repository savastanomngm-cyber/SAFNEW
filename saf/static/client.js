"use strict";
const API = "";
const $ = s => document.querySelector(s);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const clamp = (a, b, v) => Math.max(a, Math.min(b, v));

function fmtPct(v) {
    if (v == null || isNaN(v)) return '<span class="dim">—</span>';
    return v >= 0 ? `<span class="pos">▲ +${v.toFixed(2)}%</span>` : `<span class="neg">▼ ${v.toFixed(2)}%</span>`;
}
function fmtMC(mc) {
    if (mc == null) return "—";
    if (mc >= 1e12) return "$" + (mc / 1e12).toFixed(2) + "T";
    if (mc >= 9e8) return "$" + (mc / 1e9).toFixed(2) + "B";
    if (mc >= 1e6) return "$" + (mc / 1e6).toFixed(1) + "M";
    return "$" + Math.round(mc).toLocaleString();
}
function downloadCSV(name, rows) {
    const csv = rows.map(r => r.map(c => '"' + String(c ?? "").replace(/"/g, '""') + '"').join(",")).join("\n");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    a.download = name; a.click();
}
async function api(path, opts) {
    const r = await fetch(API + path, opts);
    if (!r.ok) throw new Error(path + " → HTTP " + r.status);
    return r.json();
}

const state = { tab: "dash" };

function renderChart(containerId, candles) {
    const el = document.getElementById(containerId);
    if (!el || !candles || !candles.length) return;
    el.innerHTML = '';
    const chart = LightweightCharts.createChart(el, {
        width: el.clientWidth || 800, height: 420,
        layout: { background: { color: '#fbf7ec' }, textColor: '#1a2b40' },
        grid: { vertLines: { color: 'rgba(13,31,51,0.05)' }, horzLines: { color: 'rgba(13,31,51,0.05)' } },
        rightPriceScale: { borderColor: 'rgba(13,31,51,0.14)' },
        timeScale: { borderColor: 'rgba(13,31,51,0.14)', timeVisible: false },
    });
    const series = chart.addCandlestickSeries({
        upColor: '#1d7a4f', downColor: '#a33535', borderVisible: false,
        wickUpColor: '#1d7a4f', wickDownColor: '#a33535',
    });
    series.setData(candles);
    chart.timeScale().fitContent();
}

function openModal(title, html) { $("#mtitle").innerHTML = title; $("#mbody").innerHTML = html; $("#mbg").style.display = "flex"; window.scrollTo(0, 0); }
function closeModal() { $("#mbg").style.display = "none"; }

function openBrand() {
    openModal("🏛️ Skia Alpha Fund — Brand System", `
    <div class="row" style="align-items:flex-end;gap:34px;justify-content:center;padding:10px 0">
        <div style="text-align:center"><div style="font-size:80px;color:var(--gold)">α</div><div class="sub">Emblem</div></div>
        <div style="text-align:center"><div class="serif" style="font-size:48px;font-weight:800;letter-spacing:2px">SAF</div><div class="sub">Logo</div></div>
        <div style="text-align:center"><div class="serif" style="font-size:28px;font-weight:800;letter-spacing:2px">SAF</div><div style="height:2px;background:linear-gradient(90deg,transparent,var(--gold),transparent);margin:5px 0"></div><div style="letter-spacing:3.5px;font-weight:600;font-size:11px;color:var(--navy)">SKIA ALPHA FUND</div><div class="sub">Full lockup</div></div>
    </div>
    <div class="panel"><h3>Design language</h3>Champagne-gold α whose exit stroke <b>becomes the upward arrow</b> — alpha generation on an upward trajectory. Navy = trust, gold = performance, ivory = private-bank paper. Serif typography = old-money lineage.</div>`);
}

async function openBasket(name) {
    openModal("🧺 " + name, `<div class="sub"><span class="loader"></span> opening basket…</div>`);
    try {
        const d = await api("/api/basket/" + encodeURIComponent(name));
        const tot = d.holdings.reduce((a, b) => a + b.weight, 0);
        let rows = "";
        for (const h of d.holdings) {
            rows += `<tr class="clickable" data-ticker="${h.ticker}"><td><b>${h.ticker}</b></td><td class="r">${h.weight.toFixed(1)} <span class="dim">(${Math.round(h.weight / tot * 100)}%)</span></td><td class="r">${h.price != null ? "$" + h.price.toFixed(2) : "—"}</td><td class="r">${fmtPct(h.ytd_pct)}</td></tr>`;
        }
        $("#mbody").innerHTML = `<div class="row" style="margin-bottom:12px"><span class="chip">${d.holdings.length} holdings</span><span class="chip">${d.section}</span></div>
        <div class="tbl"><table><tr><th>Ticker (click for dossier)</th><th class="r">Weight</th><th class="r">Price</th><th class="r">YTD</th></tr>${rows}</table></div>`;
    } catch (e) { $("#mbody").innerHTML = `<div class="panel sell">Error: ${e.message}</div>`; }
}

async function openTicker(t) {
    openModal("🔍 " + t + " — Dossier", `<div class="sub"><span class="loader"></span> compiling dossier…</div>`);
    try {
        const [d, chart, rub] = await Promise.all([
            api("/api/ticker/" + encodeURIComponent(t)),
            api("/api/ticker/" + encodeURIComponent(t) + "/chart?bars=252"),
            api("/api/ticker/" + encodeURIComponent(t) + "/rubric").catch(() => ({ ok: false }))
        ]);
        const sc = d.score_v2_core;
        const f = d.fundamentals || {};
        const inB = []; // TODO: cross-reference baskets if needed

        $("#mbody").innerHTML = `<div class="row" style="margin-bottom:10px">
            <span class="chip">$${d.price}</span>
            <span class="chip">YTD ${fmtPct(sc ? (sc.components.trend / 25 * 100 - 50) : null)}</span>
            <span class="chip">Shadow Alpha <b>${sc ? sc.total : "—"}/100</b></span>
            <span class="prov ${d.provenance.source}">${d.provenance.source}</span>
        </div>
        <div class="panel" style="margin:10px 0"><h3>📈 Price Action</h3><div id="tvbox" style="height:420px;display:flex;align-items:center;justify-content:center;background:#fff;border-radius:10px;border:1px solid var(--line)"><span class="loader"></span>&nbsp;chart loading…</div></div>
        <div class="grid2" style="gap:12px">
            <div class="panel"><h3>Technicals</h3><div class="kv"><b>Bars</b><span>${d.quality.bars}</span><b>Usable</b><span>${d.quality.usable ? "✅" : "❌"}</span><b>Flags</b><span>${d.quality.flags.join(", ") || "none"}</span></div></div>
            <div class="panel"><h3>Fundamentals</h3><div class="kv"><b>Sector</b><span>${f.sector || "—"}</span><b>Market cap</b><span>${fmtMC(f.marketCap)}</span><b>Gross margin</b><span>${f.grossMargins ? (f.grossMargins * 100).toFixed(1) + "%" : "—"}</span></div></div>
        </div>
        ${sc ? `<div class="panel"><h3>Shadow Alpha score v2 breakdown</h3><div class="kv"><b>Trend</b><span>${sc.components.trend.toFixed(1)}/25</span><b>α-Indep</b><span>${sc.components.alpha_indep.toFixed(1)}/30</span><b>RelStr</b><span>${sc.components.rel_strength.toFixed(1)}/20</span><b>Quality</b><span>${sc.components.quality.toFixed(1)}/15</span><b>Bottleneck</b><span>${sc.components.bottleneck_prior.toFixed(1)}/10</span></div></div>` : ""}
        ${rub.ok ? `<div class="panel"><h3>Grounded Bottleneck Rubric (${rub.cached ? "cached" : "live"})</h3><div class="kv"><b>Total</b><span><b>${rub.rubric.total}/30</b></span></div></div>` : ""}
        <div class="panel"><h3>📰 News Wire</h3><div id="mnews"><span class="loader"></span> fetching…</div></div>
        <div class="row"><button class="btn gold" id="mrun">▶ Run 5-Stage Pipeline</button></div>
        <div id="mpipe"></div>`;

        setTimeout(() => renderChart("tvbox", chart.candles), 100);

        api("/api/news?q=" + encodeURIComponent(t)).then(hs => {
            const el = $("#mnews");
            if (el) el.innerHTML = hs.items.length ? hs.items.map(h => `<div style="margin:5px 0">${h.link ? `<a class="nl" href="${h.link}" target="_blank" rel="noopener">${h.title} ↗</a>` : `<b>${h.title}</b>`} <span class="dim">— ${h.source}</span> ${h.signal ? '<span class="sig buy">SIGNAL</span>' : ''}</div>`).join("") : "<span class='dim'>No headlines.</span>";
        });

        $("#mrun").onclick = async () => {
            $("#mrun").disabled = true;
            $("#mpipe").innerHTML = `<div class="panel"><span class="loader"></span> Running real pipeline (60-120s)…</div>`;
            try {
                const res = await api("/api/pipeline/" + encodeURIComponent(t), { method: "POST" });
                renderPipelineStages("mpipe", res.state);
                const a = res.state.trader.action;
                const col = a === "BUY" ? "buy" : a === "SELL" ? "sell" : "hold";
                $("#mpipe").insertAdjacentHTML("beforeend", `<div class="panel ${col}"><h3 style="font-size:18px">FINAL SIGNAL: ${a} · Approved by Math Engine</h3><div class="sub">${res.state.trader.rationale || ""}</div></div>`);
            } catch (e) {
                $("#mpipe").innerHTML = `<div class="panel sell">Pipeline error: ${e.message}</div>`;
            }
            $("#mrun").disabled = false;
        };
    } catch (e) { $("#mbody").innerHTML = `<div class="panel sell">Error: ${e.message}</div>`; }
}

function renderPipelineStages(containerId, state) {
    const c = document.getElementById(containerId);
    if (!c) return;
    const stages = [
        { n: 1, title: "I. ANALYST TEAM", txt: Object.entries(state.analysts).map(([k, v]) => `— ${k.toUpperCase()} —\n${v}`).join("\n\n") },
        { n: 2, title: "II. BULL vs BEAR + JUDGE", txt: `[BULL]\n${state.bull}\n\n[BEAR]\n${state.bear}\n\nVERDICT: ${state.verdict.winner} (conf ${state.verdict.confidence})\n${state.verdict.rationale}` },
        { n: 3, title: "III. TRADER", txt: JSON.stringify(state.trader, null, 1) },
        { n: 4, title: "IV. MATH ENGINE (Risk & Sizing)", txt: state.trade ? JSON.stringify(state.trade.sizing, null, 1) : "HOLD — no position sized." },
        { n: 5, title: "V. VERDICT", txt: `Direction: ${state.trader.action}\nPosition opened: ${state.position_opened}` }
    ];
    c.innerHTML = stages.map(s => `<div class="card stage"><div class="n">${s.n}</div><div class="b"><h3>${s.title}</h3><div class="txt"><pre class="memo">${s.txt}</pre></div></div></div>`).join("");
}

document.addEventListener("click", e => {
    if (e.target.closest("[data-close]") || e.target.id === "mbg") { closeModal(); return; }
    if (e.target.closest("#brand")) { openBrand(); return; }
    const tk = e.target.closest("[data-ticker]"); if (tk) { openTicker(tk.dataset.ticker); return; }
    const bk = e.target.closest("[data-basket]"); if (bk) { openBasket(bk.dataset.basket); return; }
});

$("#nav").addEventListener("click", e => {
    const b = e.target.closest("button"); if (!b) return;
    state.tab = b.dataset.tab;
    document.querySelectorAll("#nav button").forEach(x => x.classList.toggle("on", x === b));
    render();
});

function render() {
    ({ dash: renderDash, news: renderNews, screener: renderScreener, agents: renderAgents, intraday: renderIntraday, positions: renderPositions, memory: renderMemory, diag: renderDiag, settings: renderSettings })[state.tab]();
}

async function renderDash() {
    $("#main").innerHTML = `<div class="wrap"><div class="card"><h1>🦅 Skia Alpha Fund — Dashboard</h1><div class="sub" id="dsub">loading…</div></div><div class="grid2"><div class="card"><h2>📈 All Baskets Performance <span class="dim" style="font:400 11px Inter">(click a basket to open it)</span></h2><div class="tbl" id="dt"></div></div><div class="card"><h2>🏆 YTD Ranking</h2><div class="tbl" id="rt"></div></div></div></div>`;
    try {
        const d = await api("/api/baskets");
        $("#dsub").textContent = new Date().toLocaleString() + " · " + d.baskets.length + " baskets";
        const bySection = {};
        d.baskets.forEach(b => { (bySection[b.section] = bySection[b.section] || []).push(b); });
        let html = `<table><tr><th>Basket</th><th class="r">1D</th><th class="r">1W</th><th class="r">1M</th><th class="r">YTD</th></tr>`;
        const perf = [];
        for (const sec in bySection) {
            html += `<tr class="secrow"><td colspan="5">── ${sec} ──</td></tr>`;
            for (const b of bySection[sec]) {
                html += `<tr class="clickable" data-basket="${b.name}"><td>${b.name}</td><td class="r">${fmtPct(b.returns_pct["1d"])}</td><td class="r">${fmtPct(b.returns_pct["1w"])}</td><td class="r">${fmtPct(b.returns_pct["1m"])}</td><td class="r">${fmtPct(b.returns_pct["ytd"])}</td></tr>`;
                if (b.returns_pct.ytd != null) perf.push([b.name, b.returns_pct.ytd]);
            }
        }
        $("#dt").innerHTML = html + "</table>";
        perf.sort((a, b) => b[1] - a[1]);
        $("#rt").innerHTML = `<table><tr><th class="c">#</th><th>Basket</th><th class="r">YTD</th></tr>` + perf.map((p, i) => `<tr class="clickable" data-basket="${p[0]}"><td class="c" style="color:${i < 5 ? "var(--pos)" : i >= perf.length - 3 ? "var(--neg)" : "inherit"};font-weight:700">${i + 1}</td><td>${p[0]}</td><td class="r">${fmtPct(p[1])}</td></tr>`).join("") + "</table>";
    } catch (e) { $("#dt").innerHTML = `<div class="panel sell">Error: ${e.message}</div>`; }
}

async function renderNews() {
    $("#main").innerHTML = `<div class="wrap"><div class="card"><h1>📰 Shadow Alpha News Wire</h1><div class="sub">Server-side relevance-scored feed · type any asset or theme</div>
    <div class="row"><input id="nq" value="NVDA" placeholder="Ticker or theme…" style="width:260px"><button class="btn gold" id="ngo">Load News</button></div>
    <div id="nwire" style="margin-top:14px"></div></div></div>`;
    const load = async () => {
        const q = $("#nq").value.trim(); if (!q) return;
        $("#nwire").innerHTML = '<div class="panel"><span class="loader"></span> fetching headlines…</div>';
        try {
            const hs = await api("/api/news?q=" + encodeURIComponent(q));
            $("#nwire").innerHTML = hs.items.length ? `<div class="sub">${hs.items.length} headlines for "${q}"</div>` + hs.items.map(h => `<div class="panel">${h.link ? `<a class="nl" href="${h.link}" target="_blank" rel="noopener">${h.title} ↗</a>` : `<b>${h.title}</b>`}<div class="dim">${h.source} · relevance ${h.relevance} ${h.keywords.map(k => `<span class="chip">${k}</span>`).join("")}</div></div>`).join("") : `<div class="panel sell">No headlines found.</div>`;
        } catch (e) { $("#nwire").innerHTML = `<div class="panel sell">Error: ${e.message}</div>`; }
    };
    $("#ngo").onclick = load;
    $("#nq").addEventListener("keydown", e => { if (e.key === "Enter") load(); });
    load();
}

async function renderScreener() {
    $("#main").innerHTML = `<div class="wrap"><div class="card"><h1>🔍 SAF Screener</h1><div class="sub">Score v2 (backtest-validated) · click any row for the dossier</div><div id="out"><span class="loader"></span> scoring universe…</div></div></div>`;
    try {
        const d = await api("/api/screen?top=50");
        let html = `<div class="tbl"><table><tr><th>#</th><th>Ticker</th><th class="r">Total</th><th class="r">Trend</th><th class="r">α-Indep</th><th class="r">RelStr</th><th class="r">Quality</th><th class="r">Bottleneck</th><th class="c">Verdict</th></tr>`;
        d.top.forEach((r, i) => {
            const c = r.components || {};
            html += `<tr class="clickable" data-ticker="${r.ticker}"><td>${i + 1}</td><td><b>${r.ticker}</b></td><td class="r"><b>${r.total.toFixed(1)}</b></td><td class="r">${c.trend.toFixed(0)}</td><td class="r">${c.alpha_indep.toFixed(0)}</td><td class="r">${c.rel_strength.toFixed(0)}</td><td class="r">${c.quality.toFixed(0)}</td><td class="r">${c.bottleneck_prior.toFixed(0)}</td><td class="c"><span class="sig ${r.verdict.toLowerCase()}">${r.verdict}</span></td></tr>`;
        });
        $("#out").innerHTML = html + "</table></div>";
    } catch (e) { $("#out").innerHTML = `<div class="panel sell">Error: ${e.message}</div>`; }
}

async function renderAgents() {
    $("#main").innerHTML = `<div class="wrap"><div class="card"><h1>⚛️ TradingAgents — 5-Stage Firm Simulation</h1><div class="sub">Real data only · server-side Groq · math sizing</div>
    <div class="row"><input id="tk" value="MP" style="width:110px"><button class="btn gold" id="go">▶ Run Pipeline</button></div></div>
    <div id="tl"></div></div>`;
    $("#go").onclick = async () => {
        const t = $("#tk").value.toUpperCase().trim();
        $("#tl").innerHTML = `<div class="panel"><span class="loader"></span> Running real pipeline (60-120s)…</div>`;
        try {
            const res = await api("/api/pipeline/" + encodeURIComponent(t), { method: "POST" });
            $("#tl").innerHTML = "";
            renderPipelineStages("tl", res.state);
        } catch (e) { $("#tl").innerHTML = `<div class="panel sell">Error: ${e.message}</div>`; }
    };
}

async function renderIntraday() {
    $("#main").innerHTML = `<div class="wrap"><div class="card"><h1>⏱️ Intraday Lead-Lag Scanner</h1><div class="sub">Computed stationarity & cointegration gates (PART 12)</div>
    <div class="row"><input id="eq" value="XYL" placeholder="Equity" style="width:100px"><input id="px" value="PHO" placeholder="Proxy" style="width:100px"><button class="btn gold" id="go">Scan</button></div>
    <div id="out" style="margin-top:14px"></div></div></div>`;
    $("#go").onclick = async () => {
        const eq = $("#eq").value.trim(), px = $("#px").value.trim();
        if (!eq || !px) return;
        $("#out").innerHTML = `<div class="panel"><span class="loader"></span> scanning 60d of 5m bars…</div>`;
        try {
            const d = await api(`/api/intraday/${encodeURIComponent(eq)}/${encodeURIComponent(px)}`);
            const r = d.result;
            $("#out").innerHTML = `<div class="panel ${r.tradeable ? "buy" : "sell"}"><h3>TRADEABLE: ${r.tradeable}</h3><div class="kv"><b>Best Lag</b><span>${r.best_lag_bars} bars (${r.lag_note})</span><b>Corr</b><span>${r.corr_at_best}</span><b>Spread Stationary</b><span>${r.spread_stationary ? "✅ p=" + r.spread_pval : "❌ p=" + r.spread_pval}</span><b>Cointegrated</b><span>${r.cointegrated ? "✅ p=" + r.coint_pval : "❌ p=" + r.coint_pval}</span></div></div>`;
        } catch (e) { $("#out").innerHTML = `<div class="panel sell">Error: ${e.message}</div>`; }
    };
}

async function renderPositions() {
    $("#main").innerHTML = `<div class="wrap"><div class="card"><h1>📌 Open Positions</h1><div class="sub">Lifecycle monitored daily</div><div id="out"><span class="loader"></span></div></div></div>`;
    try {
        const d = await api("/api/positions");
        if (!d.positions.length) { $("#out").innerHTML = `<div class="panel">No open positions.</div>`; return; }
        let html = `<table><tr><th>Ticker</th><th class="c">Dir</th><th class="r">Shares</th><th class="r">Entry</th><th class="r">Last</th><th class="r">P/L</th><th class="r">Days</th><th class="r">Stop</th></tr>`;
        d.positions.forEach(p => {
            html += `<tr class="clickable" data-ticker="${p.ticker}"><td><b>${p.ticker}</b></td><td class="c">${p.direction}</td><td class="r">${p.shares}</td><td class="r">$${p.entry.toFixed(2)}</td><td class="r">${p.last_price ? "$" + p.last_price.toFixed(2) : "—"}</td><td class="r">${fmtPct(p.unrealized_pct)}</td><td class="r">${p.days_held || 0}</td><td class="r">${p.stop ? "$" + p.stop.toFixed(2) : "—"}</td></tr>`;
        });
        $("#out").innerHTML = html + "</table>";
    } catch (e) { $("#out").innerHTML = `<div class="panel sell">Error: ${e.message}</div>`; }
}

async function renderMemory() {
    $("#main").innerHTML = `<div class="wrap"><div class="card"><h1>🧠 Trading Memory</h1><div class="sub">Outcome-graded decisions (PART 6)</div><div id="out"><span class="loader"></span></div></div></div>`;
    try {
        const d = await api("/api/memory");
        let html = `<table><tr><th>Date</th><th>Ticker</th><th class="c">Action</th><th class="r">Size %</th><th class="c">Outcome</th><th class="r">Return</th><th>Notes</th></tr>`;
        d.decisions.forEach(m => {
            html += `<tr class="clickable" data-ticker="${m.ticker}"><td>${m.date}</td><td><b>${m.ticker}</b></td><td class="c"><span class="sig ${String(m.action).toLowerCase()}">${m.action}</span></td><td class="r">${m.position_pct || "—"}</td><td class="c"><span class="sig ${(m.outcome || "none").toLowerCase()}">${m.outcome || "PENDING"}</span></td><td class="r">${m.realized_ret != null ? fmtPct(m.realized_ret) : "—"}</td><td>${(m.notes || "").slice(0, 80)}</td></tr>`;
        });
        $("#out").innerHTML = html + "</table>";
    } catch (e) { $("#out").innerHTML = `<div class="panel sell">Error: ${e.message}</div>`; }
}

async function renderDiag() {
    $("#main").innerHTML = `<div class="wrap"><div class="card"><h1>🧪 System Diagnostics</h1><div id="out"><span class="loader"></span></div></div></div>`;
    try {
        const [h, q, a] = await Promise.all([api("/api/system/health"), api("/api/quality"), api("/api/audit")]);
        const bad = q.reports.filter(r => !r.usable);
        $("#out").innerHTML = `<div class="panel"><h3>Health</h3><div class="kv"><b>Status</b><span>${h.status}</span><b>Benchmark</b><span>${h.benchmark} (${h.benchmark_bars} bars)</span><b>Audit chain</b><span>${h.audit_chain_ok ? "✅ intact" : "❌ BROKEN"}</span><b>Universe</b><span>${h.universe_tickers} tickers</span></div></div>
        <div class="panel"><h3>Quality Gate</h3><div class="kv"><b>Usable</b><span>${q.reports.length - bad.length}/${q.reports.length}</span><b>Excluded</b><span>${bad.map(r => r.ticker).join(", ") || "none"}</span></div></div>
        <div class="panel"><h3>Audit Log (last 10)</h3><div class="tbl"><table><tr><th>Time</th><th>Event</th><th>Payload</th></tr>${a.events.slice(0, 10).map(e => `<tr><td>${new Date(e.ts * 1000).toLocaleString()}</td><td>${e.kind}</td><td class="dim">${JSON.stringify(e.payload).slice(0, 60)}</td></tr>`).join("")}</table></div></div>`;
    } catch (e) { $("#out").innerHTML = `<div class="panel sell">Error: ${e.message}</div>`; }
}

async function renderSettings() {
    $("#main").innerHTML = `<div class="wrap"><div class="card"><h1>⚙️ Settings</h1><div class="sub">Read-only surface. Edit saf/universe.yaml and restart server to change.</div><div id="out"><span class="loader"></span></div></div></div>`;
    try {
        const d = await api("/api/settings");
        let html = `<div class="panel"><h3>Configuration</h3><div class="kv">`;
        for (const k in d.settings) html += `<b>${k}</b><span>${typeof d.settings[k] === "object" ? JSON.stringify(d.settings[k]) : d.settings[k]}</span>`;
        html += `</div></div>`;
        $("#out").innerHTML = html;
    } catch (e) { $("#out").innerHTML = `<div class="panel sell">Error: ${e.message}</div>`; }
}

render();