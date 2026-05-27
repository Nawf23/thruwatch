"""
ThruWatch — FastAPI Server
Exposes all collected data to the dashboard frontend via a simple REST API.
Serves the dashboard HTML directly from /
"""

import time
from pathlib import Path
from typing import Optional
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from core.db import (
    get_latest_snapshot,
    get_latest_snapshots,
    get_resets,
    get_leaderboard,
    get_faucet_stats,
    get_reset_count,
)

app = FastAPI(title="ThruWatch API", version="1.0.0")

# Allow the frontend (even if served from a different port) to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

DASHBOARD_HTML = Path(__file__).parent.parent / "dashboard" / "index.html"


# ─── Dashboard ───────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Serve the ThruWatch dashboard."""
    if DASHBOARD_HTML.exists():
        return HTMLResponse(content=DASHBOARD_HTML.read_text())
    return HTMLResponse("<h1>ThruWatch</h1><p>Dashboard not found. Ensure dashboard/index.html exists.</p>")


# ─── Network Status ──────────────────────────────────────────────────────────

@app.get("/api/status")
async def network_status():
    """
    Current network health summary.
    Returns the most recent snapshot plus derived status.
    """
    snapshot = get_latest_snapshot()
    reset_count = get_reset_count()

    if not snapshot:
        return JSONResponse({
            "status": "unknown",
            "message": "No data yet — watcher may still be starting up.",
            "reset_count": reset_count,
        })

    age_seconds = int(time.time()) - snapshot["ts"]
    is_stale = age_seconds > 120  # data older than 2 minutes = stale

    if is_stale or not snapshot["is_healthy"]:
        status = "red"
        label = "Unreachable"
    elif snapshot.get("rpc_latency_ms", 0) > 3000:
        status = "yellow"
        label = "Degraded"
    else:
        status = "green"
        label = "Healthy"

    return {
        "status": status,
        "label": label,
        "block_height": snapshot.get("block_height"),
        "rpc_latency_ms": snapshot.get("rpc_latency_ms"),
        "tps": snapshot.get("tps"),
        "avg_block_time_ms": snapshot.get("avg_block_time_ms"),
        "last_updated": snapshot["ts"],
        "data_age_seconds": age_seconds,
        "reset_count": reset_count,
    }


# ─── Reset History ───────────────────────────────────────────────────────────

@app.get("/api/resets")
async def reset_history(limit: int = 20):
    """Full reset history — the data that makes ThruWatch unique."""
    resets = get_resets(limit=limit)

    # Enrich with human-readable fields
    for r in resets:
        r["downtime_human"] = _format_duration(r.get("downtime_seconds"))
        r["detected_at_human"] = _format_ts(r.get("detected_at"))

    return {"resets": resets, "total": get_reset_count()}


# ─── Leaderboard ─────────────────────────────────────────────────────────────

@app.get("/api/leaderboard")
async def leaderboard(limit: int = 20):
    """Most active wallets on the network."""
    entries = get_leaderboard(limit=limit)
    # Truncate addresses for display
    for e in entries:
        e["address_short"] = _shorten_address(e.get("address", ""))
        e["first_seen_human"] = _format_ts(e.get("first_seen_at"))
    return {"leaderboard": entries}


# ─── Faucet Stats ────────────────────────────────────────────────────────────

@app.get("/api/faucet")
async def faucet_stats():
    """How much faucet activity has ThruWatch observed."""
    return get_faucet_stats()


# ─── Sparkline Data ──────────────────────────────────────────────────────────

@app.get("/api/sparkline")
async def sparkline(points: int = 60):
    """Recent block height history for the mini chart on the dashboard."""
    snapshots = get_latest_snapshots(limit=points)
    snapshots.reverse()  # oldest first
    return {
        "points": [
            {"ts": s["ts"], "height": s.get("block_height"), "healthy": bool(s["is_healthy"])}
            for s in snapshots
        ]
    }


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _format_duration(seconds: Optional[int]) -> str:
    if seconds is None:
        return "ongoing"
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    secs = seconds % 60
    return f"{minutes}m {secs}s"


def _format_ts(ts: Optional[int]) -> str:
    if ts is None:
        return "—"
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d, %H:%M UTC")


def _shorten_address(addr: str) -> str:
    if len(addr) > 12:
        return f"{addr[:6]}...{addr[-4:]}"
    return addr
