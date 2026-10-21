"""Small, dependency-free wrapper around the ``adguardvpn-cli`` executable."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
LOCATION_RE = re.compile(
    r"^(?P<iso>[A-Z]{2})\s{2,}(?P<country>.+?)\s{2,}"
    r"(?P<city>.+?)\s{2,}(?P<ping>\d+)\s*$"
)
STATUS_RE = re.compile(
    r"Connected to (?P<location>.+?) in (?P<mode>[A-Za-z0-9_-]+) mode, "
    r"listening on (?P<host>.+):(?P<port>\d+)",
    re.IGNORECASE,
)


class AdGuardError(RuntimeError):
    """Raised when the AdGuard VPN CLI cannot complete an operation."""


def strip_ansi(value: str) -> str:
    return ANSI_RE.sub("", value)


@dataclass(frozen=True)
class Location:
    iso: str
    country: str
    city: str
    ping: int = 0

    @property
    def slug(self) -> str:
        return f"{self.iso}/{slugify(self.city)}"

    @property
    def label(self) -> str:
        return f"{self.city}, {self.country}"


@dataclass(frozen=True)
class VpnStatus:
    connected: bool
    location: str | None = None
    mode: str | None = None
    host: str | None = None
    port: int | None = None
    raw: str = ""


def slugify(value: str) -> str:
    """Turn a city name into the upper-case path component used by endpoints."""
    value = value.strip().upper().replace("&", " AND ")
    value = re.sub(r"[^A-Z0-9]+", "-", value)
    return value.strip("-")


def parse_locations(output: str) -> list[Location]:
    """Parse the fixed-width table emitted by ``adguardvpn-cli list-locations``."""
    locations: list[Location] = []
    seen: set[tuple[str, str]] = set()

    for raw_line in strip_ansi(output).splitlines():
        line = raw_line.rstrip()
        if not line or line.startswith("ISO "):
            continue
        match = LOCATION_RE.match(line)
        if not match:
            continue
        key = (match.group("iso"), match.group("city"))
        if key in seen:
            continue
        seen.add(key)
        locations.append(
            Location(
                iso=match.group("iso"),
                country=match.group("country").strip(),
                city=match.group("city").strip(),
                ping=int(match.group("ping")),
            )
        )

    return locations


class AdGuardCLI:
    def __init__(self, binary: str | None = None) -> None:
        self.binary = binary or os.environ.get("ADGUARD_CLI_PROXY_BIN", "adguardvpn-cli")

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def _run(
        self,
        args: Sequence[str],
        *,
        timeout: float = 60,
        check: bool = True,
    ) -> str:
        if not self.available():
            raise AdGuardError(
                f"{self.binary!r} was not found. Install AdGuard VPN CLI first: "
                "https://adguard-vpn.com/en/windows/overview.html"
            )

        env = os.environ.copy()
        env["NO_COLOR"] = "1"
        env.setdefault("TERM", "dumb")

        try:
            completed = subprocess.run(
                [self.binary, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AdGuardError(
                f"`{self.binary} {' '.join(args)}` timed out after {timeout:g}s"
            ) from exc
        except OSError as exc:
            raise AdGuardError(f"Could not run {self.binary!r}: {exc}") from exc

        output = strip_ansi(completed.stdout + completed.stderr).strip()
        if check and completed.returncode != 0:
            detail = output or "no output"
            raise AdGuardError(
                f"`{self.binary} {' '.join(args)}` failed "
                f"(exit {completed.returncode}): {detail}"
            )
        return output

    def version(self) -> str:
        return self._run(["--version"])

    def list_locations(self) -> list[Location]:
        output = self._run(["list-locations"], timeout=30)
        locations = parse_locations(output)
        if not locations:
            raise AdGuardError("AdGuard VPN CLI returned no locations")
        return locations

    def status(self) -> VpnStatus:
        output = self._run(["status"], timeout=20, check=False)
        lowered = output.lower()
        if "disconnected" in lowered:
            return VpnStatus(connected=False, raw=output)

        match = STATUS_RE.search(output)
        if not match:
            return VpnStatus(connected=False, raw=output)

        return VpnStatus(
            connected=True,
            location=match.group("location").strip(),
            mode=match.group("mode").lower(),
            host=match.group("host").strip(),
            port=int(match.group("port")),
            raw=output,
        )

    def configure_socks(
        self,
        host: str,
        port: int,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self._run(["config", "set-mode", "socks"])

        if username and password:
            self._run(
                [
                    "config",
                    "set-socks-host",
                    host,
                    "--username",
                    username,
                    "--password",
                    password,
                ]
            )
        else:
            self._run(["config", "clear-socks-auth"])
            self._run(["config", "set-socks-host", host])

        self._run(["config", "set-socks-port", str(port)])

    def connect(self, location: Location, timeout: float = 90) -> str:
        return self._run(
            [
                "connect",
                "-l",
                location.city,
                "--yes",
                "--no-progress",
            ],
            timeout=timeout,
        )

    def disconnect(self) -> str:
        return self._run(["disconnect"], timeout=30, check=False)

    def license(self) -> str:
        return self._run(["license"], timeout=20, check=False)
