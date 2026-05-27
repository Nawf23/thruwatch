"""
ThruWatch — Config Loader
Reads thruwatch.toml from the current directory or a specified path.
"""

import os
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib
    except ImportError:
        raise ImportError("Install tomli: pip install tomli")


DEFAULT_CONFIG_PATH = Path("thruwatch.toml")


@dataclass
class NetworkConfig:
    rpc_url: str = "https://grpc.alphanet.thruput.org"
    poll_interval: int = 30
    reset_threshold: int = 100


@dataclass
class WalletConfig:
    key_file: str = "~/.thru/default.key"
    address: str = ""
    faucet_amount: int = 100


@dataclass
class RecoveryConfig:
    auto_recover: bool = True
    recovery_delay: int = 60
    contracts: List[str] = field(default_factory=list)


@dataclass
class NotificationsConfig:
    discord_webhook: str = ""
    notify_on_reset_detected: bool = True
    notify_on_recovery_complete: bool = True
    notify_on_network_unhealthy: bool = True


@dataclass
class DashboardConfig:
    port: int = 8000
    host: str = "127.0.0.1"


@dataclass
class ThruWatchConfig:
    network: NetworkConfig = field(default_factory=NetworkConfig)
    wallet: WalletConfig = field(default_factory=WalletConfig)
    recovery: RecoveryConfig = field(default_factory=RecoveryConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)


def _apply_env_overrides(cfg: "ThruWatchConfig") -> "ThruWatchConfig":
    """
    Override config values from environment variables.
    Useful for Railway / Docker deployments where thruwatch.toml isn't present.

    Supported env vars:
      THRU_ADDRESS          — your Thru public key
      THRU_DISCORD_WEBHOOK  — Discord webhook URL
      THRU_FAUCET_AMOUNT    — amount to request from faucet (default 100)
      THRU_AUTO_RECOVER     — "true" or "false"
      THRU_POLL_INTERVAL    — polling interval in seconds
    """
    import os
    if os.environ.get("THRU_ADDRESS"):
        cfg.wallet.address = os.environ["THRU_ADDRESS"]
    if os.environ.get("THRU_DISCORD_WEBHOOK"):
        cfg.notifications.discord_webhook = os.environ["THRU_DISCORD_WEBHOOK"]
    if os.environ.get("THRU_FAUCET_AMOUNT"):
        cfg.wallet.faucet_amount = int(os.environ["THRU_FAUCET_AMOUNT"])
    if os.environ.get("THRU_AUTO_RECOVER"):
        cfg.recovery.auto_recover = os.environ["THRU_AUTO_RECOVER"].lower() == "true"
    if os.environ.get("THRU_POLL_INTERVAL"):
        cfg.network.poll_interval = int(os.environ["THRU_POLL_INTERVAL"])
    return cfg


def load_config(path: Optional[Path] = None) -> ThruWatchConfig:
    """Load config from a TOML file. Falls back to defaults if file not found."""
    config_path = path or DEFAULT_CONFIG_PATH

    if not config_path.exists():
        print(f"[ThruWatch] No config file found at '{config_path}'. Using defaults + env vars.")
        return _apply_env_overrides(ThruWatchConfig())

    with open(config_path, "rb") as f:
        raw = tomllib.load(f)

    cfg = ThruWatchConfig()

    if "network" in raw:
        n = raw["network"]
        cfg.network = NetworkConfig(
            rpc_url=n.get("rpc_url", cfg.network.rpc_url),
            poll_interval=n.get("poll_interval", cfg.network.poll_interval),
            reset_threshold=n.get("reset_threshold", cfg.network.reset_threshold),
        )

    if "wallet" in raw:
        w = raw["wallet"]
        cfg.wallet = WalletConfig(
            key_file=w.get("key_file", cfg.wallet.key_file),
            address=w.get("address", cfg.wallet.address),
            faucet_amount=w.get("faucet_amount", cfg.wallet.faucet_amount),
        )

    if "recovery" in raw:
        r = raw["recovery"]
        cfg.recovery = RecoveryConfig(
            auto_recover=r.get("auto_recover", cfg.recovery.auto_recover),
            recovery_delay=r.get("recovery_delay", cfg.recovery.recovery_delay),
            contracts=r.get("contracts", cfg.recovery.contracts),
        )

    if "notifications" in raw:
        n = raw["notifications"]
        cfg.notifications = NotificationsConfig(
            discord_webhook=n.get("discord_webhook", cfg.notifications.discord_webhook),
            notify_on_reset_detected=n.get("notify_on_reset_detected", True),
            notify_on_recovery_complete=n.get("notify_on_recovery_complete", True),
            notify_on_network_unhealthy=n.get("notify_on_network_unhealthy", True),
        )

    if "dashboard" in raw:
        d = raw["dashboard"]
        cfg.dashboard = DashboardConfig(
            port=d.get("port", cfg.dashboard.port),
            host=d.get("host", cfg.dashboard.host),
        )

    return _apply_env_overrides(cfg)
