"""Tiny SOCKS5 client used for health checks (no third-party dependencies)."""

from __future__ import annotations

import ipaddress
import socket
import ssl
import struct
from urllib.parse import urlsplit


class SocksError(RuntimeError):
    """Raised when a SOCKS5 request fails."""


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise SocksError("SOCKS proxy closed the connection unexpectedly")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def socks5_connect(
    proxy_host: str,
    proxy_port: int,
    destination_host: str,
    destination_port: int,
    *,
    username: str | None = None,
    password: str | None = None,
    timeout: float = 10,
) -> socket.socket:
    """Open a TCP stream to ``destination`` through a SOCKS5 proxy."""
    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    try:
        sock.settimeout(timeout)
        method = 0x02 if username is not None else 0x00
        sock.sendall(bytes((0x05, 0x01, method)))
        version, selected = _recv_exact(sock, 2)
        if version != 0x05:
            raise SocksError("Proxy did not answer with SOCKS5")
        if selected == 0xFF:
            raise SocksError("Proxy rejected all authentication methods")
        if selected != method:
            raise SocksError(f"Proxy selected unsupported auth method 0x{selected:02x}")

        if selected == 0x02:
            user_bytes = (username or "").encode("utf-8")
            pass_bytes = (password or "").encode("utf-8")
            if len(user_bytes) > 255 or len(pass_bytes) > 255:
                raise SocksError("SOCKS username/password must be at most 255 bytes")
            sock.sendall(
                bytes((0x01, len(user_bytes)))
                + user_bytes
                + bytes((len(pass_bytes),))
                + pass_bytes
            )
            auth_version, auth_status = _recv_exact(sock, 2)
            if auth_version != 0x01 or auth_status != 0x00:
                raise SocksError("SOCKS username/password authentication failed")

        try:
            ip = ipaddress.ip_address(destination_host)
        except ValueError:
            encoded_host = destination_host.encode("idna")
            if len(encoded_host) > 255:
                raise SocksError("Destination hostname is too long")
            address = bytes((0x03, len(encoded_host))) + encoded_host
        else:
            address = (bytes((0x01,)) if ip.version == 4 else bytes((0x04,))) + ip.packed

        request = bytes((0x05, 0x01, 0x00)) + address + struct.pack("!H", destination_port)
        sock.sendall(request)

        reply_version, reply_code, _, address_type = _recv_exact(sock, 4)
        if reply_version != 0x05:
            raise SocksError("Invalid SOCKS5 reply")
        if reply_code != 0x00:
            messages = {
                0x01: "general SOCKS server failure",
                0x02: "connection not allowed by ruleset",
                0x03: "network unreachable",
                0x04: "host unreachable",
                0x05: "connection refused",
                0x06: "TTL expired",
                0x07: "command not supported",
                0x08: "address type not supported",
            }
            raise SocksError(
                f"SOCKS proxy refused the connection: "
                f"{messages.get(reply_code, f'error 0x{reply_code:02x}')}"
            )

        if address_type == 0x01:
            _recv_exact(sock, 4)
        elif address_type == 0x04:
            _recv_exact(sock, 16)
        elif address_type == 0x03:
            length = _recv_exact(sock, 1)[0]
            _recv_exact(sock, length)
        else:
            raise SocksError("SOCKS proxy returned an unknown address type")
        _recv_exact(sock, 2)

        return sock
    except Exception:
        sock.close()
        raise


def http_get_via_socks(
    proxy_host: str,
    proxy_port: int,
    url: str,
    *,
    username: str | None = None,
    password: str | None = None,
    timeout: float = 10,
    max_bytes: int = 64 * 1024,
) -> str:
    """Fetch an HTTP(S) URL through SOCKS5 and return the response body."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only absolute http:// and https:// URLs are supported")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    sock = socks5_connect(
        proxy_host,
        proxy_port,
        parsed.hostname,
        port,
        username=username,
        password=password,
        timeout=timeout,
    )
    try:
        if parsed.scheme == "https":
            context = ssl.create_default_context()
            sock = context.wrap_socket(sock, server_hostname=parsed.hostname)
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.netloc}\r\n"
            "User-Agent: adguard-cli-proxy/health-check\r\n"
            "Accept: text/plain\r\n"
            "Connection: close\r\n\r\n"
        )
        sock.sendall(request.encode("ascii"))
        data = bytearray()
        while len(data) < max_bytes:
            chunk = sock.recv(min(8192, max_bytes - len(data)))
            if not chunk:
                break
            data.extend(chunk)
    finally:
        sock.close()

    header, separator, body = bytes(data).partition(b"\r\n\r\n")
    if not separator:
        raise SocksError("Invalid HTTP response from egress check")
    status_line = header.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
    parts = status_line.split()
    if len(parts) < 2 or not parts[1].startswith("2"):
        raise SocksError(f"Egress check returned HTTP status {status_line!r}")
    return body.decode("utf-8", errors="replace").strip()


def probe_egress(
    proxy_host: str,
    proxy_port: int,
    *,
    username: str | None = None,
    password: str | None = None,
    timeout: float = 8,
) -> str | None:
    """Return the public IP observed through the proxy, if it can be checked."""
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            value = http_get_via_socks(
                proxy_host,
                proxy_port,
                url,
                username=username,
                password=password,
                timeout=timeout,
            )
            ipaddress.ip_address(value)
            return value
        except (OSError, SocksError, ValueError):
            continue
    return None


def direct_public_ip(timeout: float = 5) -> str | None:
    """Return the host's public ingress IP without using a configured proxy."""
    import urllib.error
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            with opener.open(url, timeout=timeout) as response:
                value = response.read(64).decode("ascii", errors="replace").strip()
            ipaddress.ip_address(value)
            return value
        except (OSError, ValueError, urllib.error.URLError):
            continue
    return None


def local_ipv4() -> str | None:
    """Discover the address of the interface used for the default route."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:
        return None
    finally:
        sock.close()


def probe_tcp(host: str, port: int, timeout: float = 1) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
