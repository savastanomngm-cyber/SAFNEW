"""SQLite persistence. Single source of truth for all state."""
import sqlite3, json, hashlib, time
import pandas as pd
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "saf.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    ticker TEXT NOT NULL, date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, adj_close REAL, volume INTEGER,
    PRIMARY KEY (ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(date);

CREATE TABLE IF NOT EXISTS fundamentals (
    ticker TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    ticker TEXT PRIMARY KEY, last_fetch TEXT, last_success TEXT,
    fetch_failures INTEGER DEFAULT 0, quality_flags TEXT DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
    kind TEXT NOT NULL, payload TEXT NOT NULL, prev_hash TEXT, hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, ticker TEXT NOT NULL,
    action TEXT, position_pct REAL, notes TEXT,
    outcome TEXT, realized_ret REAL, signal_json TEXT
);
CREATE TABLE IF NOT EXISTS rubric_cache (
    ticker TEXT PRIMARY KEY, scored_at TEXT NOT NULL,
    total_score REAL, raw_json TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    ticker TEXT NOT NULL, opened_at TEXT NOT NULL,
    action TEXT NOT NULL, direction TEXT NOT NULL,
    entry REAL NOT NULL, shares INTEGER NOT NULL,
    account_size REAL NOT NULL,
    binding TEXT, realized_vol REAL,
    stop REAL, trail REAL, trail_stop REAL,
    time_stop_date TEXT, target_exit_date TEXT,
    state TEXT NOT NULL DEFAULT 'OPEN',
    closed_at TEXT, exit_price REAL, realized_pct REAL,
    signal_json TEXT, close_reason TEXT,
    PRIMARY KEY (ticker, opened_at)
);
CREATE INDEX IF NOT EXISTS idx_positions_state ON positions(state);
"""

def con() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init():
    with con() as c:
        c.executescript(SCHEMA)

# ── prices ──────────────────────────────────────────────────
def upsert_prices(ticker: str, df: pd.DataFrame):
    rows = [(ticker, str(idx.date()), r.open, r.high, r.low,
             r.close, r.adj_close, int(r.volume)) for idx, r in df.iterrows()]
    with con() as c:
        c.executemany("""INSERT OR REPLACE INTO prices
                         (ticker,date,open,high,low,close,adj_close,volume)
                         VALUES (?,?,?,?,?,?,?,?)""", rows)
        c.execute("""INSERT INTO meta (ticker,last_fetch,last_success)
                     VALUES (?,datetime('now'),datetime('now'))
                     ON CONFLICT(ticker) DO UPDATE SET
                     last_fetch=datetime('now'), last_success=datetime('now')""", (ticker,))

def load_prices(ticker: str) -> pd.DataFrame:
    df = pd.read_sql("SELECT * FROM prices WHERE ticker=? ORDER BY date", con(), params=(ticker,))
    if df.empty:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")
    df["px"] = df["adj_close"].fillna(df["close"])
    return df

def last_price_date(ticker: str):
    row = con().execute("SELECT MAX(date) AS d FROM prices WHERE ticker=?", (ticker,)).fetchone()
    return row["d"] if row else None

# ── fundamentals ────────────────────────────────────────────
def save_fundamentals(ticker: str, info: dict):
    with con() as c:
        c.execute("""INSERT OR REPLACE INTO fundamentals (ticker,fetched_at,json)
                     VALUES (?,datetime('now'),?)""", (ticker, json.dumps(info, default=str)))

def get_fundamentals(ticker: str, max_age_days=7):
    row = con().execute("SELECT fetched_at,json FROM fundamentals WHERE ticker=?", (ticker,)).fetchone()
    if not row:
        return None
    age = (pd.Timestamp.now() - pd.Timestamp(row["fetched_at"])).days
    info = json.loads(row["json"])
    info["_stale_days"] = age
    return info if age <= max_age_days else {**info, "_stale": True}

# ── quality flags ───────────────────────────────────────────
def set_flags(ticker: str, flags: list):
    with con() as c:
        c.execute("""INSERT INTO meta (ticker,quality_flags) VALUES (?,?)
                     ON CONFLICT(ticker) DO UPDATE SET quality_flags=excluded.quality_flags""",
                  (ticker, json.dumps(flags)))

def get_meta(ticker: str) -> dict:
    row = con().execute("SELECT * FROM meta WHERE ticker=?", (ticker,)).fetchone()
    return dict(row) if row else {}

# ── audit log (hash-chained, append-only) ───────────────────
def audit_log(kind: str, payload: dict):
    prev = con().execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
    prev_hash = prev["hash"] if prev else "GENESIS"
    entry = {"ts": time.time(), "kind": kind, "payload": payload, "prev": prev_hash}
    h = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()[:16]
    with con() as c:
        c.execute("""INSERT INTO audit (ts,kind,payload,prev_hash,hash) VALUES (?,?,?,?,?)""",
                  (entry["ts"], kind, json.dumps(payload, default=str), prev_hash, h))

def verify_audit_chain() -> bool:
    rows = con().execute("SELECT * FROM audit ORDER BY id").fetchall()
    prev = "GENESIS"
    for r in rows:
        entry = {"ts": r["ts"], "kind": r["kind"], "payload": json.loads(r["payload"]), "prev": prev}
        expect = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()[:16]
        if expect != r["hash"]:
            return False
        prev = r["hash"]
    return True

def recent_audit(limit=25):
    rows = con().execute("""SELECT ts, kind, payload, hash FROM audit
                            ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
    out = []
    for r in rows:
        try:
            pl = json.loads(r["payload"])
        except Exception:
            pl = {"raw": str(r["payload"])[:80]}
        out.append({"ts": r["ts"], "kind": r["kind"], "payload": pl, "hash": r["hash"]})
    return out

# ── trading memory ──────────────────────────────────────────
def save_memory(ticker, action, pct, notes, signal_json=None):
    with con() as c:
        c.execute("""INSERT INTO memory (date,ticker,action,position_pct,notes,signal_json)
                     VALUES (datetime('now'),?,?,?,?,?)""",
                  (ticker, action, pct, str(notes)[:200], signal_json))

def recent_memory(limit=50):
    rows = con().execute("""SELECT date, ticker, action, position_pct, notes,
                                   outcome, realized_ret
                            FROM memory ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows]

def memory_for(ticker, limit=8):
    rows = con().execute(
        """SELECT date, action, position_pct, notes, outcome, realized_ret
           FROM memory WHERE ticker=? ORDER BY id DESC LIMIT ?""", (ticker, limit)).fetchall()
    return [dict(r) for r in rows][::-1]

def graded_memory_text(ticker):
    rows = memory_for(ticker)
    if not rows:
        return "No past decisions on this ticker."
    lines = []
    for r in rows:
        if r["outcome"] and r["realized_ret"] is not None:
            lines.append(f"- {r['date']}: {r['action']} -> {r['outcome']} ({r['realized_ret']:+.1f}%)")
        else:
            lines.append(f"- {r['date']}: {r['action']} -> PENDING (not yet graded)")
    graded = [r for r in rows if r["outcome"]]
    wins = sum(1 for r in graded if r["outcome"] == "WIN")
    tail = f"\nHit rate: {wins}/{len(graded)}" if graded else ""
    return "Past decisions:\n" + "\n".join(lines) + tail

def grade_memory(horizon_days=30, win=2.0, loss=-2.0):
    c = con()
    rows = c.execute("""SELECT id,ticker,date,action FROM memory
                        WHERE outcome IS NULL AND date <= datetime('now', ?)""",
                     (f"-{horizon_days} days",)).fetchall()
    for r in rows:
        px = load_prices(r["ticker"])
        if px.empty:
            continue
        entry_date = pd.Timestamp(r["date"])
        future = px[px.index > entry_date]
        if len(future) < 2:
            continue
        ret = (future["px"].iloc[-1] / future["px"].iloc[0] - 1) * 100
        if r["action"] == "SELL":
            ret = -ret
        outcome = "WIN" if ret > win else "LOSS" if ret < loss else "FLAT"
        c.execute("UPDATE memory SET outcome=?, realized_ret=? WHERE id=?",
                  (outcome, round(ret, 2), r["id"]))
    c.commit()
    return len(rows)

def scorecard():
    c = con()
    rows = c.execute("""SELECT ticker, action, date, outcome, realized_ret, signal_json
                        FROM memory WHERE outcome IS NOT NULL ORDER BY date ASC""").fetchall()
    if not rows:
        return {"n_signals": 0, "n_wins": 0, "n_losses": 0, "n_flat": 0,
                "hit_rate": None, "avg_return": None, "by_action": {},
                "recent": [], "positions_summary": None}
    total = len(rows)
    wins = [r for r in rows if r["outcome"] == "WIN"]
    losses = [r for r in rows if r["outcome"] == "LOSS"]
    flats = [r for r in rows if r["outcome"] == "FLAT"]
    rets = [r["realized_ret"] for r in rows if r["realized_ret"] is not None]
    by_action = {}
    for r in rows:
        a = r["action"]
        by_action.setdefault(a, {"n": 0, "wins": 0, "losses": 0, "rets": []})
        by_action[a]["n"] += 1
        if r["outcome"] == "WIN":
            by_action[a]["wins"] += 1
        if r["outcome"] == "LOSS":
            by_action[a]["losses"] += 1
        if r["realized_ret"] is not None:
            by_action[a]["rets"].append(r["realized_ret"])
    for a in by_action:
        by_action[a]["hit_rate"] = (round(by_action[a]["wins"] / by_action[a]["n"], 3)
                                    if by_action[a]["n"] else None)
        by_action[a]["avg_ret"] = (round(sum(by_action[a]["rets"]) / len(by_action[a]["rets"]), 2)
                                   if by_action[a]["rets"] else None)
    recent = [dict(r) for r in rows[-10:]]
    return {"n_signals": total, "n_wins": len(wins), "n_losses": len(losses),
            "n_flat": len(flats),
            "hit_rate": round(len(wins) / total, 3) if total else None,
            "avg_return": round(sum(rets) / len(rets), 2) if rets else None,
            "by_action": by_action, "recent": recent,
            "positions_summary": positions_summary()}

# ── positions ───────────────────────────────────────────────
def open_position(ticker, action, entry, shares, account_size, binding,
                  realized_vol, stop, trail, trail_stop, time_stop_date,
                  target_exit_date, signal_json=None):
    direction = "LONG" if action == "BUY" else "SHORT"
    with con() as c:
        c.execute("""INSERT OR REPLACE INTO positions
                     (ticker, opened_at, action, direction, entry, shares,
                      account_size, binding, realized_vol, stop, trail, trail_stop,
                      time_stop_date, target_exit_date, state, signal_json)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'OPEN', ?)""",
                  (ticker, pd.Timestamp.now().isoformat(timespec="seconds"),
                   action, direction, entry, shares, account_size, binding,
                   realized_vol, stop, trail, trail_stop, time_stop_date,
                   target_exit_date, signal_json))

def active_positions():
    rows = con().execute("""SELECT * FROM positions WHERE state='OPEN'
                            ORDER BY opened_at""").fetchall()
    return [dict(r) for r in rows]

def all_positions(limit=50):
    rows = con().execute("""SELECT * FROM positions ORDER BY opened_at DESC LIMIT ?""",
                         (limit,)).fetchall()
    return [dict(r) for r in rows]

def update_position(ticker, opened_at, **fields):
    if not fields:
        return
    c = con()
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [ticker, opened_at]
    c.execute(f"UPDATE positions SET {sets} WHERE ticker=? AND opened_at=?", vals)
    c.commit()

def close_position(ticker, opened_at, exit_price, reason):
    px = con().execute("""SELECT entry, direction FROM positions
                          WHERE ticker=? AND opened_at=?""", (ticker, opened_at)).fetchone()
    if not px:
        return None
    sign = 1 if px["direction"] == "LONG" else -1
    pct = (exit_price / px["entry"] - 1) * 100 * sign
    update_position(ticker, opened_at,
                    state="CLOSED", closed_at=pd.Timestamp.now().isoformat(timespec="seconds"),
                    exit_price=exit_price, realized_pct=round(pct, 2), close_reason=reason)
    return round(pct, 2)

def positions_summary():
    active = active_positions()
    if not active:
        return {"n_open": 0, "avg_realized_pct": None, "states": {}}
    all_closed = [dict(r) for r in con().execute(
        "SELECT state, realized_pct FROM positions WHERE state!='OPEN'").fetchall()]
    from collections import Counter
    states = Counter(r["state"] for r in all_closed)
    rets = [r["realized_pct"] for r in all_closed if r.get("realized_pct") is not None]
    return {"n_open": len(active),
            "avg_realized_pct": round(sum(rets) / len(rets), 2) if rets else None,
            "states": dict(states)}

# ── rubric cache ────────────────────────────────────────────
def save_rubric(ticker: str, total: float, raw_dict: dict):
    with con() as c:
        c.execute("""INSERT OR REPLACE INTO rubric_cache
                     (ticker,scored_at,total_score,raw_json)
                     VALUES (?,datetime('now'),?,?)""",
                  (ticker, total, json.dumps(raw_dict, default=str)))

def get_cached_rubric(ticker: str, max_age_days=30):
    row = con().execute("""SELECT scored_at,total_score,raw_json FROM rubric_cache
                           WHERE ticker=?""", (ticker,)).fetchone()
    if not row:
        return None
    age = (pd.Timestamp.now() - pd.Timestamp(row["scored_at"])).days
    if age > max_age_days:
        return None
    return {"total": row["total_score"], "raw": json.loads(row["raw_json"]), "age_days": age}