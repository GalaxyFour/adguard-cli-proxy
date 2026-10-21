"""Command line interface for adguard-cli-proxy."""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .adguard import AdGuardCLI, AdGuardError, Location
from .endpoint import (
    LocationError,
    connect_host,
    format_endpoint,
    format_proxy_url,
    is_loopback,
    is_wildcard,
    resolve_location,
)
from .gateway import GatewayConfig, GatewayError
from .gateway import serve as serve_gateway
from .instances import (
    InstanceError,
    instance_data_dir,
    resolve_data_dir,
    seed_login,
    validate_instance,
)
from .net import direct_public_ip, local_ipv4, probe_egress, probe_tcp
from .state import (
    State,
    clear_state,
    list_instances,
    load_state,
    save_state,
    state_path,
)


def _add_instance_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--instance",
        help=(
            "named VPN instance with its own data directory and daemon; "
            "required for simultaneous locations"
        ),
    )
    parser.add_argument(
        "--data-dir",
        help="override the AdGuard data directory for --instance",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="adguard-cli-proxy",
        description=(
            "Run and label a SOCKS5 proxy backed by AdGuard VPN CLI. "
            "Endpoints use the form {ip}:{port}/{ISO}/{CITY-SLUG}. "
            "The optional gateway on port 9111 maps that form to backend "
            "ports 9112-9222, for example 127.0.0.1:9111/US/LAS-VEGAS."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    up = subparsers.add_parser(
        "up",
        help="connect to a location and expose its SOCKS5 proxy",
        description="Configure AdGuard VPN SOCKS mode, connect, and print the endpoint.",
    )
    up.add_argument("location", help="e.g. US/LAS-VEGAS, 'Las Vegas', US, or 'United States'")
    up.add_argument(
        "--host",
        default="127.0.0.1",
        help="SOCKS listen address (default: 127.0.0.1)",
    )
    up.add_argument(
        "--port", type=int, default=9112, help="SOCKS listen port (default: 9112)"
    )
    up.add_argument(
        "--advertise",
        metavar="HOST",
        help="IP/hostname to put in the printed endpoint (default: listen host or public IP)",
    )
    up.add_argument(
        "--no-public-ip",
        action="store_true",
        help="when binding a wildcard address, advertise the LAN IP instead of the public IP",
    )
    up.add_argument(
        "--no-auth",
        action="store_true",
        help="disable SOCKS authentication (only allowed for loopback binds)",
    )
    up.add_argument("--username", help="explicit SOCKS username")
    up.add_argument("--password", help="explicit SOCKS password")
    up.add_argument(
        "--wait",
        type=float,
        default=30,
        metavar="SECONDS",
        help="how long to wait for the tunnel (default: 30)",
    )
    up.add_argument(
        "--skip-egress-check",
        action="store_true",
        help="do not verify the public IP through the proxy",
    )
    up.add_argument(
        "--no-copy-login",
        action="store_true",
        help="do not seed a new --instance data dir with the default AdGuard login",
    )
    _add_instance_arguments(up)
    up.add_argument("--json", action="store_true", help="print machine-readable JSON")
    up.set_defaults(func=cmd_up)

    down = subparsers.add_parser("down", help="disconnect and remove the active endpoint")
    _add_instance_arguments(down)
    down.add_argument("--json", action="store_true", help="print machine-readable JSON")
    down.set_defaults(func=cmd_down)

    status = subparsers.add_parser("status", help="show the current proxy status")
    _add_instance_arguments(status)
    status.add_argument("--json", action="store_true", help="print machine-readable JSON")
    status.add_argument(
        "--all", action="store_true", help="show every configured instance"
    )
    status.add_argument(
        "--probe", action="store_true", help="probe the egress IP through the proxy"
    )
    status.add_argument(
        "--check",
        action="store_true",
        help="exit with status 3 when the VPN is not connected",
    )
    status.set_defaults(func=cmd_status)

    listing = subparsers.add_parser(
        "list", help="list AdGuard VPN locations and their endpoint labels"
    )
    listing.add_argument("--country", help="filter by ISO code or country name")
    listing.add_argument("--search", help="filter by a case-insensitive substring")
    listing.add_argument("--json", action="store_true", help="print machine-readable JSON")
    listing.set_defaults(func=cmd_list)

    resolve = subparsers.add_parser(
        "resolve", help="resolve a location query to its ISO/CITY-SLUG label"
    )
    resolve.add_argument("location")
    resolve.add_argument("--json", action="store_true", help="print machine-readable JSON")
    resolve.set_defaults(func=cmd_resolve)

    doctor = subparsers.add_parser(
        "doctor", help="check the AdGuard VPN CLI, login, listener, and egress"
    )
    _add_instance_arguments(doctor)
    doctor.add_argument("--json", action="store_true", help="print machine-readable JSON")
    doctor.add_argument(
        "--probe", action="store_true", help="probe the egress IP through the active proxy"
    )
    doctor.set_defaults(func=cmd_doctor)

    gateway = subparsers.add_parser(
        "gateway",
        help="run the port 9111 location router and mapping API",
        description=(
            "Listen on one port and provision locations on backend ports "
            "from a reserved range. SOCKS5 selects a location through the "
            "username; HTTP selects it with X-AdGuard-Location or Basic auth; "
            "GET /US/LAS-VEGAS returns the mapped endpoint."
        ),
    )
    gateway.add_argument(
        "--host", default="127.0.0.1", help="listen address (default: 127.0.0.1)"
    )
    gateway.add_argument(
        "--port", type=int, default=9111, help="listen port (default: 9111)"
    )
    gateway.add_argument(
        "--min-port",
        type=int,
        default=9112,
        help="first backend port (default: 9112)",
    )
    gateway.add_argument(
        "--max-port",
        type=int,
        default=9222,
        help="last backend port (default: 9222)",
    )
    gateway.add_argument(
        "--default-location",
        default="US/LAS-VEGAS",
        help="location used when a proxy request does not specify one",
    )
    gateway.add_argument(
        "--startup-timeout",
        type=float,
        default=120,
        metavar="SECONDS",
        help="maximum time to provision one location (default: 120)",
    )
    gateway.set_defaults(func=cmd_gateway)

    return parser


def _print_error(message: str, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"ok": False, "error": message}, indent=2))
    else:
        print(f"error: {message}", file=sys.stderr)


def _resolve_instance(
    args: argparse.Namespace,
    *,
    seed: bool = False,
) -> tuple[str | None, Path | None]:
    instance = getattr(args, "instance", None)
    explicit = getattr(args, "data_dir", None)
    if explicit and not instance:
        raise AdGuardError("--data-dir requires --instance")

    if not instance:
        return None, None

    name = validate_instance(instance)
    data_dir = resolve_data_dir(name, explicit)

    if seed:
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not getattr(args, "no_copy_login", False):
            source = seed_login(data_dir)
            if source:
                print(
                    f"Seeded AdGuard login for instance {name!r} from {source}",
                    file=sys.stderr,
                )
    return name, data_dir


def _auth_for(
    host: str,
    username: str | None,
    password: str | None,
    no_auth: bool,
) -> tuple[str | None, str | None]:
    if bool(username) != bool(password):
        raise AdGuardError("--username and --password must be provided together")
    if username and password:
        return username, password
    if no_auth:
        if not is_loopback(host):
            raise AdGuardError(
                "--no-auth is only allowed with a loopback --host; "
                "AdGuard VPN CLI requires authentication for public binds"
            )
        return None, None
    if is_loopback(host):
        return None, None
    return "adg", secrets.token_urlsafe(24)


def _advertise_host(
    listen_host: str,
    explicit: str | None,
    *,
    allow_public_ip: bool,
) -> str:
    if explicit:
        return explicit.strip()
    if not is_wildcard(listen_host):
        return "127.0.0.1" if listen_host.lower() == "localhost" else listen_host

    if allow_public_ip:
        detected = direct_public_ip() or local_ipv4()
    else:
        detected = local_ipv4()
    return detected or "127.0.0.1"


def _wait_until_ready(
    adguard: AdGuardCLI,
    *,
    host: str,
    port: int,
    location: Location,
    seconds: float,
) -> None:
    probe_host = connect_host(host)
    deadline = time.monotonic() + max(seconds, 1)
    last: str | None = None

    while time.monotonic() < deadline:
        status = adguard.status()
        last = status.raw or last
        if status.connected and status.port == port and probe_tcp(probe_host, port):
            if status.location and location.city.upper() not in status.location.upper():
                print(
                    f"warning: AdGuard reports {status.location!r}, "
                    f"requested {location.city!r}",
                    file=sys.stderr,
                )
            return
        time.sleep(1)

    detail = f" Last status: {last.strip()}" if last else ""
    raise AdGuardError(
        f"VPN did not become ready on {probe_host}:{port} within {seconds:g}s.{detail}"
    )


def cmd_up(args: argparse.Namespace) -> int:
    if not 1 <= args.port <= 65535:
        raise AdGuardError("--port must be between 1 and 65535")

    instance, data_dir = _resolve_instance(args, seed=True)
    adguard = AdGuardCLI(data_dir=data_dir)
    locations = adguard.list_locations()
    location = resolve_location(locations, args.location)
    username, password = _auth_for(
        args.host, args.username, args.password, args.no_auth
    )

    current = adguard.status()
    if current.connected:
        adguard.disconnect()

    adguard.configure_socks(args.host, args.port, username, password)
    adguard.connect(location)
    _wait_until_ready(
        adguard,
        host=args.host,
        port=args.port,
        location=location,
        seconds=args.wait,
    )

    probe_host = connect_host(args.host)
    egress_ip = None
    if not args.skip_egress_check:
        egress_ip = probe_egress(
            probe_host,
            args.port,
            username=username,
            password=password,
        )
        if egress_ip is None:
            print(
                "warning: connected, but the egress IP check failed; "
                "run `adguard-cli-proxy doctor --probe`",
                file=sys.stderr,
            )

    advertise = _advertise_host(
        args.host,
        args.advertise,
        allow_public_ip=not args.no_public_ip,
    )
    endpoint = format_endpoint(advertise, args.port, location)
    proxy_url = format_proxy_url(
        advertise, args.port, username=username, password=password
    )
    connected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    state = State(
        iso=location.iso,
        country=location.country,
        city=location.city,
        slug=location.slug,
        listen_host=args.host,
        listen_port=args.port,
        advertise_host=advertise,
        endpoint=endpoint,
        proxy_url=proxy_url,
        username=username,
        password=password,
        egress_ip=egress_ip,
        connected_at=connected_at,
        instance=instance,
        data_dir=str(data_dir) if data_dir else None,
    )
    saved_to = save_state(state)

    if not is_loopback(args.host):
        print(
            "warning: the SOCKS listener is reachable beyond loopback. "
            "Allow only trusted source IPs in your firewall.",
            file=sys.stderr,
        )
    if is_wildcard(args.host) and not args.advertise:
        print(
            f"note: advertising {advertise}; port {args.port} must be reachable "
            "from the client (NAT/firewall permitting).",
            file=sys.stderr,
        )

    if args.json:
        payload = {
            "ok": True,
            "instance": instance,
            "location": asdict(location),
            "slug": location.slug,
            "listen": f"{args.host}:{args.port}",
            "endpoint": endpoint,
            "proxy_url": proxy_url,
            "egress_ip": egress_ip,
            "healthy": egress_ip is not None,
            "authenticated": username is not None,
            "state_file": str(saved_to),
        }
        print(json.dumps(payload, indent=2))
        return 0

    if instance:
        print(f"Instance:  {instance}")
    print(f"Connected: {location.slug} ({location.label})")
    print(f"Listen:    {args.host}:{args.port}")
    print(f"Endpoint:  {endpoint}")
    print(f"Proxy:     {proxy_url}")
    print(f"Egress:    {egress_ip or 'unverified'}")
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    instance, data_dir = _resolve_instance(args)
    adguard = AdGuardCLI(data_dir=data_dir)
    output = adguard.disconnect()
    clear_state(instance)

    if args.json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "disconnected": True,
                    "instance": instance,
                    "detail": output,
                },
                indent=2,
            )
        )
    else:
        label = f" instance {instance!r}" if instance else ""
        print(f"Disconnected{label}. Endpoint removed.")
    return 0


def _status_payload(
    instance: str | None,
    state: State | None,
    *,
    probe: bool,
) -> dict[str, object]:
    if state and state.data_dir:
        data_dir: Path | None = Path(state.data_dir)
    elif instance:
        data_dir = instance_data_dir(instance)
    else:
        data_dir = None

    status = AdGuardCLI(data_dir=data_dir).status()
    egress_ip = state.egress_ip if state else None
    if probe and status.connected and state:
        egress_ip = probe_egress(
            connect_host(state.listen_host),
            state.listen_port,
            username=state.username,
            password=state.password,
        )

    stale = bool(state and not status.connected)
    healthy = bool(status.connected and state and egress_ip)
    return {
        "ok": True,
        "instance": instance,
        "connected": status.connected,
        "stale_state": stale,
        "location": status.location,
        "mode": status.mode,
        "listen": f"{status.host}:{status.port}" if status.connected else None,
        "endpoint": state.endpoint if state else None,
        "proxy_url": state.proxy_url if state else None,
        "egress_ip": egress_ip,
        "healthy": healthy,
        "state_file": str(state_path(instance)) if state else None,
    }


def cmd_status(args: argparse.Namespace) -> int:
    if args.all and args.instance:
        raise AdGuardError("--all cannot be combined with --instance")

    items: list[tuple[str | None, State | None]] = []
    if args.all:
        default_state = load_state()
        if default_state:
            items.append((None, default_state))
        for name in list_instances():
            items.append((name, load_state(name)))
        if not items:
            items.append((None, None))
    elif args.instance:
        instance, _ = _resolve_instance(args)
        items.append((instance, load_state(instance)))
    else:
        items.append((None, load_state()))

    payloads = [
        _status_payload(instance, state, probe=args.probe)
        for instance, state in items
    ]

    if args.json:
        print(json.dumps(payloads if args.all else payloads[0], indent=2))
    else:
        multiple = len(payloads) > 1
        for payload in payloads:
            name = payload["instance"]
            if multiple:
                print(f"=== {name or 'default'} ===")
            print(f"status:   {'connected' if payload['connected'] else 'disconnected'}")
            if payload["connected"]:
                print(f"location: {payload['location']}")
                print(f"listen:   {payload['listen']}")
            elif payload["stale_state"]:
                print("note:     active state is stale; run `adguard-cli-proxy down`")
            if payload["endpoint"]:
                if payload["connected"]:
                    print(f"endpoint: {payload['endpoint']}")
                    print(f"proxy:    {payload['proxy_url']}")
                    print(f"egress:   {payload['egress_ip'] or 'unverified'}")
                else:
                    print(f"last:     {payload['endpoint']}")
            if multiple:
                print()

    if args.check and any(not item["connected"] for item in payloads):
        return 3
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    adguard = AdGuardCLI()
    locations = adguard.list_locations()

    if args.country:
        query = args.country.strip()
        query_upper = query.upper()
        query_folded = query.casefold()
        locations = [
            item
            for item in locations
            if item.iso == query_upper
            or item.country.casefold() == query_folded
            or item.country.casefold().startswith(query_folded)
        ]
    if args.search:
        needle = args.search.casefold()
        locations = [
            item
            for item in locations
            if needle in item.city.casefold()
            or needle in item.country.casefold()
            or needle in item.slug.casefold()
        ]

    if not locations:
        raise LocationError("No locations matched the filter")

    if args.json:
        payload = [
            {
                "iso": item.iso,
                "country": item.country,
                "city": item.city,
                "ping": item.ping,
                "slug": item.slug,
                "endpoint_suffix": f"/{item.slug}",
            }
            for item in locations
        ]
        print(json.dumps(payload, indent=2))
        return 0

    print(f"{'ISO':<4} {'CITY':<24} {'COUNTRY':<22} LABEL")
    for item in locations:
        print(f"{item.iso:<4} {item.city:<24.24} {item.country:<22.22} {item.slug}")
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    adguard = AdGuardCLI()
    location = resolve_location(adguard.list_locations(), args.location)
    if args.json:
        print(
            json.dumps(
                {
                    "iso": location.iso,
                    "country": location.country,
                    "city": location.city,
                    "ping": location.ping,
                    "slug": location.slug,
                },
                indent=2,
            )
        )
    else:
        print(location.slug)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    instance, data_dir = _resolve_instance(args)
    adguard = AdGuardCLI(data_dir=data_dir)
    state = load_state(instance)
    checks: list[dict[str, object]] = []
    failed = False

    available = adguard.available()
    checks.append(
        {
            "name": "adguardvpn-cli",
            "status": "ok" if available else "fail",
            "detail": shutil.which(adguard.binary) or "not found",
        }
    )
    failed = failed or not available

    if available:
        try:
            version = adguard.version().splitlines()[0]
            checks.append({"name": "version", "status": "ok", "detail": version})
        except AdGuardError as exc:
            checks.append({"name": "version", "status": "fail", "detail": str(exc)})
            failed = True

        license_text = adguard.license().lower()
        logged_in = "logged in" in license_text
        premium = "premium" in license_text
        checks.append(
            {
                "name": "login",
                "status": "ok" if logged_in else "fail",
                "detail": "logged in" if logged_in else "run `adguardvpn-cli login`",
            }
        )
        checks.append(
            {
                "name": "license",
                "status": "ok" if premium else "warn",
                "detail": "premium" if premium else "free or unknown",
            }
        )
        failed = failed or not logged_in

        status = adguard.status()
        checks.append(
            {
                "name": "vpn",
                "status": "ok" if status.connected else "warn",
                "detail": (
                    f"{status.location} via {status.mode} on {status.host}:{status.port}"
                    if status.connected
                    else "disconnected"
                ),
            }
        )

        if state:
            listening = probe_tcp(connect_host(state.listen_host), state.listen_port)
            checks.append(
                {
                    "name": "listener",
                    "status": "ok" if listening else "fail",
                    "detail": (
                        f"{state.listen_host}:{state.listen_port} "
                        f"({state.endpoint})"
                    ),
                }
            )
            failed = failed or not listening

            if args.probe and listening:
                egress = probe_egress(
                    connect_host(state.listen_host),
                    state.listen_port,
                    username=state.username,
                    password=state.password,
                )
                checks.append(
                    {
                        "name": "egress",
                        "status": "ok" if egress else "fail",
                        "detail": egress or "could not determine public IP",
                    }
                )
                failed = failed or egress is None
        else:
            checks.append(
                {
                    "name": "endpoint",
                    "status": "warn",
                    "detail": "none active; run `adguard-cli-proxy up US/LAS-VEGAS`",
                }
            )

    if args.json:
        print(
            json.dumps(
                {"ok": not failed, "instance": instance, "checks": checks}, indent=2
            )
        )
    else:
        if instance:
            print(f"=== {instance} ===")
        for check in checks:
            marker = {"ok": "OK", "warn": "WARN", "fail": "FAIL"}[str(check["status"])]
            print(f"[{marker:<4}] {check['name']!s:<14} {check['detail']}")
    return 1 if failed else 0


def cmd_gateway(args: argparse.Namespace) -> int:
    if not is_loopback(args.host):
        raise GatewayError(
            "the gateway API is unauthenticated; bind it to a loopback address "
            "and reach it through SSH, WireGuard, or a reverse proxy"
        )
    if not (1 <= args.port <= 65535):
        raise GatewayError("--port must be between 1 and 65535")
    if not (1 <= args.min_port <= 65535 and 1 <= args.max_port <= 65535):
        raise GatewayError("--min-port and --max-port must be between 1 and 65535")
    if args.min_port > args.max_port:
        raise GatewayError("--min-port must be less than or equal to --max-port")
    if args.min_port <= args.port <= args.max_port:
        raise GatewayError("--port must be outside the backend port range")
    if args.startup_timeout <= 0:
        raise GatewayError("--startup-timeout must be greater than zero")

    config = GatewayConfig(
        host=args.host,
        port=args.port,
        min_port=args.min_port,
        max_port=args.max_port,
        default_location=args.default_location,
        startup_timeout=args.startup_timeout,
    )
    try:
        serve_gateway(config)
    except OSError as exc:
        raise GatewayError(
            f"could not listen on {config.host}:{config.port}: {exc}"
        ) from exc
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (AdGuardError, LocationError, InstanceError, GatewayError) as exc:
        _print_error(str(exc), as_json=getattr(args, "json", False))
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
