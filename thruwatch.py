#!/usr/bin/env python3
"""
ThruWatch — Main CLI Entry Point

Usage:
  thruwatch start      Start the watcher, poller, auto-recovery, and dashboard server
  thruwatch watch      Start only the watcher (no dashboard server)
  thruwatch recover    Manually trigger recovery (useful after a manual reset check)
  thruwatch status     Print current network status to the terminal
  thruwatch init       Copy thruwatch.toml.example → thruwatch.toml

Install:
  pip install -r requirements.txt
  python thruwatch.py start
"""

import sys
import time
import shutil
import threading
from pathlib import Path

import click
import uvicorn
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

# ── Local imports ──────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

from config import load_config
from core.db import init_db, get_latest_snapshot, get_resets, get_reset_count, get_faucet_stats
from core.watcher import start_watcher
from core.poller import start_poller
from core.recovery import run_recovery

console = Console()

BANNER = """
[bold cyan]╔══════════════════════════════════╗
║       ThruWatch v1.0.0           ║
║  Thru Alphanet Community Monitor ║
╚══════════════════════════════════╝[/bold cyan]
"""


# ─── CLI Group ────────────────────────────────────────────────────────────────

@click.group()
def cli():
    """ThruWatch — Thru Alphanet Reset Monitor & Recovery Toolkit."""
    pass


# ─── start ───────────────────────────────────────────────────────────────────

@cli.command()
@click.option('--config', default='thruwatch.toml', help='Path to config file.')
@click.option('--no-server', is_flag=True, help='Run watcher only, no dashboard server.')
@click.option('--no-recovery', is_flag=True, help='Disable auto-recovery on reset.')
def start(config, no_server, no_recovery):
    """Start ThruWatch: watcher + auto-recovery + dashboard."""
    console.print(BANNER)

    cfg = load_config(Path(config))
    init_db()

    console.print(f"[dim]Config: {config}[/dim]")
    console.print(f"[dim]RPC:    {cfg.network.rpc_url}[/dim]")
    console.print(f"[dim]Poll:   every {cfg.network.poll_interval}s[/dim]")
    if cfg.notifications.discord_webhook:
        console.print(f"[dim]Discord: webhook configured ✓[/dim]")
    console.print()

    stop_event = threading.Event()

    # Auto-recovery callback
    def on_reset(reset_number: int):
        if no_recovery or not cfg.recovery.auto_recover:
            console.print(f"[yellow][ThruWatch] Auto-recovery disabled. Run `thruwatch recover` manually.[/yellow]")
            return
        # Run recovery in a separate thread so the watcher keeps polling
        threading.Thread(
            target=run_recovery,
            args=(cfg, reset_number),
            daemon=True,
            name=f"Recovery-{reset_number}",
        ).start()

    # Start watcher
    watcher_thread, watcher_state = start_watcher(cfg, on_reset=on_reset)

    # Start poller
    poller_thread = start_poller(cfg, stop_event)

    if not no_server:
        # Import the FastAPI app
        from api.server import app

        console.print(f"\n[green]✓ Dashboard available at: http://{cfg.dashboard.host}:{cfg.dashboard.port}[/green]\n")

        # Run uvicorn (blocking — this keeps the process alive)
        try:
            uvicorn.run(
                app,
                host=cfg.dashboard.host,
                port=cfg.dashboard.port,
                log_level="warning",  # quiet mode — let ThruWatch handle its own output
            )
        except KeyboardInterrupt:
            pass
    else:
        console.print("[green]✓ Watcher running. Press Ctrl+C to stop.[/green]\n")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    console.print("\n[yellow]Shutting down ThruWatch...[/yellow]")
    stop_event.set()
    watcher_state.stop_event.set()


# ─── watch ───────────────────────────────────────────────────────────────────

@cli.command()
@click.option('--config', default='thruwatch.toml', help='Path to config file.')
def watch(config):
    """Start only the reset watcher (lightweight, no server)."""
    cfg = load_config(Path(config))
    init_db()

    console.print("[bold cyan]ThruWatch[/bold cyan] — Watcher mode\n")

    def on_reset(reset_number):
        if cfg.recovery.auto_recover:
            run_recovery(cfg, reset_number)

    thread, state = start_watcher(cfg, on_reset=on_reset)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        state.stop_event.set()
        console.print("\n[yellow]Watcher stopped.[/yellow]")


# ─── recover ─────────────────────────────────────────────────────────────────

@cli.command()
@click.option('--config', default='thruwatch.toml', help='Path to config file.')
@click.option('--reset-number', default=0, help='Reset number to associate with this recovery.')
def recover(config, reset_number):
    """Manually trigger the full recovery sequence."""
    cfg = load_config(Path(config))
    init_db()

    if reset_number == 0:
        reset_number = get_reset_count() + 1

    success = run_recovery(cfg, reset_number)
    sys.exit(0 if success else 1)


# ─── status ──────────────────────────────────────────────────────────────────

@cli.command()
@click.option('--config', default='thruwatch.toml', help='Path to config file.')
def status(config):
    """Print current network status from the local database."""
    cfg = load_config(Path(config))
    init_db()

    snapshot = get_latest_snapshot()
    resets = get_resets(limit=5)
    faucet = get_faucet_stats()
    reset_count = get_reset_count()

    console.print(BANNER)

    if not snapshot:
        console.print("[yellow]No data yet. Start the watcher with `thruwatch start`.[/yellow]")
        return

    # Network health
    age = int(time.time()) - snapshot["ts"]
    if not snapshot["is_healthy"] or age > 120:
        health_str = "[red]● UNHEALTHY[/red]"
    elif snapshot.get("rpc_latency_ms", 0) > 3000:
        health_str = "[yellow]● DEGRADED[/yellow]"
    else:
        health_str = "[green]● HEALTHY[/green]"

    console.print(Panel(
        f"  Status      : {health_str}\n"
        f"  Block Height: [bold]{snapshot.get('block_height') or '?'}[/bold]\n"
        f"  RPC Latency : {snapshot.get('rpc_latency_ms') or '?'}ms\n"
        f"  TPS (est.)  : {snapshot.get('tps') or '?'}\n"
        f"  Data Age    : {age}s ago",
        title="[cyan]Network[/cyan]",
        border_style="cyan",
    ))

    # Reset history
    if resets:
        table = Table(title=f"Reset History (total: {reset_count})", box=box.SIMPLE)
        table.add_column("#", style="red bold", width=4)
        table.add_column("Detected At")
        table.add_column("Prev Height", justify="right")
        table.add_column("Downtime")

        for r in resets:
            from datetime import datetime, timezone
            detected = datetime.fromtimestamp(r["detected_at"], tz=timezone.utc).strftime("%b %d %H:%M")
            height = f"{r['prev_block_height']:,}" if r.get('prev_block_height') else "—"
            if r.get("downtime_seconds"):
                secs = r["downtime_seconds"]
                downtime = f"{secs//60}m {secs%60}s"
            else:
                downtime = "[red]ongoing[/red]"
            table.add_row(str(r["reset_number"]), detected, height, downtime)

        console.print(table)
    else:
        console.print("\n[green]No resets recorded yet — network has been stable.[/green]")

    # Faucet
    console.print(f"\n[dim]Faucet calls today: {faucet['calls_last_24h']} / total: {faucet['total_calls']}[/dim]")


# ─── init ─────────────────────────────────────────────────────────────────────

@cli.command("init")
def init_config():
    """Create thruwatch.toml from the example config."""
    src = Path(__file__).parent / "thruwatch.toml.example"
    dst = Path("thruwatch.toml")

    if dst.exists():
        console.print(f"[yellow]thruwatch.toml already exists. Delete it first if you want to reset.[/yellow]")
        return

    if not src.exists():
        console.print(f"[red]Example config not found at {src}[/red]")
        return

    shutil.copy(src, dst)
    console.print(f"[green]✓ Created thruwatch.toml[/green]")
    console.print("\nNext steps:")
    console.print("  1. Edit [bold]thruwatch.toml[/bold] and set your wallet address")
    console.print("  2. Add your Discord webhook URL for reset alerts")
    console.print("  3. Run [bold]python thruwatch.py start[/bold]")


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()
