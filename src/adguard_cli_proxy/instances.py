"""Instance names and per-instance AdGuard VPN data directories."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

INSTANCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class InstanceError(ValueError):
    """Raised for invalid or unsafe instance names."""


def validate_instance(name: str) -> str:
    name = name.strip()
    if not INSTANCE_RE.fullmatch(name):
        raise InstanceError(
            "Instance names must be 1-64 characters using letters, digits, "
            "dots, underscores, or hyphens, and must start with a letter or digit"
        )
    return name


def data_home() -> Path:
    value = os.environ.get("XDG_DATA_HOME")
    if value:
        return Path(value)
    return Path.home() / ".local" / "share"


def default_data_dir() -> Path:
    value = os.environ.get("AGVPN_CLI_DATA_PATH")
    if value:
        return Path(value).expanduser()
    return data_home() / "adguardvpn-cli"


def instance_data_dir(name: str) -> Path:
    return data_home() / "adguard-cli-proxy" / "instances" / validate_instance(name)


def resolve_data_dir(name: str, explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    return instance_data_dir(name)


def seed_login(data_dir: Path, source: Path | None = None) -> Path | None:
    """Copy an existing AdGuard login into a new instance data directory.

    AdGuard VPN CLI stores its account session in ``adguardvpn-cli.conf``.
    A separate data directory gives each VPN connection its own daemon and
    SOCKS listener. Returns the source path when a copy was made.
    """
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = data_dir / "adguardvpn-cli.conf"
    if target.exists():
        return None

    source = source or (default_data_dir() / "adguardvpn-cli.conf")
    if not source.exists():
        return None

    shutil.copy2(source, target)
    target.chmod(0o600)
    return source
