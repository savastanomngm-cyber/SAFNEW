"""
Polymarket calibration readout (improvements.txt PART 11.3).

CALIBRATION ONLY — never a signal generator.
Compares model-implied probabilities (derived from our own data, formulas
disclosed inline) against Polymarket market prices. Where no model-implied
probability exists, the market price is shown as crowd-consensus context.

Read-only: Polymarket public Gamma API. No account. No keys.

Run standalone:
    python -m saf.news.polymarket
    python -m saf.news.polymarket --slug <market-slug>
    python -m saf.news.polymarket --query "fed rate cut"
"""
import argparse
import json
import math

import requests

from .. import store

GAMMA = "https://gamma-api.polymarket.com"
HEADERS = {"User-Agent": "SkiaAlphaFund research contact@example.com"}
TIMEOUT = 12

# ── calibration book ──────────────────────────────────────────
# Each entry: a macro/thesis question to calibrate against the crowd.
# "model" names a function below that derives OUR implied probability
# from our own data. Entries without a model show crowd price only.
CALIBRATIONS = [
    {"name": "US recession (12m)", "query": "recession", "model": "regime_recession"},
    {"name": "Fed policy", "query": "fed rate cut", "model": None},
    {"name": "Geopolitical stress", "query": "sanctions", "model": None},
    {"name": "AI capex cycle", "query": "nvidia", "model": None},
]


# ── Polymarket access (public Gamma API) ──────────────────────
def market_by_slug(slug):
    try:
        r = requests.get(f"{GAMMA}/markets", params={"slug": slug},
                         headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        m = data[0] if isinstance(data, list) and data else None
        return _extract(m) if m else None
    except Exception as e:
        return {"error": str(e)[:120]}


def search_markets(query, limit=5):
    """Best-effort search: public-search first, then event listing."""
    results = []
    try:
        r = requests.get(f"{GAMMA}/public-search",
                         params={"q": query, "limit_per_type": limit},
                         headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json() or {}
        for ev in (data.get("events") or [])[:limit]:
            for m in (ev.get("markets") or []):
                x = _extract(m)
                if x:
                    results.append(x)
        if results:
            return results
    except Exception:
        pass
    try:
        r = requests.get(f"{GAMMA}/events",
                         params={"closed": "false", "limit": 25,
                                 "order": "volume24hr", "ascending": "false"},
                         headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        for ev in (r.json() or []):
            if query.lower() in (ev.get("title") or "").lower():
                for m in (ev.get("markets") or []):
                    x = _extract(m)
                    if x:
                        results.append(x)
    except Exception as e:
        if not results:
            return [{"error": str(e)[:120]}]
    return results


def _extract(m):
    try:
        outcomes = json.loads(m.get("outcomes") or "[]")
        prices = [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
    except Exception:
        return None
    if not outcomes or len(outcomes) != len(prices):
        return None
    yes = next((p for o, p in zip(outcomes, prices)
                if str(o).strip().lower() == "yes"), None)
    if yes is None:
        yes = prices[0]
    return {"question": m.get("question"),
            "yes": round(yes, 3),
            "volume": m.get("volumeNum") or m.get("volume"),
            "liquidity": m.get("liquidityNum") or m.get("liquidity"),
            "end": (m.get("endDate") or "")[:10]}


# ── model-implied probabilities (formulas disclosed) ─────────
def model_regime_recession():
    """EST — logistic on SPY 12-month drawdown and 20-day realized vol.
    Disclosed heuristic, not a black box:
        z = (-drawdown - 0.10) * 4 + (vol - 0.16) * 6
        p = 1 / (1 + e^-z)
    """
    spy = store.load_prices("SPY")
    if spy.empty or len(spy) < 260:
        return None
    px = spy["px"]
    dd = float(px.iloc[-1] / px.rolling(252).max().iloc[-1] - 1)
    vol = float(px.pct_change().tail(20).std() * math.sqrt(252))
    z = (-dd - 0.10) * 4 + (vol - 0.16) * 6
    return round(1 / (1 + math.exp(-z)), 3)


MODELS = {"regime_recession": model_regime_recession}


def divergence(model_p, market_p):
    if model_p is None or market_p is None:
        return None, "no model — crowd context only"
    gap = round(model_p - market_p, 3)
    if abs(gap) > 0.15:
        return gap, "DIVERGENCE — model vs crowd (research, never a trigger)"
    return gap, "aligned with consensus"


def calibration_rows():
    store.init()
    rows = []
    for cal in CALIBRATIONS:
        found = search_markets(cal["query"], limit=3)
        found = [f for f in found if f and "error" not in f]
        if not found:
            rows.append({"name": cal["name"], "market": None,
                         "note": "no matching market found — verify query/slug"})
            continue
        best = max(found, key=lambda f: float(f.get("volume") or 0))
        model_p = MODELS[cal["model"]]() if cal.get("model") else None
        gap, interp = divergence(model_p, best["yes"])
        rows.append({"name": cal["name"], "market": best,
                     "model_p": model_p, "gap": gap, "interpretation": interp})
    return rows


def main():
    ap = argparse.ArgumentParser(description="Polymarket calibration readout")
    ap.add_argument("--slug", help="fetch one specific market by slug")
    ap.add_argument("--query", help="ad-hoc search query")
    args = ap.parse_args()

    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich import box
    console = Console()
    console.print(Panel.fit(
        "[bold magenta]🎯 POLYMARKET CALIBRATION READOUT[/bold magenta]\n"
        "[dim]Calibration only — never a signal (PART 11.3)[/dim]",
        box=box.DOUBLE, border_style="magenta"))

    if args.slug:
        m = market_by_slug(args.slug)
        if not m or "error" in m:
            console.print(f"[red]Market not found: {(m or {}).get('error', 'no data')}[/red]")
            return
        console.print(f"[bold]{m['question']}[/bold]")
        console.print(f"  P(yes): {m['yes']*100:.1f}%  ·  volume ${float(m.get('volume') or 0):,.0f}"
                      f"  ·  liquidity ${float(m.get('liquidity') or 0):,.0f}"
                      f"  ·  ends {m['end']}")
        return

    if args.query:
        results = [x for x in search_markets(args.query) if x and "error" not in x]
        if not results:
            console.print(f"[red]No markets matched '{args.query}'[/red]")
            return
        tbl = Table(title=f"Search: '{args.query}'", box=box.HEAVY_HEAD)
        tbl.add_column("Market", overflow="fold", max_width=50)
        tbl.add_column("P(yes)", justify="right")
        tbl.add_column("Vol $", justify="right")
        tbl.add_column("Ends", justify="right")
        for m in sorted(results, key=lambda x: float(x.get("volume") or 0),
                        reverse=True)[:10]:
            tbl.add_row(m["question"] or "—", f"{m['yes']*100:.1f}%",
                        f"{float(m.get('volume') or 0)/1e6:.1f}M", m["end"])
        console.print(tbl)
        return

    rows = calibration_rows()
    tbl = Table(title="Crowd prices vs model-implied probabilities", box=box.HEAVY_HEAD)
    tbl.add_column("Calibration", style="bold")
    tbl.add_column("Market", overflow="fold", max_width=36)
    tbl.add_column("P(yes)", justify="right")
    tbl.add_column("Model P", justify="right")
    tbl.add_column("Gap", justify="right")
    tbl.add_column("Interpretation", overflow="fold", max_width=28)
    tbl.add_column("Vol $", justify="right")
    for r in rows:
        m = r.get("market")
        if not m:
            tbl.add_row(r["name"], "[dim]—[/dim]", "—", "—", "—",
                        r.get("note", ""), "—")
            continue
        mp = r.get("model_p")
        tbl.add_row(r["name"], m["question"] or "—",
                    f"{m['yes']*100:.1f}%",
                    f"{mp*100:.1f}%" if mp is not None else "[dim]—[/dim]",
                    f"{r['gap']*100:+.1f}pp" if r.get("gap") is not None else "—",
                    r["interpretation"],
                    f"{float(m.get('volume') or 0)/1e6:.1f}M")
    console.print(tbl)
    console.print("[dim]Provenance: market prices LIVE (Polymarket Gamma API) · "
                  "model probabilities EST with formulas disclosed in "
                  "saf/news/polymarket.py · gaps > 15pp flagged as divergence, "
                  "never as trade triggers.[/dim]")


if __name__ == "__main__":
    main()