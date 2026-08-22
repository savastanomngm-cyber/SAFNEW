"""Intraday lead-lag scanner (improvements.txt PART 12).
FIXED: negative-lag slicing bug + NaN-proof lag selection."""
import numpy as np
import pandas as pd
import yfinance as yf
from statsmodels.tsa.stattools import coint, adfuller

def _align_rth(eq, px):
    if eq.index.tz is None:
        return eq, px
    return eq.between_time('09:30', '16:00'), px.between_time('09:30', '16:00')

def _lag_corr(re, rp, lag):
    """Correlation at a given lag. lag>0: equity leads. lag<0: proxy leads."""
    if lag == 0:
        a, b = re, rp
    elif lag > 0:
        a, b = re.iloc[:-lag], rp.iloc[lag:]
    else:
        a, b = re.iloc[-lag:], rp.iloc[:lag]
    common = a.index.intersection(b.index)
    if len(common) < 30:
        return None
    c = a[common].corr(b[common])
    if c is None or np.isnan(c):
        return None
    return float(c)

def lead_lag_scan(equity, proxy, interval="5m", period="60d"):
    try:
        eq = yf.download(equity, interval=interval, period=period,
                         progress=False, prepost=False, threads=False)
        px = yf.download(proxy,  interval=interval, period=period,
                         progress=False, prepost=False, threads=False)
        if isinstance(eq.columns, pd.MultiIndex):
            eq.columns = eq.columns.get_level_values(0)
        if isinstance(px.columns, pd.MultiIndex):
            px.columns = px.columns.get_level_values(0)
        if eq.empty or px.empty or len(eq) < 100 or len(px) < 100:
            return {"error": "insufficient intraday data (need 60d of 5m bars)"}

        eq, px = _align_rth(eq, px)
        re = eq["Close"].pct_change().dropna()
        rp = px["Close"].pct_change().dropna()

        results = {}
        for lag in range(-5, 6):
            c = _lag_corr(re, rp, lag)
            if c is not None:
                results[lag] = c
        if not results:
            return {"error": "could not compute correlations (index misalignment?)"}

        best_lag = max(results, key=lambda k: abs(results[k]))

        eq_c, px_c = eq["Close"].dropna(), px["Close"].dropna()
        common = eq_c.index.intersection(px_c.index)
        eq_c, px_c = eq_c[common], px_c[common]

        spread_stationary = cointegrated = False
        spread_pval = coint_pval = 1.0
        if len(eq_c) > 50:
            try:
                log_eq, log_px = np.log(eq_c), np.log(px_c)
                beta = np.polyfit(log_px, log_eq, 1)[0]
                spread = log_eq - beta * log_px
                spread_pval = adfuller(spread.dropna())[1]
                spread_stationary = spread_pval < 0.05
            except Exception:
                pass
            try:
                coint_pval = coint(eq_c, px_c)[1]
                cointegrated = coint_pval < 0.05
            except Exception:
                pass

        min_overlap_ok = len(re) >= (30 * 78 // 5)
        corr_best = results.get(best_lag, 0.0)

        tradeable = all([
            abs(corr_best) >= 0.15,
            best_lag > 0,
            spread_stationary,
            min_overlap_ok,
        ])

        return {
            "pair": f"{equity}->{proxy}",
            "best_lag_bars": best_lag,
            "corr_at_best": round(corr_best, 4),
            "lag_note": ("equity LEADS proxy" if best_lag > 0 else
                         "proxy LEADS equity (thesis violated)" if best_lag < 0 else "no lead"),
            "min_overlap_ok": min_overlap_ok,
            "spread_stationary": spread_stationary,
            "spread_pval": round(spread_pval, 4),
            "cointegrated": cointegrated,
            "coint_pval": round(coint_pval, 4),
            "tradeable": tradeable,
            "bars": len(re),
        }
    except Exception as e:
        return {"error": str(e)[:150]}