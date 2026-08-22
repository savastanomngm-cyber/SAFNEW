"""SAF v4 — FastAPI research server."""
import json, time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from . import config, store, data
from .security import load_env, clean_text
from .quant import score as S

GROQ_API_KEY = load_env()
store.init()
app = FastAPI(title="Skia Alpha Fund v4", version="4.2.0")

@asynccontextmanager
async def lifespan(_app):
    store.audit_log("server_start", {"ai_key_present": bool(GROQ_API_KEY)})
    yield

app.router.lifespan_context = lifespan
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, lambda r, e: JSONResponse(429, {"detail": f"Rate limit: {e.detail}"}))
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])

def provenance(source: str, asof: str = None, note: str = None) -> dict:
    return {"source": source, "asof": asof, "note": note, "served_at": datetime.now().isoformat(timespec="seconds")}

_PX = {"cache": {}, "built": 0.0}
def _px(ticker: str) -> pd.DataFrame:
    if time.time() - _PX["built"] > 900: _PX["cache"], _PX["built"] = {}, time.time()
    if ticker not in _PX["cache"]: _PX["cache"][ticker] = store.load_prices(ticker)
    return _PX["cache"][ticker]

def _period_return(series: pd.Series, days):
    series = series.dropna()
    if len(series) < 2: return None
    if days == "YTD":
        yp = series[series.index.year == series.index[-1].year]
        if len(yp) < 2: return None
        base = yp.iloc[0]
    else:
        valid = series[series.index >= series.index[-1] - pd.Timedelta(days=days)]
        if len(valid) < 2: return None
        base = valid.iloc[0]
    return round((series.iloc[-1] / base - 1) * 100, 2)

def _flags(raw):
    try:
        parsed = json.loads(raw or "[]")
        return parsed if isinstance(parsed, list) else []
    except Exception: return []

def _f(v):
    try: return None if pd.isna(float(v)) else round(float(v), 4)
    except Exception: return None

@app.get("/api/system/health")
@limiter.limit("30/minute")
def system_health(request: Request):
    cfg = config.load(); bench = cfg["settings"]["benchmark"]; spy = _px(bench)
    return {"status": "ok", "ai_key_present": bool(GROQ_API_KEY), "benchmark": bench,
            "benchmark_bars": int(len(spy)), "benchmark_last": str(spy.index[-1].date()) if not spy.empty else None,
            "baskets": len(cfg["baskets"]), "universe_tickers": len(config.all_tickers(cfg)),
            "audit_chain_ok": store.verify_audit_chain(), "provenance": provenance("live")}

@app.get("/api/settings")
@limiter.limit("30/minute")
def api_settings(request: Request):
    cfg = config.load()
    return {"settings": cfg["settings"], "n_baskets": len(cfg["baskets"]),
            "n_universe": len(config.all_tickers(cfg)), "provenance": provenance("cached")}

@app.get("/api/audit")
@limiter.limit("30/minute")
def api_audit(request: Request):
    return {"events": store.recent_audit(25), "chain_ok": store.verify_audit_chain(), "provenance": provenance("live")}

@app.get("/api/quality")
@limiter.limit("10/minute")
def api_quality(request: Request):
    reports = [data.quality_report(t) for t in config.all_tickers()]
    return {"reports": reports, "provenance": provenance("cached")}

@app.get("/api/baskets")
@limiter.limit("30/minute")
def api_baskets(request: Request):
    cfg = config.load(); out = []
    for b in cfg["baskets"]:
        total_w = sum(b["holdings"].values()) or 1; rets = {}
        for label, days in (("1d", 1), ("1w", 7), ("1m", 30), ("ytd", "YTD")):
            w_ret, count = 0.0, 0
            for t, w in b["holdings"].items():
                px = _px(t)
                if px.empty: continue
                r = _period_return(px["px"], days)
                if r is not None: w_ret += r * (w / total_w); count += 1
            if count: rets[label] = round(w_ret, 2)
        out.append({"name": b["name"], "section": b.get("section", "OTHER"),
                    "timing_class": b.get("timing_class", "hold_only"),
                    "n_holdings": len(b["holdings"]), "returns_pct": rets})
    return {"baskets": out, "provenance": provenance("cached")}

@app.get("/api/basket/{name}")
@limiter.limit("30/minute")
def api_basket(request: Request, name: str):
    cfg = config.load()
    b = next((x for x in cfg["baskets"] if x["name"] == name), None)
    if not b: raise HTTPException(404, f"Basket not found: {name}")
    holdings = []
    for t, w in b["holdings"].items():
        px = _px(t)
        holdings.append({"ticker": t, "weight": w,
                         "price": _f(px["px"].iloc[-1]) if not px.empty else None,
                         "ytd_pct": _period_return(px["px"], "YTD") if not px.empty else None})
    holdings.sort(key=lambda h: h["weight"], reverse=True)
    return {"name": b["name"], "section": b.get("section", ""), "timing_class": b.get("timing_class", "hold_only"),
            "holdings": holdings, "provenance": provenance("cached")}

@app.get("/api/ticker/{t}")
@limiter.limit("30/minute")
def api_ticker(request: Request, t: str):
    t = t.upper().strip(); px = _px(t)
    if px.empty: raise HTTPException(404, f"No data for {t}")
    cfg = config.load(); spy = _px(cfg["settings"]["benchmark"])
    fund = store.get_fundamentals(t); q = data.quality_report(t)
    fund_norm = {"gross_margin": (fund or {}).get("grossMargins"),
                 "oper_margin": (fund or {}).get("operatingMargins"),
                 "returnOnEquity": (fund or {}).get("returnOnEquity")} if fund else None
    s = S.score_v2(t, px.index[-1], {t: px}, spy, fund=fund_norm)
    return {"ticker": t, "price": _f(px["px"].iloc[-1]), "asof": str(px.index[-1].date()),
            "quality": {"usable": q["usable"], "bars": q["bars"], "flags": _flags(q["flags"])},
            "fundamentals": {k: v for k, v in (fund or {}).items() if not k.startswith("_")},
            "score_v2_core": s, "provenance": provenance("cached")}

@app.get("/api/ticker/{t}/chart")
@limiter.limit("30/minute")
def api_chart(request: Request, t: str, bars: int = 252):
    t = t.upper().strip(); px = _px(t)
    if px.empty: raise HTTPException(404, f"No data for {t}")
    tail = px.tail(max(2, min(int(bars), len(px))))
    candles = [{"time": str(idx.date()), "open": _f(r.get("open")), "high": _f(r.get("high")),
                "low": _f(r.get("low")), "close": _f(r.get("close"))} for idx, r in tail.iterrows()]
    return {"ticker": t, "candles": candles, "provenance": provenance("cached")}

@app.get("/api/ticker/{t}/rubric")
@limiter.limit("10/minute")
def api_rubric(request: Request, t: str):
    from .ai import evidence, rubric
    t = t.upper().strip()
    cached = store.get_cached_rubric(t)
    if cached and not request.query_params.get("force"):
        return {"ticker": t, "ok": True, "rubric": cached["raw"], "cached": True,
                "age_days": cached["age_days"], "provenance": provenance("cached")}
    pack = evidence.build_evidence_pack(t)
    if not pack.get("business_desc"): raise HTTPException(404, f"No evidence for {t}")
    result = rubric.score_bottleneck(t, pack)
    ok = "error" not in result
    if ok:
        store.save_rubric(t, result["total"], result)
        return {"ticker": t, "ok": True, "rubric": result, "cached": False, "provenance": provenance("live")}
    return {"ticker": t, "ok": False, "rubric": result, "cached": False, "provenance": provenance("live")}

@app.get("/api/screen")
@limiter.limit("5/minute")
def api_screen(request: Request, top: int = 20):
    cfg = config.load(); bench = cfg["settings"]["benchmark"]; spy = _px(bench)
    if spy.empty: raise HTTPException(503, "Benchmark not fetched")
    upto = spy.index[-1]; rows = []
    for t in config.all_tickers(cfg):
        if t == bench: continue
        px = _px(t)
        if px.empty or len(px) < 250: continue
        fund = store.get_fundamentals(t)
        fund_norm = {"gross_margin": (fund or {}).get("grossMargins"),
                     "oper_margin": (fund or {}).get("operatingMargins"),
                     "returnOnEquity": (fund or {}).get("returnOnEquity")} if fund else None
        s = S.score_v2(t, upto, {t: px}, spy, fund=fund_norm)
        if s: rows.append(s)
    rows.sort(key=lambda r: r["total"], reverse=True)
    return {"asof": str(upto.date()), "n_scored": len(rows), "top": rows[:top], "provenance": provenance("cached")}

@app.get("/api/news")
@limiter.limit("10/minute")
def api_news(request: Request, q: str = ""):
    from .news import feed
    if q:
        items = feed.fetch_news_adhoc(q)
    else:
        cfg = config.load()
        tickers = config.basket_tickers(cfg)[:30]
        items = feed.fetch_news(tickers)
    return {"items": items, "threshold": feed.SIGNAL_THRESHOLD, "provenance": provenance("live")}

@app.get("/api/polymarket")
@limiter.limit("10/minute")
def api_polymarket(request: Request, q: str = "fed rate cut", limit: int = 5):
    from .news import polymarket
    items = polymarket.search_markets(q, limit=limit)
    return {"items": items, "provenance": provenance("live")}

@app.get("/api/positions")
@limiter.limit("30/minute")
def api_positions(request: Request, include_closed: bool = False):
    from .exec import lifecycle
    rows = store.all_positions(limit=50) if include_closed else lifecycle.positions_table()
    return {"positions": rows, "provenance": provenance("cached")}

@app.get("/api/monitor")
@limiter.limit("5/minute")
def api_monitor(request: Request, close: bool = False):
    from .exec import lifecycle
    return lifecycle.run_monitor(auto_close=close)

@app.get("/api/memory")
@limiter.limit("30/minute")
def api_memory(request: Request):
    store.grade_memory()
    return {"decisions": store.recent_memory(50), "provenance": provenance("cached")}

@app.get("/api/scorecard")
@limiter.limit("10/minute")
def api_scorecard(request: Request):
    store.grade_memory()
    return {"scorecard": store.scorecard(), "provenance": provenance("cached")}

@app.get("/api/intraday/{equity}/{proxy}")
@limiter.limit("5/minute")
def api_intraday(request: Request, equity: str, proxy: str, interval: str = "5m", period: str = "60d"):
    from .intraday import leadlag
    res = leadlag.lead_lag_scan(equity.upper(), proxy.upper(), interval=interval, period=period)
    if "error" in res: raise HTTPException(400, res["error"])
    return {"result": res, "provenance": provenance("live")}

@app.post("/api/pipeline/{t}")
@limiter.limit("2/minute")
def api_pipeline(request: Request, t: str):
    from .agents import pipeline
    state = pipeline.run_pipeline(t.upper().strip())
    slim = {"ticker": state["ticker"],
            "analysts": {k: v[:600] for k, v in state["analysts"].items()},
            "bull": state["bull"][:800], "bear": state["bear"][:800],
            "verdict": state["verdict"], "trader": state["trader"],
            "trade": state.get("trade"), "score": state.get("score"),
            "position_opened": state.get("position_opened", False)}
    store.audit_log("api_pipeline", {"ticker": state["ticker"], "action": state["trader"].get("action")})
    return {"state": slim, "provenance": provenance("live")}

_static_dir = Path(__file__).resolve().parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    print("SAF v4 server  → http://127.0.0.1:8000/static/")
    print("API docs       → http://127.0.0.1:8000/docs")
    print("AI key:", "present (server-side)" if GROQ_API_KEY else "MISSING")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")