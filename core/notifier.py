"""
ThruWatch — Discord Notifier
Sends rich Discord embeds via webhook when resets or network issues are detected.
"""

import time
import httpx
from typing import Optional
from config import ThruWatchConfig


def _ts_to_human(ts: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _post_webhook(webhook_url: str, payload: dict) -> bool:
    """Send a payload to a Discord webhook. Returns True on success."""
    if not webhook_url:
        return False
    try:
        resp = httpx.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[ThruWatch][Notifier] Discord webhook failed: {e}")
        return False


def notify_reset_detected(cfg: ThruWatchConfig, reset_number: int, prev_height: int) -> None:
    """Fire when a reset is first confirmed."""
    if not cfg.notifications.notify_on_reset_detected:
        return
    if not cfg.notifications.discord_webhook:
        return

    payload = {
        "username": "ThruWatch",
        "avatar_url": "https://thru.org/favicon.ico",
        "embeds": [
            {
                "title": f"⚠️ Thru Alphanet Reset Detected — Reset #{reset_number}",
                "color": 0xFF4444,
                "fields": [
                    {
                        "name": "Previous Block Height",
                        "value": f"`{prev_height:,}`",
                        "inline": True,
                    },
                    {
                        "name": "Detected At",
                        "value": _ts_to_human(int(time.time())),
                        "inline": True,
                    },
                    {
                        "name": "What to do",
                        "value": (
                            "Don't panic — your private key is safe.\n"
                            "ThruWatch is running auto-recovery. If you need to do it manually:\n"
                            "```\nthru account create\nthru faucet withdraw <ADDRESS> 100\n```\n"
                            "Then check `thru account info` — the timeout error is a false alarm 9/10 times."
                        ),
                        "inline": False,
                    },
                ],
                "footer": {"text": "ThruWatch • Alphanet Monitor"},
            }
        ],
    }
    _post_webhook(cfg.notifications.discord_webhook, payload)


def notify_recovery_complete(
    cfg: ThruWatchConfig,
    reset_number: int,
    address: str,
    new_balance: Optional[str] = None,
    contracts_redeployed: int = 0,
) -> None:
    """Fire when auto-recovery finishes successfully."""
    if not cfg.notifications.notify_on_recovery_complete:
        return
    if not cfg.notifications.discord_webhook:
        return

    fields = [
        {"name": "Address", "value": f"`{address}`", "inline": False},
        {"name": "Reset", "value": f"#{reset_number}", "inline": True},
    ]
    if new_balance:
        fields.append({"name": "New Balance", "value": new_balance, "inline": True})
    if contracts_redeployed > 0:
        fields.append({
            "name": "Contracts Redeployed",
            "value": str(contracts_redeployed),
            "inline": True,
        })

    payload = {
        "username": "ThruWatch",
        "avatar_url": "https://thru.org/favicon.ico",
        "embeds": [
            {
                "title": f"✅ Auto-Recovery Complete — Reset #{reset_number}",
                "color": 0x44FF88,
                "fields": fields,
                "footer": {"text": "ThruWatch • Alphanet Monitor"},
            }
        ],
    }
    _post_webhook(cfg.notifications.discord_webhook, payload)


def notify_network_unhealthy(cfg: ThruWatchConfig, reason: str) -> None:
    """Fire when the RPC becomes unreachable (not a reset, just downtime)."""
    if not cfg.notifications.notify_on_network_unhealthy:
        return
    if not cfg.notifications.discord_webhook:
        return

    payload = {
        "username": "ThruWatch",
        "avatar_url": "https://thru.org/favicon.ico",
        "embeds": [
            {
                "title": "🔴 Thru RPC Unreachable",
                "color": 0xFF8800,
                "fields": [
                    {"name": "Reason", "value": reason, "inline": False},
                    {"name": "Time", "value": _ts_to_human(int(time.time())), "inline": True},
                ],
                "footer": {"text": "ThruWatch • Alphanet Monitor"},
            }
        ],
    }
    _post_webhook(cfg.notifications.discord_webhook, payload)


def notify_network_recovered(cfg: ThruWatchConfig, downtime_seconds: int) -> None:
    """Fire when the RPC comes back after being unreachable."""
    if not cfg.notifications.discord_webhook:
        return

    minutes = downtime_seconds // 60
    seconds = downtime_seconds % 60
    duration = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"

    payload = {
        "username": "ThruWatch",
        "avatar_url": "https://thru.org/favicon.ico",
        "embeds": [
            {
                "title": "🟢 Thru RPC Back Online",
                "color": 0x44FF88,
                "fields": [
                    {"name": "Downtime", "value": duration, "inline": True},
                    {"name": "Time", "value": _ts_to_human(int(time.time())), "inline": True},
                ],
                "footer": {"text": "ThruWatch • Alphanet Monitor"},
            }
        ],
    }
    _post_webhook(cfg.notifications.discord_webhook, payload)
