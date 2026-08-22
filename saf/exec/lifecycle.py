"""Position lifecycle monitor (PART 8).
Daily check of every OPEN position against live prices.
States: OPEN -> STOPPED | TRAIL_STOPPED | TIME_STOPPED | THESIS_DECAY
        | SCORE_DECAY (longs) | SCORE_RECOVERY (shorts) | CLOSED_MANUAL

FIXED: score checks are direction-aware. For a LONG, a collapsing score is
the alarm. For a SHORT, a RECOVERING score is the alarm — a decaying score
confirms the short thesis and must NOT trigger an exit.

All transitions are logged in the audit chain."""
import pandas as pd
from .. import config, store
from ..ai import evidence, rubric as rb

WATCH_THRESHOLD = 45
RUBRIC_PASS_THRESHOLD = 22


def check_position(pos) -> dict:
    """Returns a trigger dict or {no_change: True}."""
    ticker = pos["ticker"]
    px = store.load_prices(ticker)
    if px.empty:
        return {"ticker": ticker, "no_change": True, "reason": "no price data"}

    last = px.iloc[-1]
    last_date = str(px.index[-1].date())
    price = float(last["px"])
    entry = float(pos["entry"])
    direction = pos["direction"]

    # Track extremes since entry for trailing ratchets
    since = px[px.index > pd.Timestamp(pos["opened_at"])]
    max_high = float(since["high"].max()) if not since.empty else entry
    min_low = float(since["low"].min()) if not since.empty else entry

    # Trailing ratchet (tightens only, never loosens)
    new_trail_stop = None
    if direction == "LONG" and pos["trail"]:
        new_trail_stop = max_high - float(pos["trail"])
        if new_trail_stop > (pos.get("trail_stop") or -1e9):
            store.update_position(ticker, pos["opened_at"], trail_stop=new_trail_stop)

    # Hard stops and trailing stops
    if direction == "LONG":
        if pos["stop"] is not None and price <= pos["stop"]:
            return {"ticker": ticker, "state": "STOPPED",
                    "reason": f"hard stop hit @ {price} (stop {pos['stop']})",
                    "new_price": price, "exit_price": float(pos["stop"])}
        if new_trail_stop is not None and price <= new_trail_stop:
            return {"ticker": ticker, "state": "TRAIL_STOPPED",
                    "reason": f"trailing stop hit @ {price} (trail {new_trail_stop:.2f})",
                    "new_price": price, "exit_price": price}
    else:  # SHORT
        if pos["stop"] is not None and price >= pos["stop"]:
            return {"ticker": ticker, "state": "STOPPED",
                    "reason": f"short stop hit @ {price} (stop {pos['stop']})",
                    "new_price": price, "exit_price": float(pos["stop"])}
        if pos["trail"]:
            trail_up = min_low + float(pos["trail"])
            if price >= trail_up:
                return {"ticker": ticker, "state": "TRAIL_STOPPED",
                        "reason": f"short trailing stop @ {price} (trail {trail_up:.2f})",
                        "new_price": price, "exit_price": price}

    # Time stop — the thesis must work in a quarter
    if pos["time_stop_date"]:
        if pd.Timestamp(last_date) >= pd.Timestamp(pos["time_stop_date"]):
            return {"ticker": ticker, "state": "TIME_STOPPED",
                    "reason": f"time stop — {pos['time_stop_date']} reached",
                    "new_price": price, "exit_price": price}

    # Thesis decay — rubric re-score if cache is stale
    cached = store.get_cached_rubric(ticker)
    if cached and cached["age_days"] > 30:
        pack = evidence.build_evidence_pack(ticker)
        if pack.get("business_desc"):
            fresh = rb.score_bottleneck(ticker, pack)
            if fresh and "error" not in fresh:
                store.save_rubric(ticker, fresh["total"], fresh)
                cached = store.get_cached_rubric(ticker)
    if cached and cached.get("total", 30) < RUBRIC_PASS_THRESHOLD:
        return {"ticker": ticker, "state": "THESIS_DECAY",
                "reason": f"rubric total {cached['total']} below {RUBRIC_PASS_THRESHOLD}",
                "new_price": price, "exit_price": price}

    # Score check — DIRECTION-AWARE (the fix)
    cfg = config.load()
    bench = cfg["settings"]["benchmark"]
    spy = store.load_prices(bench)
    if not spy.empty:
        from ..quant import score as S
        s = S.score_v2(ticker, px.index[-1], {ticker: px}, spy)
        if s:
            if direction == "LONG" and s["total"] < WATCH_THRESHOLD:
                return {"ticker": ticker, "state": "SCORE_DECAY",
                        "reason": f"long thesis decay — score {s['total']} < {WATCH_THRESHOLD}",
                        "new_price": price, "exit_price": price}
            if direction == "SHORT" and s["total"] >= WATCH_THRESHOLD:
                return {"ticker": ticker, "state": "SCORE_RECOVERY",
                        "reason": f"short in danger — score recovered to {s['total']} >= {WATCH_THRESHOLD}",
                        "new_price": price, "exit_price": price}

    return {"ticker": ticker, "no_change": True, "new_price": price}


def run_monitor(auto_close=False):
    """Check every OPEN position. With auto_close=True, executes closes."""
    positions = store.active_positions()
    if not positions:
        return {"n_positions": 0, "events": []}
    events = []
    for pos in positions:
        result = check_position(pos)
        if result.get("no_change"):
            events.append({"ticker": result["ticker"], "status": "open",
                           "price": result.get("new_price")})
        else:
            events.append({**result, "status": "triggered"})
            if auto_close:
                pct = store.close_position(result["ticker"], pos["opened_at"],
                                           result["exit_price"], result["state"])
                result["realized_pct"] = pct
                store.audit_log("position_closed", {
                    "ticker": result["ticker"], "state": result["state"],
                    "reason": result["reason"], "realized_pct": pct})
    return {"n_positions": len(positions), "events": events}


def positions_table():
    """Snapshot for CLI / web display — all open positions with live P/L."""
    positions = store.active_positions()
    rows = []
    for pos in positions:
        px = store.load_prices(pos["ticker"])
        last_price = None
        unrealized_pct = None
        days_held = None
        if not px.empty:
            last_price = float(px["px"].iloc[-1])
            sign = 1 if pos["direction"] == "LONG" else -1
            unrealized_pct = (last_price / pos["entry"] - 1) * 100 * sign
            days_held = (pd.Timestamp.now() - pd.Timestamp(pos["opened_at"])).days
        rows.append({**pos, "last_price": last_price,
                     "unrealized_pct": round(unrealized_pct, 2) if unrealized_pct is not None else None,
                     "days_held": days_held})
    return rows