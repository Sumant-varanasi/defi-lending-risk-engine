"""SQLite analytics database.

Stores time-series snapshots of pool state, prices, and lifecycle events
(borrows, repays, liquidations). The dashboard reads from here; the indexer
and risk engine write to it.

Schema:
  - prices(asset_addr, ts, price_wad)              -> oracle history
  - reserve_snapshots(asset_addr, ts, ...)         -> utilization, rates, indices
  - positions(user, ts, total_coll_usd, ...)       -> per-user snapshots
  - events(ts, kind, user, asset, amount, tx_hash) -> on-chain event log
  - risk_params(asset_addr, ts, ltv_recommended_bps, ...) -> risk engine outputs
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from python.config import DB_PATH


SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    asset_addr TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    price_wad  TEXT NOT NULL,           -- stored as string to preserve uint256 precision
    PRIMARY KEY (asset_addr, ts)
);
CREATE INDEX IF NOT EXISTS idx_prices_ts ON prices(ts);

CREATE TABLE IF NOT EXISTS reserve_snapshots (
    asset_addr        TEXT NOT NULL,
    ts                INTEGER NOT NULL,
    total_supply      TEXT NOT NULL,
    total_borrow      TEXT NOT NULL,
    utilization_wad   TEXT NOT NULL,
    borrow_apr_wad    TEXT NOT NULL,    -- annualized
    supply_apr_wad    TEXT NOT NULL,
    liquidity_index   TEXT NOT NULL,
    borrow_index      TEXT NOT NULL,
    PRIMARY KEY (asset_addr, ts)
);
CREATE INDEX IF NOT EXISTS idx_reserve_ts ON reserve_snapshots(ts);

CREATE TABLE IF NOT EXISTS positions (
    user_addr            TEXT NOT NULL,
    ts                   INTEGER NOT NULL,
    total_coll_usd_wad   TEXT NOT NULL,
    total_debt_usd_wad   TEXT NOT NULL,
    health_factor_wad    TEXT NOT NULL,
    liq_threshold_bps    INTEGER NOT NULL,
    PRIMARY KEY (user_addr, ts)
);
CREATE INDEX IF NOT EXISTS idx_positions_hf ON positions(health_factor_wad);
CREATE INDEX IF NOT EXISTS idx_positions_ts ON positions(ts);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    block_num   INTEGER NOT NULL,
    kind        TEXT NOT NULL,          -- 'deposit'|'withdraw'|'borrow'|'repay'|'liquidation'
    user_addr   TEXT,
    asset_addr  TEXT,
    amount      TEXT,
    extra_json  TEXT,                   -- e.g., liquidation details
    tx_hash     TEXT NOT NULL,
    log_index   INTEGER NOT NULL,
    UNIQUE(tx_hash, log_index)
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);

CREATE TABLE IF NOT EXISTS risk_params (
    asset_addr             TEXT NOT NULL,
    ts                     INTEGER NOT NULL,
    realized_vol_30d       REAL,
    ltv_recommended_bps    INTEGER,
    liq_threshold_recommended_bps INTEGER,
    stress_score           REAL,        -- 0-100, higher = riskier
    notes                  TEXT,
    PRIMARY KEY (asset_addr, ts)
);

CREATE TABLE IF NOT EXISTS indexer_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def get_conn(path: str = DB_PATH) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def session(path: str = DB_PATH):
    conn = get_conn(path)
    try:
        yield conn
    finally:
        conn.close()


# ---- write helpers -------------------------------------------------------
def write_price(conn, asset_addr: str, ts: int, price_wad: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO prices VALUES (?, ?, ?)",
        (asset_addr.lower(), ts, str(price_wad)),
    )


def write_reserve_snapshot(conn, asset_addr: str, ts: int, snap: dict) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO reserve_snapshots
           VALUES (:asset_addr, :ts, :total_supply, :total_borrow, :utilization_wad,
                   :borrow_apr_wad, :supply_apr_wad, :liquidity_index, :borrow_index)""",
        {
            "asset_addr": asset_addr.lower(),
            "ts": ts,
            "total_supply": str(snap["total_supply"]),
            "total_borrow": str(snap["total_borrow"]),
            "utilization_wad": str(snap["utilization_wad"]),
            "borrow_apr_wad": str(snap["borrow_apr_wad"]),
            "supply_apr_wad": str(snap["supply_apr_wad"]),
            "liquidity_index": str(snap["liquidity_index"]),
            "borrow_index": str(snap["borrow_index"]),
        },
    )


def write_position(conn, user_addr: str, ts: int, data: dict) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO positions VALUES (?, ?, ?, ?, ?, ?)""",
        (
            user_addr.lower(),
            ts,
            str(data["total_coll_usd_wad"]),
            str(data["total_debt_usd_wad"]),
            str(data["health_factor_wad"]),
            data["liq_threshold_bps"],
        ),
    )


def write_event(conn, ev: dict) -> None:
    try:
        conn.execute(
            """INSERT INTO events (ts, block_num, kind, user_addr, asset_addr, amount,
                                   extra_json, tx_hash, log_index)
               VALUES (:ts, :block_num, :kind, :user_addr, :asset_addr, :amount,
                       :extra_json, :tx_hash, :log_index)""",
            ev,
        )
    except sqlite3.IntegrityError:
        pass  # duplicate (tx_hash, log_index)


def get_state(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM indexer_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_state(conn, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO indexer_state VALUES (?, ?)", (key, value))


# ---- read helpers (used by dashboard) ------------------------------------
def get_recent_positions(conn, limit: int = 100) -> list[tuple]:
    """Return latest snapshot per user, sorted by health factor (ascending = riskiest first)."""
    return conn.execute(
        """SELECT user_addr,
                  ts,
                  CAST(total_coll_usd_wad AS REAL) / 1e18,
                  CAST(total_debt_usd_wad AS REAL) / 1e18,
                  CAST(health_factor_wad AS REAL) / 1e18,
                  liq_threshold_bps
           FROM positions p
           WHERE ts = (SELECT MAX(ts) FROM positions WHERE user_addr = p.user_addr)
             AND CAST(total_debt_usd_wad AS REAL) > 0
           ORDER BY CAST(health_factor_wad AS REAL) ASC
           LIMIT ?""",
        (limit,),
    ).fetchall()


def get_price_series(conn, asset_addr: str, since_ts: int | None = None) -> list[tuple]:
    q = "SELECT ts, CAST(price_wad AS REAL) / 1e18 FROM prices WHERE asset_addr = ?"
    args: list = [asset_addr.lower()]
    if since_ts is not None:
        q += " AND ts >= ?"
        args.append(since_ts)
    q += " ORDER BY ts ASC"
    return conn.execute(q, args).fetchall()


def get_reserve_series(conn, asset_addr: str, since_ts: int | None = None) -> list[tuple]:
    q = """SELECT ts,
                  CAST(utilization_wad AS REAL) / 1e18,
                  CAST(borrow_apr_wad AS REAL) / 1e18,
                  CAST(supply_apr_wad AS REAL) / 1e18,
                  CAST(total_supply AS REAL),
                  CAST(total_borrow AS REAL)
           FROM reserve_snapshots WHERE asset_addr = ?"""
    args: list = [asset_addr.lower()]
    if since_ts is not None:
        q += " AND ts >= ?"
        args.append(since_ts)
    q += " ORDER BY ts ASC"
    return conn.execute(q, args).fetchall()


def get_recent_events(conn, limit: int = 200, kinds: Iterable[str] | None = None) -> list[tuple]:
    q = "SELECT ts, kind, user_addr, asset_addr, amount, tx_hash FROM events"
    args: list = []
    if kinds:
        q += " WHERE kind IN (" + ",".join("?" for _ in kinds) + ")"
        args.extend(kinds)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return conn.execute(q, args).fetchall()
