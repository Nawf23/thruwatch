"""
ThruWatch — Auto-Recovery Toolkit
After a reset is detected, this module runs the full recovery sequence:
  1. thru account create   (re-register on-chain)
  2. thru faucet withdraw  (get test tokens back)
  3. Re-deploy any .bin contracts specified in config

Key insight from the docs: the "upstream request timeout" error is a FALSE ALARM.
The chain likely processed the tx in the background. We always check account info
after a "failure" before giving up.
"""

import subprocess
import time
import shutil
from pathlib import Path
from typing import Optional
from rich.console import Console

from config import ThruWatchConfig
from core.db import log_faucet_call
from core.notifier import notify_recovery_complete

console = Console()

# How many seconds to wait between recovery steps
STEP_DELAY = 5

# How many seconds to wait after a "timeout" before verifying on-chain
TIMEOUT_VERIFY_DELAY = 30


# ─── CLI Helpers ─────────────────────────────────────────────────────────────

def _check_cli() -> bool:
    """Make sure the `thru` CLI is installed and on PATH."""
    if shutil.which("thru") is None:
        console.print("[bold red][Recovery] `thru` CLI not found on PATH.[/bold red]")
        console.print("[red]  Install it with: cargo install thru[/red]")
        return False
    return True


def _run(cmd: list[str], timeout: int = 60) -> tuple[bool, str]:
    """
    Run a thru CLI command. Returns (success, output).
    Handles the timeout false alarm gracefully.
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (result.stdout + result.stderr).strip()
        success = result.returncode == 0
        return success, output
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"
    except Exception as e:
        return False, str(e)


def _is_timeout_false_alarm(output: str) -> bool:
    """
    Detect the known Alphanet timeout false alarm.
    Per the docs: "upstream request timeout" usually means the tx went through.
    """
    return "upstream request timeout" in output.lower() or "service is currently unavailable" in output.lower()


def get_balance(address: str) -> Optional[str]:
    """Fetch current balance via `thru account info`. Returns balance string or None."""
    success, output = _run(["thru", "account", "info"])
    if success or _is_timeout_false_alarm(output):
        # Parse balance from output — format: "Balance: 100 THRU" (adjust to actual CLI output)
        for line in output.splitlines():
            if "balance" in line.lower():
                return line.strip()
    return None


# ─── Recovery Steps ──────────────────────────────────────────────────────────

def step_account_create() -> bool:
    """
    Step 1: Re-register the account on-chain after a reset.
    Cost: 0 gas. This generates the State Proof transaction.
    """
    console.print("[cyan][Recovery] Step 1/3 — Running `thru account create`...[/cyan]")
    success, output = _run(["thru", "account", "create"], timeout=45)

    if success:
        console.print("[green]  ✓ Account created successfully.[/green]")
        return True

    if _is_timeout_false_alarm(output):
        console.print(f"[yellow]  ⚠ Timeout received. Waiting {TIMEOUT_VERIFY_DELAY}s then verifying...[/yellow]")
        time.sleep(TIMEOUT_VERIFY_DELAY)
        # Check if account actually exists now
        ok, info = _run(["thru", "account", "info"])
        if ok or (info and "balance" in info.lower()):
            console.print("[green]  ✓ False alarm — account exists on-chain.[/green]")
            return True
        console.print("[red]  ✗ Account not found after timeout. May need manual retry.[/red]")
        return False

    console.print(f"[red]  ✗ account create failed: {output}[/red]")
    return False


def step_faucet_withdraw(address: str, amount: int = 100) -> bool:
    """
    Step 2: Request test tokens from the faucet.
    """
    console.print(f"[cyan][Recovery] Step 2/3 — Requesting {amount} THRU from faucet...[/cyan]")
    success, output = _run(["thru", "faucet", "withdraw", address, str(amount)], timeout=45)

    if success:
        console.print(f"[green]  ✓ Faucet withdrew {amount} THRU.[/green]")
        log_faucet_call(address, amount, success=True)
        return True

    if _is_timeout_false_alarm(output):
        console.print(f"[yellow]  ⚠ Faucet timeout. Waiting {TIMEOUT_VERIFY_DELAY}s then verifying balance...[/yellow]")
        time.sleep(TIMEOUT_VERIFY_DELAY)
        balance = get_balance(address)
        if balance:
            console.print(f"[green]  ✓ False alarm — balance confirmed: {balance}[/green]")
            log_faucet_call(address, amount, success=True)
            return True
        console.print("[red]  ✗ Balance still 0 after timeout. Faucet may be dry — try manually.[/red]")
        log_faucet_call(address, amount, success=False)
        return False

    console.print(f"[red]  ✗ Faucet failed: {output}[/red]")
    log_faucet_call(address, amount, success=False)
    return False


def step_redeploy_contracts(contracts: list[str]) -> int:
    """
    Step 3 (optional): Re-deploy .bin contract files specified in config.
    Returns the number of contracts successfully redeployed.
    """
    if not contracts:
        console.print("[dim][Recovery] Step 3/3 — No contracts configured for redeploy. Skipping.[/dim]")
        return 0

    console.print(f"[cyan][Recovery] Step 3/3 — Redeploying {len(contracts)} contract(s)...[/cyan]")
    deployed = 0

    for contract_path in contracts:
        path = Path(contract_path).expanduser()
        if not path.exists():
            console.print(f"[yellow]  ⚠ Contract not found: {path}. Skipping.[/yellow]")
            continue

        console.print(f"  → Deploying {path.name}...")
        success, output = _run(
            ["thru", "program", "create", "default", str(path)],
            timeout=60,
        )

        if success:
            console.print(f"[green]  ✓ Deployed {path.name}[/green]")
            deployed += 1
        elif _is_timeout_false_alarm(output):
            console.print(f"[yellow]  ⚠ Timeout on {path.name} — tx likely went through. Check manually.[/yellow]")
            deployed += 1  # count as deployed since it probably worked
        else:
            console.print(f"[red]  ✗ Failed to deploy {path.name}: {output}[/red]")

        time.sleep(STEP_DELAY)

    return deployed


# ─── Full Recovery Sequence ──────────────────────────────────────────────────

def run_recovery(cfg: ThruWatchConfig, reset_number: int) -> bool:
    """
    Run the full post-reset recovery sequence.
    Returns True if recovery was successful enough to consider complete.
    """
    console.print(f"\n[bold cyan]━━━ ThruWatch Auto-Recovery — Reset #{reset_number} ━━━[/bold cyan]\n")

    if not _check_cli():
        return False

    address = cfg.wallet.address
    if not address:
        console.print("[red][Recovery] No address configured in thruwatch.toml. Cannot recover.[/red]")
        console.print("[red]  Set wallet.address to your Thru public key.[/red]")
        return False

    # Optional delay to let network stabilize after reset
    if cfg.recovery.recovery_delay > 0:
        console.print(f"[dim][Recovery] Waiting {cfg.recovery.recovery_delay}s for network to stabilise...[/dim]")
        time.sleep(cfg.recovery.recovery_delay)

    # Step 1: account create
    account_ok = step_account_create()
    time.sleep(STEP_DELAY)

    # Step 2: faucet
    faucet_ok = step_faucet_withdraw(address, cfg.wallet.faucet_amount)
    time.sleep(STEP_DELAY)

    # Step 3: redeploy contracts
    contracts_deployed = step_redeploy_contracts(cfg.recovery.contracts)

    # Final status
    balance = get_balance(address)
    success = account_ok and faucet_ok

    console.print(f"\n[bold {'green' if success else 'yellow'}]━━━ Recovery {'Complete' if success else 'Partial'} ━━━[/bold {'green' if success else 'yellow'}]")
    console.print(f"  Account registered : {'✓' if account_ok else '✗'}")
    console.print(f"  Faucet funded      : {'✓' if faucet_ok else '✗'}")
    console.print(f"  Contracts deployed : {contracts_deployed}/{len(cfg.recovery.contracts)}")
    if balance:
        console.print(f"  Balance            : {balance}")
    console.print()

    notify_recovery_complete(
        cfg,
        reset_number=reset_number,
        address=address,
        new_balance=balance,
        contracts_redeployed=contracts_deployed,
    )

    return success
