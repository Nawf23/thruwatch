"""
ThruWatch — Network Poller
Runs alongside the watcher, collecting richer network stats:
  - TPS estimate (transactions per second)
  - Average block time
  - Active wallet count
  - Faucet usage stats

All stats are written to SQLite so the dashboard can serve them.
"""

import time
import threading
import httpx
from typing import Optional
from rich.console import Console

from config import ThruWatchConfig
from core.db import insert_snapshot, upsert_leaderboard, get_latest_snapshots

console = Console()


# ─── Stats Calculation ───────────────────────────────────────────────────────

def calculate_tps(snapshots: list[dict]) -> Optional[float]:
    """
    Estimate TPS from the last N snapshots.
    Requires block_height data — returns None if insufficient data.
    """
    valid = [s for s in snapshots if s.get("block_height") is not None]
    if len(valid) < 2:
        return None

    newest = valid[0]
    oldest = valid[-1]
    height_delta = newest["block_height"] - oldest["block_height"]
    time_delta = newest["ts"] - oldest["ts"]

    if time_delta <= 0 or height_delta <= 0:
        return None

    # Rough estimate: assume ~100 txns/block (adjust when Thru publishes tx-per-block data)
    # TODO: replace with actual tx count from RPC when available
    ASSUMED_TXN_PER_BLOCK = 50
    return round((height_delta * ASSUMED_TXN_PER_BLOCK) / time_delta, 2)


def calculate_avg_block_time(snapshots: list[dict]) -> Optional[int]:
    """
    Estimate average block time in milliseconds from recent snapshots.
    """
    valid = [s for s in snapshots if s.get("block_height") is not None]
    if len(valid) < 2:
        return None

    newest = valid[0]
    oldest = valid[-1]
    height_delta = newest["block_height"] - oldest["block_height"]
    time_delta_ms = (newest["ts"] - oldest["ts"]) * 1000

    if height_delta <= 0:
        return None

    return int(time_delta_ms / height_delta)


# ─── Explorer API Polling ────────────────────────────────────────────────────

def fetch_leaderboard_from_explorer(rpc_url: str) -> list[dict]:
    """
    Attempt to pull address activity data from the Thru explorer API.

    NOTE: The exact endpoint depends on what scan.thru.org exposes.
    This is a best-effort attempt — returns empty list if unavailable.
    Extend this function once the explorer's public API is documented.
    """
    # Try known explorer endpoints
    candidate_urls = [
        "https://scan.thru.org/api/v1/accounts/top",
        "https://scan.thru.org/api/accounts",
        f"{rpc_url}/accounts/top",
    ]

    for url in candidate_urls:
        try:
            resp = httpx.get(url, timeout=8, follow_redirects=True)
            if resp.status_code == 200:
                data = resp.json()
                # Normalise to [{address, tx_count, programs_deployed}]
                if isinstance(data, list):
                    return data
                if isinstance(data, dict) and "accounts" in data:
                    return data["accounts"]
        except Exception:
            continue

    return []


# ─── Poll Loop ───────────────────────────────────────────────────────────────

def poll_loop(cfg: ThruWatchConfig, stop_event: threading.Event) -> None:
    """
    Background loop that enriches the DB with derived stats every N seconds.
    Runs separately from the watcher so it doesn't block reset detection.
    """
    interval = cfg.network.poll_interval * 2  # poll at half the watcher rate
    rpc_url = cfg.network.rpc_url

    console.print(f"[green][ThruWatch] Poller started. Enriching stats every {interval}s[/green]")

    while not stop_event.is_set():
        try:
            # Get recent snapshots to compute derived metrics
            snapshots = get_latest_snapshots(limit=20)

            tps = calculate_tps(snapshots)
            avg_block_time = calculate_avg_block_time(snapshots)

            # Update the most recent snapshot with computed stats if available
            if (tps is not None or avg_block_time is not None) and snapshots:
                latest = snapshots[0]
                # Insert an enriched snapshot (the watcher already inserted the raw one)
                # We avoid double-inserting by only doing this if values changed
                if tps != latest.get("tps") or avg_block_time != latest.get("avg_block_time_ms"):
                    insert_snapshot(
                        block_height=latest.get("block_height"),
                        is_healthy=bool(latest.get("is_healthy")),
                        rpc_latency_ms=latest.get("rpc_latency_ms"),
                        tps=tps,
                        avg_block_time_ms=avg_block_time,
                    )

            # Try to pull leaderboard data from the explorer
            leaderboard_data = fetch_leaderboard_from_explorer(rpc_url)
            for entry in leaderboard_data:
                address = entry.get("address") or entry.get("pubkey") or entry.get("addr")
                if not address:
                    continue
                tx_count = int(entry.get("tx_count") or entry.get("transactions") or 0)
                programs = int(entry.get("programs_deployed") or entry.get("programs") or 0)
                upsert_leaderboard(address, tx_count, programs)

        except Exception as e:
            console.print(f"[yellow][Poller] Error: {e}[/yellow]")

        stop_event.wait(interval)


def start_poller(cfg: ThruWatchConfig, stop_event: threading.Event) -> threading.Thread:
    """Start the poller in a background thread."""
    thread = threading.Thread(
        target=poll_loop,
        args=(cfg, stop_event),
        daemon=True,
        name="ThruPoller",
    )
    thread.start()
    return thread
