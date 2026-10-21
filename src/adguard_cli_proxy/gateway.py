"""Port 9111 location router for adguard-cli-proxy.

The gateway accepts three kinds of traffic on one port:

* SOCKS5, with the location encoded in the SOCKS username;
* HTTP CONNECT / forward-proxy requests, with the location in
  ``X-AdGuard-Location`` or the Basic proxy username;
* an HTTP mapping API, ``GET /US/LAS-VEGAS``, which provisions the location on
  a free backend port in the configured range and returns the endpoint.

The mapping API also exposes pathless HTTP proxy URLs for clients that reject
paths in proxy URLs (for example 9router's undici ``ProxyAgent``)::

    http://US-LAS-VEGAS:x@127.0.0.1:9111

The location is carried in the proxy username, so no path or header is needed.
The password is ignored; it only has to be non-empty because undici 7 only
sends proxy credentials when both username and password are present.
"""

from __future__ import annotations

import base64
import json
import re
import select
import socket
import socketserver
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .adguard import AdGuardCLI, AdGuardError, Location
from .endpoint import LocationError, resolve_location
from .instances import validate_instance
from .net import SocksError, probe_tcp, socks5_connect
from .state import State, list_instances, load_state


class GatewayError(RuntimeError):
    """Raised when the gateway cannot provision or route a location."""


@dataclass(frozen=True)
class GatewayConfig:
    host: str = "127.0.0.1"
    port: int = 9111
    min_port: int = 9112
    max_port: int = 9222
    default_location: str = "US/LAS-VEGAS"
    startup_timeout: float = 120.0


def normalise_location_query(value: str) -> str:
    value = unquote(value).strip().strip("/")
    value = re.sub(r"\s+", " ", value)
    match = re.match(r"^([A-Za-z]{2})[-_:](.+)$", value)
    if match:
        return f"{match.group(1).upper()}/{match.group(2)}"
    return value


def instance_name(location: Location) -> str:
    return validate_instance(location.slug.lower().replace("/", "-"))


class Gateway:
    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        # The actual bound port can differ from config.port when tests ask for
        # port 0. GatewayServer replaces this after binding.
        self.port = config.port
        self._lock = threading.RLock()
        self._locations: list[Location] | None = None

    def locations(self) -> list[Location]:
        if self._locations is None:
            self._locations = AdGuardCLI().list_locations()
        return self._locations

    def resolve(self, query: str) -> Location:
        return resolve_location(self.locations(), normalise_location_query(query))

    def active_states(self) -> list[State]:
        states: list[State] = []
        for name in list_instances():
            state = load_state(name)
            if state:
                states.append(state)
        return sorted(states, key=lambda item: (item.listen_port, item.instance or ""))

    def _used_ports(self, current: str | None = None) -> set[int]:
        used: set[int] = set()
        for name in list_instances():
            state = load_state(name)
            if state and state.instance != current:
                used.add(state.listen_port)
        return used

    def _port_available(self, port: int, current: str | None = None) -> bool:
        if not self.config.min_port <= port <= self.config.max_port:
            return False
        if port in self._used_ports(current):
            return False
        if probe_tcp("127.0.0.1", port, timeout=0.2):
            return False
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
        finally:
            sock.close()

    def allocate_port(self, current: str | None = None) -> int:
        for port in range(self.config.min_port, self.config.max_port + 1):
            if self._port_available(port, current):
                return port
        raise GatewayError(
            f"No free backend port in {self.config.min_port}-{self.config.max_port}"
        )

    def ensure(self, query: str) -> State:
        """Return the mapped state, starting a tunnel only when no state exists.

        Raw TCP probes against the AdGuard SOCKS listener make its next client
        connection fail, so the normal path deliberately does not probe. The
        caller retries and calls :meth:`restart` if the backend is really dead.
        """
        with self._lock:
            location = self.resolve(query)
            name = instance_name(location)
            state = load_state(name)
            if (
                state
                and self.config.min_port <= state.listen_port <= self.config.max_port
                and state.listen_port not in self._used_ports(name)
            ):
                return state

            if state and self._port_available(state.listen_port, name):
                port = state.listen_port
            else:
                port = self.allocate_port(name)

            self._start(location, name, port)
            state = load_state(name)
            if not state:
                raise GatewayError(f"Backend {name!r} did not create a state file")
            return state

    def restart(self, state: State) -> State:
        """Restart a backend that no longer accepts SOCKS connections."""
        with self._lock:
            name = state.instance or instance_name(self.resolve(state.slug))
            location = self.resolve(state.slug)
            if self._port_available(state.listen_port, name):
                port = state.listen_port
            else:
                port = self.allocate_port(name)
            self._start(location, name, port)
            replacement = load_state(name)
            if not replacement:
                raise GatewayError(f"Backend {name!r} did not create a state file")
            return replacement

    @staticmethod
    def _backend_failure(exc: BaseException) -> bool:
        if isinstance(exc, OSError):
            return True
        message = str(exc).lower()
        return "closed the connection unexpectedly" in message or "authentication" in message

    def connect(
        self,
        state: State,
        host: str,
        port: int,
        *,
        timeout: float = 20,
    ) -> socket.socket:
        """Open a stream through a backend, retrying a poisoned first connection."""
        last: BaseException | None = None
        for _attempt in range(2):
            try:
                return socks5_connect(
                    "127.0.0.1", state.listen_port, host, port, timeout=timeout
                )
            except (OSError, SocksError) as exc:
                last = exc
                if not self._backend_failure(exc):
                    raise
                time.sleep(0.1)

        replacement = self.restart(state)
        try:
            return socks5_connect(
                "127.0.0.1",
                replacement.listen_port,
                host,
                port,
                timeout=timeout,
            )
        except (OSError, SocksError) as exc:
            raise GatewayError(
                f"backend {replacement.instance or replacement.slug} is unavailable: {exc}"
            ) from last

    def _start(self, location: Location, name: str, port: int) -> None:
        command = [
            sys.executable,
            "-m",
            "adguard_cli_proxy",
            "up",
            location.slug,
            "--instance",
            name,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--wait",
            "30",
        ]
        print(
            f"gateway: provisioning {location.slug} as {name} on 127.0.0.1:{port}",
            flush=True,
        )
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.config.startup_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GatewayError(
                f"Timed out provisioning {location.slug} after "
                f"{self.config.startup_timeout:g}s"
            ) from exc

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise GatewayError(
                f"Could not provision {location.slug}: {detail or 'unknown error'}"
            )

    def stop(self, query: str) -> None:
        with self._lock:
            location = self.resolve(query)
            name = instance_name(location)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "adguard_cli_proxy",
                    "down",
                    "--instance",
                    name,
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if completed.returncode != 0:
                raise GatewayError(
                    (completed.stderr or completed.stdout).strip()
                    or f"Could not stop {name}"
                )

    def pool_url(self, state: State) -> str:
        """Return a pathless HTTP proxy URL with the location in the username.

        Clients such as 9router build an ``undici.ProxyAgent`` from the pool
        URL and reject any URL with a path, so ``/US/LAS-VEGAS`` cannot be used
        as a proxy URL. ``http://US-LAS-VEGAS:x@127.0.0.1:9111`` is accepted by
        those clients and this gateway resolves the username to the location.

        undici 7 only emits ``Proxy-Authorization`` when both the username and
        password are non-empty, so the URL includes a dummy password ``x``.
        The gateway ignores the password.
        """
        host = self.config.host
        if host in {"", "0.0.0.0", "::"}:
            host = "127.0.0.1"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        username = quote(state.slug.replace("/", "-"), safe="-._~")
        return f"http://{username}:x@{host}:{self.port}"

    def nine_router_pool(self, state: State) -> dict[str, str]:
        return {
            "name": f"AG/{state.slug}",
            "proxyUrl": self.pool_url(state),
            "noProxy": "",
            "type": "http",
        }

    def nine_router_payload(self) -> dict[str, object]:
        return {
            "gateway": f"{self.config.host}:{self.port}",
            "pools": [
                self.nine_router_pool(state) for state in self.active_states()
            ],
        }

    def payload(self, state: State) -> dict[str, object]:
        return {
            "location": state.slug,
            "instance": state.instance,
            "mapped_port": state.listen_port,
            "endpoint": state.endpoint,
            "proxy_url": state.proxy_url,
            "pool_url": self.pool_url(state),
            "egress_ip": state.egress_ip,
            "healthy": state.healthy,
        }

    def index_payload(self) -> dict[str, object]:
        states = self.active_states()
        return {
            "gateway": f"{self.config.host}:{self.config.port}",
            "port_range": f"{self.config.min_port}-{self.config.max_port}",
            "default_location": self.config.default_location,
            "active": [self.payload(state) for state in states],
        }


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


def _relay(left: socket.socket, right: socket.socket) -> None:
    try:
        while True:
            readable, _, _ = select.select([left, right], [], [], 300)
            if not readable:
                return
            for source in readable:
                data = source.recv(65536)
                if not data:
                    return
                destination = right if source is left else left
                destination.sendall(data)
    finally:
        for item in (left, right):
            try:
                item.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                item.close()
            except OSError:
                pass


def _socks_reply(sock: socket.socket, code: int) -> None:
    sock.sendall(bytes((0x05, code, 0x00, 0x01)) + b"\x00" * 6)


def _read_socks_address(sock: socket.socket, address_type: int) -> tuple[str, int]:
    if address_type == 0x01:
        host = socket.inet_ntoa(_recv_exact(sock, 4))
    elif address_type == 0x04:
        host = socket.inet_ntop(socket.AF_INET6, _recv_exact(sock, 16))
    elif address_type == 0x03:
        length = _recv_exact(sock, 1)[0]
        host = _recv_exact(sock, length).decode("idna")
    else:
        raise ValueError(f"Unsupported SOCKS address type: {address_type}")
    port = int.from_bytes(_recv_exact(sock, 2), "big")
    return host, port


def _parse_headers(raw: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in raw.decode("latin-1").split("\r\n")[1:]:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    return headers


def _basic_username(value: str | None) -> str | None:
    if not value:
        return None
    scheme, _, encoded = value.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded).decode("utf-8", errors="replace")
    except ValueError:
        return None
    return decoded.split(":", 1)[0]


def _request_location(headers: dict[str, str], default: str) -> str:
    return normalise_location_query(
        headers.get("x-adguard-location")
        or headers.get("x-proxy-location")
        or _basic_username(headers.get("proxy-authorization"))
        or default
    )


def _split_hostport(target: str, default_port: int) -> tuple[str, int]:
    if target.startswith("["):
        end = target.find("]")
        if end == -1:
            raise ValueError("Invalid IPv6 target")
        host = target[1:end]
        port = int(target[end + 2 :]) if target[end + 1 :].startswith(":") else default_port
        return host, port
    if target.count(":") == 1:
        host, raw_port = target.rsplit(":", 1)
        return host, int(raw_port)
    return target, default_port


def _read_chunked(sock: socket.socket, initial: bytes) -> bytes:
    """Read a chunked body and return its original wire representation."""
    raw = bytearray(initial)
    buffer = bytearray(initial)
    while True:
        while b"\r\n" not in buffer:
            chunk = sock.recv(4096)
            if not chunk:
                raise EOFError
            raw.extend(chunk)
            buffer.extend(chunk)
        line, _, rest = bytes(buffer).partition(b"\r\n")
        size = int(line.split(b";", 1)[0], 16)
        buffer = bytearray(rest)
        if size == 0:
            # The final chunk is followed either by a bare CRLF or by trailers.
            while not buffer.startswith(b"\r\n") and b"\r\n\r\n" not in buffer:
                chunk = sock.recv(4096)
                if not chunk:
                    raise EOFError
                raw.extend(chunk)
                buffer.extend(chunk)
            return bytes(raw)
        needed = size + 2
        while len(buffer) < needed:
            chunk = sock.recv(65536)
            if not chunk:
                raise EOFError
            raw.extend(chunk)
            buffer.extend(chunk)
        buffer = buffer[needed:]


class GatewayHandler(socketserver.BaseRequestHandler):
    server: GatewayServer

    def handle(self) -> None:
        self.request.settimeout(60)
        try:
            first = self.request.recv(1, socket.MSG_PEEK)
        except OSError:
            return
        if not first:
            return
        if first == b"\x05":
            self._handle_socks()
        else:
            self._handle_http()

    # -- SOCKS5 ---------------------------------------------------------
    def _handle_socks(self) -> None:
        sock = self.request
        try:
            version, count = _recv_exact(sock, 2)
            if version != 0x05:
                return
            methods = set(_recv_exact(sock, count))
            username = ""
            if 0x02 in methods:
                sock.sendall(b"\x05\x02")
                _, name_length = _recv_exact(sock, 2)
                username = _recv_exact(sock, name_length).decode(
                    "utf-8", errors="replace"
                )
                password_length = _recv_exact(sock, 1)[0]
                _recv_exact(sock, password_length)
                sock.sendall(b"\x01\x00")
            elif 0x00 in methods:
                sock.sendall(b"\x05\x00")
            else:
                sock.sendall(b"\x05\xff")
                return

            version, command, _, address_type = _recv_exact(sock, 4)
            if version != 0x05:
                return
            destination_host, destination_port = _read_socks_address(sock, address_type)
            if command != 0x01:
                _socks_reply(sock, 0x07)
                return

            query = username.strip() or self.server.gateway.config.default_location
            try:
                state = self.server.gateway.ensure(query)
            except (GatewayError, LocationError, AdGuardError):
                _socks_reply(sock, 0x04)
                return

            try:
                upstream = self.server.gateway.connect(
                    state, destination_host, destination_port
                )
            except (GatewayError, OSError, SocksError) as exc:
                print(f"gateway: SOCKS backend failed: {exc}", file=sys.stderr, flush=True)
                _socks_reply(sock, 0x05)
                return

            _socks_reply(sock, 0x00)
            _relay(sock, upstream)
        except (EOFError, OSError, ValueError):
            return

    # -- HTTP -----------------------------------------------------------
    def _read_request(
        self,
    ) -> tuple[str, str, str, dict[str, str], bytes] | None:
        sock = self.request
        data = bytearray()
        while b"\r\n\r\n" not in data and len(data) < 65536:
            chunk = sock.recv(4096)
            if not chunk:
                return None
            data.extend(chunk)
        header, separator, initial_body = bytes(data).partition(b"\r\n\r\n")
        if not separator:
            return None
        lines = header.split(b"\r\n")
        if not lines:
            return None
        try:
            method, target, version = lines[0].decode("latin-1").split(" ", 2)
        except ValueError:
            return None
        return method.upper(), target, version, _parse_headers(header), initial_body

    def _read_body(
        self, headers: dict[str, str], initial: bytes, limit: int = 16 * 1024 * 1024
    ) -> bytes:
        if "chunked" in headers.get("transfer-encoding", "").lower():
            return _read_chunked(self.request, initial)
        length_header = headers.get("content-length")
        if not length_header:
            return initial
        length = int(length_header)
        if length > limit:
            raise ValueError("Request body too large")
        data = bytearray(initial)
        while len(data) < length:
            chunk = self.request.recv(min(65536, length - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data[:length])

    def _respond(
        self,
        status: int,
        body: bytes,
        content_type: str = "application/json",
        *,
        head_only: bool = False,
    ) -> None:
        reason = HTTPStatus(status).phrase
        payload = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        self.request.sendall(payload)
        if not head_only:
            self.request.sendall(body)

    def _handle_http(self) -> None:
        try:
            request = self._read_request()
            if request is None:
                return
            method, target, _version, headers, initial_body = request

            if method == "CONNECT":
                self._proxy_connect(target, headers)
            elif target.startswith(("http://", "https://")):
                self._proxy_forward(method, target, headers, initial_body)
            else:
                self._handle_api(method, target, headers)
        except (GatewayError, LocationError, AdGuardError) as exc:
            message = json.dumps({"error": str(exc)}).encode()
            try:
                self._respond(502, message)
            except OSError:
                pass
        except (ConnectionError, OSError, SocksError, ValueError):
            try:
                self._respond(400, b'{"error":"bad request"}')
            except OSError:
                pass

    def _proxy_connect(self, target: str, headers: dict[str, str]) -> None:
        gateway = self.server.gateway
        state = gateway.ensure(_request_location(headers, gateway.config.default_location))
        host, port = _split_hostport(target, 443)
        try:
            upstream = gateway.connect(state, host, port)
        except (GatewayError, OSError, SocksError) as exc:
            message = json.dumps({"error": f"backend unavailable: {exc}"}).encode()
            self._respond(502, message)
            return
        self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        _relay(self.request, upstream)

    def _proxy_forward(
        self,
        method: str,
        target: str,
        headers: dict[str, str],
        initial_body: bytes,
    ) -> None:
        parsed = urlsplit(target)
        if parsed.scheme != "http" or not parsed.hostname:
            self._respond(
                400,
                b'{"error":"Use CONNECT for HTTPS; only absolute http:// is supported"}',
            )
            return

        gateway = self.server.gateway
        state = gateway.ensure(_request_location(headers, gateway.config.default_location))
        port = parsed.port or 80
        try:
            upstream = gateway.connect(state, parsed.hostname, port)
        except (GatewayError, OSError, SocksError) as exc:
            message = json.dumps({"error": f"backend unavailable: {exc}"}).encode()
            self._respond(502, message)
            return
        try:
            body = self._read_body(headers, initial_body)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            forwarded = [
                f"{method} {path} HTTP/1.1",
                f"Host: {parsed.netloc.rsplit('@', 1)[-1]}",
            ]
            for name, value in headers.items():
                if name in {
                    "connection",
                    "proxy-connection",
                    "proxy-authorization",
                    "x-adguard-location",
                    "x-proxy-location",
                    "keep-alive",
                    "te",
                    "trailer",
                    "upgrade",
                }:
                    continue
                forwarded.append(f"{name}: {value}")
            forwarded.extend(["Connection: close", "", ""])
            upstream.sendall("\r\n".join(forwarded).encode("latin-1") + body)

            while True:
                chunk = upstream.recv(65536)
                if not chunk:
                    break
                self.request.sendall(chunk)
        finally:
            upstream.close()

    def _handle_api(
        self, method: str, target: str, headers: dict[str, str]
    ) -> None:
        parsed = urlsplit(target)
        path = unquote(parsed.path).strip("/")
        query = parse_qs(parsed.query)
        head_only = method == "HEAD"
        gateway = self.server.gateway

        if method not in {"GET", "HEAD", "DELETE"}:
            self._respond(405, b'{"error":"method not allowed"}', head_only=head_only)
            return

        if path in {"", "health"}:
            payload = gateway.index_payload() if path == "" else {"ok": True}
            self._respond(
                200,
                json.dumps(payload, indent=2).encode() + b"\n",
                head_only=head_only,
            )
            return

        if path in {"_9router", "_pools"}:
            payload = gateway.nine_router_payload()
            self._respond(
                200,
                json.dumps(payload, indent=2).encode() + b"\n",
                head_only=head_only,
            )
            return

        if path == "_instances":
            payload = [gateway.payload(state) for state in gateway.active_states()]
            self._respond(
                200,
                json.dumps(payload, indent=2).encode() + b"\n",
                head_only=head_only,
            )
            return

        if method == "DELETE":
            gateway.stop(path)
            self._respond(200, b'{"ok":true}\n', head_only=head_only)
            return

        state = gateway.ensure(path)
        output_format = query.get("format", [""])[0]
        if output_format == "text":
            body = (state.endpoint + "\n").encode()
            self._respond(200, body, "text/plain; charset=utf-8", head_only=head_only)
        elif output_format in {"9router", "pool"}:
            body = (gateway.pool_url(state) + "\n").encode()
            self._respond(200, body, "text/plain; charset=utf-8", head_only=head_only)
        else:
            body = json.dumps(gateway.payload(state), indent=2).encode() + b"\n"
            self._respond(200, body, head_only=head_only)


class GatewayServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, config: GatewayConfig) -> None:
        self.gateway = Gateway(config)
        super().__init__((config.host, config.port), GatewayHandler)
        self.gateway.port = self.server_address[1]

    def handle_error(self, request: object, client_address: object) -> None:
        print(f"gateway: handler error for {client_address}", file=sys.stderr, flush=True)
        super().handle_error(request, client_address)


def serve(config: GatewayConfig) -> None:
    server = GatewayServer(config)
    print(
        f"gateway: listening on {config.host}:{config.port}, "
        f"mapping {config.min_port}-{config.max_port}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
