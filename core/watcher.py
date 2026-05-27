"""
ThruWatch — Reset Watcher
Polls the Thru network every N seconds, detects resets (block height collapse),
stores history in SQLite, and fires Discord alerts.

Reset detection logic:
  - If block height drops by more than `reset_threshold` blocks → confirmed reset
  - If RPC is unreachable for 3+ consecutive polls → network unhealthy alert
"""

import time
import threading
import httpx
from typing import Optional
from rich.console import Console

from config import ThruWatchConfig
from core.db import (
    insert_snapshot,
    record_reset,
    resolve_reset,
    get_state,
    set_state,
)
from core.notifier import (
    notify_reset_detected,
    notify_recovery_complete,
    notify_network_unhealthy,
    notify_network_recovered,
)

console = Console()

# ─── RPC Health Check ────────────────────────────────────────────────────────

THRU_REST_URL = "https://rest.alphanet.thruput.org"


def ping_rpc(rpc_url: str) -> tuple[bool, Optional[int], Optional[int]]:
    """
    Poll the Thru REST API for current block height.
    Returns: (is_healthy, block_height, latency_ms)

    Uses the official REST endpoint: GET https://rest.alphanet.thruput.org/v1/height
    Response: { finalized: str, clusterExecuted: str, locallyExecuted: str }
    """
    start = time.monotonic()
    try:
        resp = httpx.get(f"{THRU_REST_URL}/v1/height", timeout=8)
        latency_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code == 200:
            data = resp.json()
            # Use finalized height as the canonical block height
            raw = data.get("finalized") or data.get("clusterExecuted") or data.get("locallyExecuted")
            height = int(raw) if raw else None
            return True, height, latency_ms

        # Non-200 but reachable — degraded but not down
        return True, None, int((time.monotonic() - start) * 1000)

    except httpx.TimeoutException:
        latency_ms = int((time.monotonic() - start) * 1000)
        # Timeout = congestion, not necessarily down (known Alphanet behaviour)
        return True, None, latency_ms

    except (httpx.ConnectError, httpx.NetworkError):
        return False, None, int((time.monotonic() - start) * 1000)

    except Exception as e:
        console.print(f"[yellow][Watcher] Poll error: {e}[/yellow]")
        return False, None, int((time.monotonic() - start) * 1000)


# ─── Watcher State ───────────────────────────────────────────────────────────

class WatcherState:
    def __init__(self):
        self.last_known_height: Optional[int] = None
        self.consecutive_failures: int = 0
        self.unhealthy_since: Optional[int] = None
        self.current_reset_number: Optional[int] = None
        self.in_reset: bool = False
        self.stop_event = threading.Event()


# ─── Core Watch Loop ─────────────────────────────────────────────────────────

def watch_loop(cfg: ThruWatchConfig, state: WatcherState, on_reset=None) -> None:
    """
    Main polling loop. Runs forever until state.stop_event is set.
    on_reset: optional callback(reset_number) triggered when a reset is detected.
    """
    rpc_url = cfg.network.rpc_url
    interval = cfg.network.poll_interval
    threshold = cfg.network.reset_threshold

    # Restore last known state from DB
    saved_height = get_state("last_block_height")
    if saved_height:
        state.last_known_height = int(saved_height)

    console.print(f"[green][ThruWatch] Watcher started. Polling every {interval}s[/green]")
    console.print(f"[dim]  RPC: {rpc_url}[/dim]")
    console.print(f"[dim]  Reset threshold: {threshold} blocks[/dim]\n")

    while not state.stop_event.is_set():
        is_healthy, block_height, latency_ms = ping_rpc(rpc_url)

        # ── Network went down ──────────────────────────────────────────────
        if not is_healthy:
            state.consecutive_failures += 1
            insert_snapshot(block_height=None, is_healthy=False, rpc_latency_ms=latency_ms)

            if state.consecutive_failures == 3:
                # Three strikes = alert
                console.print(f"[red][Watcher] RPC unreachable for 3 consecutive polls. Alerting.[/red]")
                state.unhealthy_since = int(time.time())
                notify_network_unhealthy(cfg, "RPC did not respond to 3 consecutive health checks.")

            console.print(f"[red][Watcher] Poll failed (attempt {state.consecutive_failures})[/red]")
            state.stop_event.wait(interval)
            continue

        # ── Network is up ─────────────────────────────────────────────────
        was_unhealthy = state.consecutive_failures >= 3
        state.consecutive_failures = 0

        if was_unhealthy and state.unhealthy_since:
            downtime = int(time.time()) - state.unhealthy_since
            notify_network_recovered(cfg, downtime)
            state.unhealthy_since = None

        insert_snapshot(
            block_height=block_height,
            is_healthy=True,
            rpc_latency_ms=latency_ms,
        )

        if block_height is not None:
            # ── Reset detection ───────────────────────────────────────────
            if (
                state.last_known_height is not None
                and block_height < state.last_known_height - threshold
                and not state.in_reset
            ):
                reset_number = record_reset(prev_block_height=state.last_known_height)
                state.in_reset = True
                state.current_reset_number = reset_number

                console.print(
                    f"\n[bold red]⚠️  RESET DETECTED — Reset #{reset_number}[/bold red]"
                )
                console.print(
                    f"[red]   Height dropped from {state.last_known_height:,} → {block_height:,}[/red]\n"
                )

                notify_reset_detected(cfg, reset_number, state.last_known_height)

                if on_reset:
                    on_reset(reset_number)

            # ── Reset resolved ────────────────────────────────────────────
            elif state.in_reset and block_height > threshold:
                console.print(f"[green][Watcher] Network recovered from Reset #{state.current_reset_number}[/green]")
                if state.current_reset_number:
                    resolve_reset(state.current_reset_number)
                state.in_reset = False
                state.current_reset_number = None

            state.last_known_height = block_height
            set_state("last_block_height", str(block_height))

        console.print(
            f"[dim][Watcher] ✓ height={block_height or '?'} latency={latency_ms}ms[/dim]"
        )
        state.stop_event.wait(interval)


def start_watcher(cfg: ThruWatchConfig, on_reset=None) -> tuple[threading.Thread, WatcherState]:
    """
    Start the watcher in a background thread.
    Returns (thread, state) — call state.stop_event.set() to stop it.
    """
    state = WatcherState()
    thread = threading.Thread(
        target=watch_loop,
        args=(cfg, state, on_reset),
        daemon=True,
        name="ThruWatcher",
    )
    thread.start()
    return thread, state
