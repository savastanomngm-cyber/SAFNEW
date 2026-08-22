"""Agent Pipeline v2 (PART 6) + math-engine handoff (PART 7) + position open (PART 8).
- 5-analyst team on REAL data only (prices from SQLite, sanitized headlines)
- Evidence-anchored debate: bull/bear may cite only from the DATA block
- Claim judge grades claims VERIFIED/FABRICATED, not rhetoric
- Judge has one retry; on total failure the debug is surfaced, not swallowed
- Abstain rule: confidence < 0.6 -> HOLD, insufficient conviction
- Trader decides DIRECTION ONLY; saf/exec/sizing.py decides size & exits.
  Judgment proposes, math disposes (PART 16.2).
- On a sized trade (shares > 0), the position is opened in the positions
  table and enters the daily lifecycle monitor.
- Reflection on GRADED memory, not unverified past opinions"""
import json
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from .. import config, store
from ..ai import llm
from ..security import clean_text
from ..quant import score as S

# ── analyst prompts ──────────────────────────────────────────
TECH_SYS = """You are the TECHNICAL ANALYST. Analyze momentum, trend, volatility, support/resistance.
End with 3-5 bullet 'Key Points Summary'. Cite actual numbers."""

NEWS_SYS = """You are the NEWS ANALYST.
When analyzing headlines, cross-reference against these supply-chain signals:
- Congressional trading disclosures, 13F filing season
- Semiconductor bottleneck keywords: CoWoS, ABF substrate, HBM3e
- Geopolitical disruption: sanctions, export controls, freight rerouting
- Physical constraints: lead times, capacity, shortages
Score news impact -1.0 to +1.0. State score first. End with 3-5 bullets."""

SENT_SYS = """You are the SENTIMENT ANALYST. Gauge crowd and geopolitical sentiment.
Look for Shadow Supply Chain signals: 'port seizure', 'dark fleet', 'sanctions evasion',
'precursor shortage', 'export control'. Score -1.0 (supply shock) to +1.0 (clear).
State score first. Under 100 words."""

FUND_SYS = """You are the FUNDAMENTALS ANALYST. Assess profitability, growth, valuation, leverage.
End with under/overvalued assessment."""

GEOPOL_SYS = """You are the GEOPOLITICAL / SHADOW SUPPLY CHAIN ANALYST.
Assess how disruptions create pricing power for LEGITIMATE bottleneck companies.
Score geopolitical risk premium -1.0 (stable) to +1.0 (severe disruption). State score first.
IMPORTANT: A HIGH score (+) means disruption WORSENING = BEARISH broad markets but
BULLISH for physical-bottleneck Shadow Alpha assets. Flag this explicitly. Under 200 words."""

# ── debate v2 prompts ────────────────────────────────────────
BULL_SYS = """You are the BULLISH RESEARCHER. Build the strongest evidence-based case FOR investing.
RULE: cite ONLY numbers and facts present in the DATA block. Any number not in DATA
will be classified FABRICATED by the judge and your side auto-loses. Max 200 words."""

BEAR_SYS = """You are the BEARISH RESEARCHER. Build the strongest evidence-based case AGAINST investing.
RULE: cite ONLY numbers and facts present in the DATA block. Any number not in DATA
will be classified FABRICATED by the judge and your side auto-loses. Max 200 words."""

CLAIM_JUDGE_SYS = """You are the DEBATE FACILITATOR. Extract every factual claim from both sides,
then classify each:
- VERIFIED: number appears in the quant data or evidence quotes
- PLAUSIBLE: consistent with evidence but not directly stated
- FABRICATED: contradicts or absent from all provided data
Return ONLY JSON:
{"bull_verified": N, "bull_fabricated": N, "bear_verified": N,
 "bear_fabricated": N, "winner": "BULL"|"BEAR"|"ABSTAIN",
 "confidence": 0.0-1.0, "rationale": "..."}
A side with >2 FABRICATED claims automatically loses regardless of rhetoric."""

TRADER_SYS = """You are the TRADER. Decide DIRECTION only — position sizing is done by the
math engine, never by you. Reflect on your GRADED past decisions below:
repeat what produced WINs, avoid what produced LOSSes.
Return ONLY JSON: {"action": "BUY"|"SELL"|"HOLD", "confidence": 0.0-1.0, "rationale": "..."}"""


def compute_indicators(ticker):
    px = store.load_prices(ticker)
    if px.empty or len(px) < 50:
        return {"error": "insufficient data"}
    c = px["px"]

    def safe(v):
        try:
            return round(float(v), 4) if pd.notna(v) else None
        except Exception:
            return None

    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14).mean().iloc[-1]
    loss = (-delta.clip(upper=0)).rolling(14).mean().iloc[-1]
    tr = pd.concat([px["high"] - px["low"],
                    (px["high"] - c.shift()).abs(),
                    (px["low"] - c.shift()).abs()], axis=1).max(axis=1)
    return {
        "date": str(c.index[-1].date()), "close": safe(c.iloc[-1]),
        "SMA20": safe(c.rolling(20).mean().iloc[-1]),
        "SMA50": safe(c.rolling(50).mean().iloc[-1]),
        "SMA200": safe(c.rolling(200).mean().iloc[-1]) if len(c) >= 200 else None,
        "RSI14": safe(100 - 100 / (1 + gain / loss)) if loss else None,
        "MACD": safe(c.ewm(span=12).mean().iloc[-1] - c.ewm(span=26).mean().iloc[-1]),
        "ATR14": safe(tr.rolling(14).mean().iloc[-1]),
        "return_20d_%": safe(c.pct_change(20).iloc[-1] * 100),
        "return_60d_%": safe(c.pct_change(60).iloc[-1] * 100) if len(c) > 60 else None,
    }


def get_headlines(ticker, limit=6):
    try:
        import yfinance as yf
        raw = yf.Ticker(ticker).news or []
        out = []
        for n in raw[:limit]:
            content = n.get("content", n) if isinstance(n, dict) else n
            title = content.get("title", "") if isinstance(content, dict) else ""
            if title:
                out.append(clean_text(title, 150))
        return out
    except Exception:
        return []


def run_analysts(ticker, tech, headlines, fund):
    news_txt = "\n".join("- " + h for h in headlines) or "No recent news."
    sector = (fund or {}).get("sector", "N/A")
    industry = (fund or {}).get("industry", "N/A")

    def _tech():
        return llm.complete(TECH_SYS, f"Ticker: {ticker}\nTechnical data:\n{json.dumps(tech, indent=1)}")
    def _news():
        return llm.complete(NEWS_SYS, f"Ticker: {ticker}\nHeadlines:\n{news_txt}")
    def _sent():
        return llm.complete(SENT_SYS, f"Ticker: {ticker}\nHeadlines: {json.dumps(headlines)}", temperature=0.3)
    def _fund():
        return llm.complete(FUND_SYS, f"Ticker: {ticker}\nFundamentals:\n{json.dumps(fund or {}, indent=1, default=str)}")
    def _geopol():
        return llm.complete(GEOPOL_SYS, f"Ticker: {ticker}\nSector: {sector} / {industry}\nHeadlines:\n{news_txt}", temperature=0.3)

    fns = {"technical": _tech, "news": _news, "sentiment": _sent,
           "fundamentals": _fund, "geopolitical": _geopol}
    results = {}
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {k: ex.submit(fn) for k, fn in fns.items()}
        for k, fut in futs.items():
            try:
                raw, _ = fut.result()
                results[k] = raw or "(no output)"
            except Exception as e:
                results[k] = f"Error: {e}"
    return results


def run_debate(analysts, data_block):
    db = json.dumps(data_block, indent=1, default=str)
    reports = "\n".join(f"### {k.upper()} ###\n{v[:800]}" for k, v in analysts.items())

    bull_raw, _ = llm.complete(BULL_SYS,
        f"DATA (only source of truth):\n{db}\n\nANALYST REPORTS:\n{reports}\n\nMake the bullish case.")
    bear_raw, _ = llm.complete(BEAR_SYS,
        f"DATA (only source of truth):\n{db}\n\nANALYST REPORTS:\n{reports}\n\nBULL says: {bull_raw}\n\nRebut.")

    transcript = f"[BULL]\n{bull_raw}\n\n[BEAR]\n{bear_raw}"
    judge_prompt = f"DATA:\n{db}\n\nDEBATE TRANSCRIPT:\n{transcript}"

    verdict, jdebug = llm.complete_json(CLAIM_JUDGE_SYS, judge_prompt)
    if not verdict or "winner" not in verdict:
        verdict, jdebug2 = llm.complete_json(CLAIM_JUDGE_SYS, judge_prompt)
        jdebug = {"first": jdebug, "retry": jdebug2}

    if not verdict or "winner" not in verdict:
        verdict = {"winner": "ABSTAIN", "confidence": 0.0,
                   "bull_verified": 0, "bull_fabricated": 0,
                   "bear_verified": 0, "bear_fabricated": 0,
                   "rationale": "Judge unavailable or returned unparseable output — "
                                "defaulting to caution.",
                   "judge_debug": jdebug}

    if verdict.get("bull_fabricated", 0) > 2 and verdict.get("winner") == "BULL":
        verdict["winner"] = "BEAR"
        verdict["rationale"] = "BULL fabricated >2 claims — auto-loss. " + verdict.get("rationale", "")
    if verdict.get("bear_fabricated", 0) > 2 and verdict.get("winner") == "BEAR":
        verdict["winner"] = "BULL"
        verdict["rationale"] = "BEAR fabricated >2 claims — auto-loss. " + verdict.get("rationale", "")

    if verdict.get("confidence", 0) < 0.6:
        verdict = {**verdict, "winner": "ABSTAIN",
                   "rationale": f"Confidence {verdict.get('confidence')} < 0.6 — "
                                f"insufficient conviction. " + verdict.get("rationale", "")}
    return bull_raw, bear_raw, verdict


def run_trader(ticker, verdict, score, data_block):
    mem_txt = store.graded_memory_text(ticker)
    prompt = (f"Ticker: {ticker}\n"
              f"QUANT SCORE v2: {json.dumps((score or {}).get('components'), default=str)} "
              f"(total {(score or {}).get('total')})\n"
              f"DATA:\n{json.dumps(data_block, indent=1, default=str)}\n"
              f"DEBATE WINNER: {verdict.get('winner')} (confidence {verdict.get('confidence')})\n"
              f"Judge rationale: {str(verdict.get('rationale', ''))[:300]}\n"
              f"YOUR GRADED HISTORY:\n{mem_txt}\n\n"
              f"Make today's DIRECTION decision.")
    res, _ = llm.complete_json(TRADER_SYS, prompt, temperature=0.3)
    if not res or "action" not in res:
        res = {"action": "HOLD", "confidence": 0.0, "rationale": "Trader unavailable."}
    if verdict.get("winner") == "ABSTAIN" and res.get("action") != "HOLD":
        res = {"action": "HOLD", "confidence": min(res.get("confidence", 0), 0.3),
               "rationale": "Judge abstained — signal downgraded to HOLD. " + res.get("rationale", "")}
    return res


def run_pipeline(ticker):
    ticker = ticker.upper().strip()
    cfg = config.load()
    bench = cfg["settings"]["benchmark"]

    tech = compute_indicators(ticker)
    headlines = get_headlines(ticker)
    fund = store.get_fundamentals(ticker)
    px = store.load_prices(ticker)
    spy = store.load_prices(bench)

    score = None
    if not px.empty and not spy.empty:
        fund_norm = {"gross_margin": (fund or {}).get("grossMargins"),
                     "oper_margin": (fund or {}).get("operatingMargins"),
                     "returnOnEquity": (fund or {}).get("returnOnEquity")} if fund else None
        score = S.score_v2(ticker, px.index[-1], {ticker: px}, spy, fund=fund_norm)

    cached = store.get_cached_rubric(ticker)
    if cached:
        rubric = cached["raw"]
        rubric_source = "cached"
        evidence_quotes = [q for q in rubric.get("citations", {}).values()
                           if q and q != "INSUFFICIENT EVIDENCE"]
    else:
        from ..ai import evidence as ev, rubric as rb
        pack = ev.build_evidence_pack(ticker)
        rubric = rb.score_bottleneck(ticker, pack) if pack.get("business_desc") else {}
        if rubric and "error" not in rubric:
            store.save_rubric(ticker, rubric.get("total", 0), rubric)
            rubric_source = "fresh"
        else:
            rubric_source = "unavailable"
        evidence_quotes = pack.get("concentration_hits", [])

    data_block = {
        "quant_score": (score or {}).get("components"),
        "quant_total": (score or {}).get("total"),
        "rubric_scores": rubric.get("scores") if isinstance(rubric, dict) else None,
        "rubric_total": rubric.get("total") if isinstance(rubric, dict) else None,
        "rubric_source": rubric_source,
        "evidence_quotes": evidence_quotes[:6],
        "fundamentals": {k: v for k, v in (fund or {}).items() if not k.startswith("_")},
        "headlines": headlines[:5],
        "technical": tech,
    }

    analysts = run_analysts(ticker, tech, headlines, fund)
    bull_raw, bear_raw, verdict = run_debate(analysts, data_block)
    trader = run_trader(ticker, verdict, score, data_block)

    trade = None
    if trader.get("action") in ("BUY", "SELL"):
        from ..exec import sizing as SZ
        trade = SZ.build_trade(ticker, trader["action"], data_block=data_block)

    # ── PART 8: open a tracked position if the trade was sized ───────
    position_opened = False
    if trade and "sizing" in trade and trade["sizing"].get("shares", 0) > 0:
        s = trade["sizing"]
        e = trade["exits"]
        if "error" not in e:
            target_exit = (pd.Timestamp.now() + pd.Timedelta(days=e["time_stop_days"])).date()
            time_stop = target_exit.isoformat()
            opened_at = pd.Timestamp.now().isoformat(timespec="seconds")
            trail_stop_init = s["close"] - e["trail_dist"] if trade["action"] == "BUY" \
                else s["close"] + e["trail_dist"]
            store.open_position(
                ticker=ticker, action=trade["action"], entry=s["close"],
                shares=s["shares"], account_size=s["account"],
                binding=s["binding_constraint"],
                realized_vol=s["realized_vol"],
                stop=e["stop"], trail=e["trail_dist"], trail_stop=trail_stop_init,
                time_stop_date=time_stop, target_exit_date=time_stop,
                signal_json=json.dumps({
                    "verdict": verdict,
                    "score_total": (score or {}).get("total"),
                    "rubric_total": rubric.get("total") if isinstance(rubric, dict) else None,
                }, default=str))
            position_opened = True
            store.audit_log("position_opened", {
                "ticker": ticker, "action": trade["action"],
                "shares": s["shares"], "notional": s["notional"],
                "binding": s["binding_constraint"]})

    state = {"ticker": ticker, "tech": tech, "analysts": analysts,
             "bull": bull_raw, "bear": bear_raw, "verdict": verdict,
             "trader": trader, "trade": trade, "score": score,
             "data_block": data_block, "position_opened": position_opened}

    pct = trade["sizing"].get("pct_account") if trade and "sizing" in trade else None
    store.save_memory(ticker, trader.get("action"), pct,
                      str(trader.get("rationale", ""))[:200],
                      signal_json=json.dumps({
                          "verdict": verdict,
                          "score_total": (score or {}).get("total"),
                          "rubric_total": rubric.get("total") if isinstance(rubric, dict) else None,
                          "trade": trade,
                      }, default=str))
    store.audit_log("pipeline_run", {"ticker": ticker,
                                     "action": trader.get("action"),
                                     "judge": verdict.get("winner"),
                                     "sized": bool(trade and "sizing" in trade),
                                     "opened": position_opened})
    return state