"""
ThruWatch — Database Layer
SQLite-backed storage for network stats, reset history, and leaderboard data.
All data lives here, NOT on-chain — so resets don't destroy our history.
"""

import sqlite3
import time
from pathlib import Path
from typing import Optional, List, Dict, Any

DB_PATH = Path("thruwatch.db")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # rows behave like dicts
    return conn


def init_db() -> None:
    """Create all tables if they don't exist."""
    with get_connection() as conn:
        conn.executescript("""
            -- Snapshots of network state taken every poll cycle
            CREATE TABLE IF NOT EXISTS network_snapshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          INTEGER NOT NULL,          -- unix timestamp
                block_height INTEGER,                  -- current block height (NULL if unreachable)
                is_healthy  INTEGER NOT NULL DEFAULT 1, -- 1 = up, 0 = down
                rpc_latency_ms INTEGER,                -- how long the RPC took to respond
                tps         REAL,                      -- estimated transactions per second
                avg_block_time_ms INTEGER              -- rolling average block time
            );

            -- Every confirmed reset gets a row here
            CREATE TABLE IF NOT EXISTS resets (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                detected_at     INTEGER NOT NULL,      -- unix timestamp when we first saw it
                resolved_at     INTEGER,               -- when block height started climbing again
                prev_block_height INTEGER,             -- height just before the reset
                reset_number    INTEGER NOT NULL,      -- sequential reset counter (Reset #1, #2 ...)
                downtime_seconds INTEGER               -- how long until network recovered
            );

            -- Per-address activity stats (rebuilt fresh each time we poll)
            CREATE TABLE IF NOT EXISTS leaderboard (
                address         TEXT PRIMARY KEY,
                tx_count        INTEGER NOT NULL DEFAULT 0,
                programs_deployed INTEGER NOT NULL DEFAULT 0,
                last_seen_at    INTEGER,
                first_seen_at   INTEGER
            );

            -- Faucet call log
            CREATE TABLE IF NOT EXISTS faucet_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          INTEGER NOT NULL,
                address     TEXT NOT NULL,
                amount      INTEGER NOT NULL DEFAULT 100,
                success     INTEGER NOT NULL DEFAULT 1
            );

            -- ThruWatch internal state (key/value)
            CREATE TABLE IF NOT EXISTS state (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL
            );
        """)


# ─── Network Snapshots ────────────────────────────────────────────────────────

def insert_snapshot(
    block_height: Optional[int],
    is_healthy: bool,
    rpc_latency_ms: Optional[int] = None,
    tps: Optional[float] = None,
    avg_block_time_ms: Optional[int] = None,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO network_snapshots
               (ts, block_height, is_healthy, rpc_latency_ms, tps, avg_block_time_ms)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (int(time.time()), block_height, int(is_healthy), rpc_latency_ms, tps, avg_block_time_ms),
        )


def get_latest_snapshots(limit: int = 60) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM network_snapshots ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_latest_snapshot() -> Optional[Dict[str, Any]]:
    snapshots = get_latest_snapshots(limit=1)
    return snapshots[0] if snapshots else None


# ─── Resets ──────────────────────────────────────────────────────────────────

def get_reset_count() -> int:
    with get_connection() as conn:
        row = conn.execute("SELECT COUNT(*) as cnt FROM resets").fetchone()
    return row["cnt"]


def record_reset(prev_block_height: int) -> int:
    """Log a new reset. Returns the reset_number."""
    reset_number = get_reset_count() + 1
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO resets (detected_at, prev_block_height, reset_number)
               VALUES (?, ?, ?)""",
            (int(time.time()), prev_block_height, reset_number),
        )
    return reset_number


def resolve_reset(reset_number: int) -> None:
    """Mark a reset as resolved once the network is back."""
    now = int(time.time())
    with get_connection() as conn:
        row = conn.execute(
            "SELECT detected_at FROM resets WHERE reset_number = ?", (reset_number,)
        ).fetchone()
        if row:
            downtime = now - row["detected_at"]
            conn.execute(
                """UPDATE resets SET resolved_at = ?, downtime_seconds = ?
                   WHERE reset_number = ?""",
                (now, downtime, reset_number),
            )


def get_resets(limit: int = 20) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM resets ORDER BY detected_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ─── Leaderboard ─────────────────────────────────────────────────────────────

def upsert_leaderboard(address: str, tx_count: int, programs_deployed: int) -> None:
    now = int(time.time())
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT first_seen_at FROM leaderboard WHERE address = ?", (address,)
        ).fetchone()
        first_seen = existing["first_seen_at"] if existing else now
        conn.execute(
            """INSERT INTO leaderboard (address, tx_count, programs_deployed, last_seen_at, first_seen_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(address) DO UPDATE SET
                 tx_count = excluded.tx_count,
                 programs_deployed = excluded.programs_deployed,
                 last_seen_at = excluded.last_seen_at""",
            (address, tx_count, programs_deployed, now, first_seen),
        )


def get_leaderboard(limit: int = 20) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM leaderboard
               ORDER BY tx_count DESC, programs_deployed DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ─── Faucet Log ──────────────────────────────────────────────────────────────

def log_faucet_call(address: str, amount: int = 100, success: bool = True) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO faucet_log (ts, address, amount, success) VALUES (?, ?, ?, ?)",
            (int(time.time()), address, amount, int(success)),
        )


def get_faucet_stats() -> Dict[str, Any]:
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) as cnt FROM faucet_log").fetchone()["cnt"]
        today_start = int(time.time()) - 86400
        today = conn.execute(
            "SELECT COUNT(*) as cnt FROM faucet_log WHERE ts >= ?", (today_start,)
        ).fetchone()["cnt"]
    return {"total_calls": total, "calls_last_24h": today}


# ─── Internal State ──────────────────────────────────────────────────────────

def set_state(key: str, value: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_state(key: str, default: str = "") -> str:
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default
