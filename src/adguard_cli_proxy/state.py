"""Persistent state for active proxy endpoints."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .instances import validate_instance

APP_NAME = "adguard-cli-proxy"


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / ".config" / APP_NAME


def state_path(instance: str | None = None) -> Path:
    if instance:
        return config_dir() / "instances" / f"{validate_instance(instance)}.json"
    return config_dir() / "state.json"


@dataclass
class State:
    iso: str
    country: str
    city: str
    slug: str
    listen_host: str
    listen_port: int
    advertise_host: str
    endpoint: str
    proxy_url: str
    username: str | None
    password: str | None
    egress_ip: str | None
    connected_at: str
    instance: str | None = None
    data_dir: str | None = None

    @property
    def healthy(self) -> bool:
        return bool(self.egress_ip)


def save_state(state: State) -> Path:
    path = state_path(state.instance)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps(asdict(state), indent=2, sort_keys=True) + "\n"

    fd, temporary_name = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return path


def load_state(instance: str | None = None) -> State | None:
    path = state_path(instance)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return State(**data)
    except (OSError, ValueError, TypeError):
        return None


def clear_state(instance: str | None = None) -> None:
    try:
        state_path(instance).unlink()
    except FileNotFoundError:
        pass


def list_instances() -> list[str]:
    directory = config_dir() / "instances"
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return []
    return [path.stem for path in paths]
