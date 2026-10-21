from __future__ import annotations

import base64
import json
import os
import socket
import socketserver
import tempfile
import threading
import unittest
from unittest import mock

from adguard_cli_proxy.adguard import Location
from adguard_cli_proxy.gateway import (
    Gateway,
    GatewayConfig,
    GatewayServer,
    _read_chunked,
    _read_socks_address,
    _recv_exact,
    instance_name,
    normalise_location_query,
)
from adguard_cli_proxy.net import SocksError, socks5_connect
from adguard_cli_proxy.state import State, save_state


def sample_state(port: int, instance: str = "us-las-vegas") -> State:
    return State(
        iso="US",
        country="United States",
        city="Las Vegas",
        slug="US/LAS-VEGAS",
        listen_host="127.0.0.1",
        listen_port=port,
        advertise_host="127.0.0.1",
        endpoint=f"127.0.0.1:{port}/US/LAS-VEGAS",
        proxy_url=f"socks5h://127.0.0.1:{port}",
        username=None,
        password=None,
        egress_ip="185.242.5.198",
        connected_at="2026-10-21T00:00:00+00:00",
        instance=instance,
    )


class FakeBackend(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        try:
            version, count = _recv_exact(self.request, 2)
            assert version == 5
            methods = set(_recv_exact(self.request, count))
            self.request.sendall(b"\x05\x00" if 0x00 in methods else b"\x05\xff")
            version, command, _, address_type = _recv_exact(self.request, 4)
            assert version == 5 and command == 1
            _read_socks_address(self.request, address_type)
            self.request.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
            while True:
                data = self.request.recv(65536)
                if not data:
                    return
                self.request.sendall(b"echo:" + data)
        except (EOFError, OSError):
            return


class GatewayServerMixin:
    def start_gateway(self, state: State) -> GatewayServer:
        self.backend = socketserver.ThreadingTCPServer(
            ("127.0.0.1", 0), FakeBackend
        )
        self.backend.daemon_threads = True
        self.addCleanup(self.backend.server_close)
        self.addCleanup(self.backend.shutdown)
        backend_thread = threading.Thread(
            target=self.backend.serve_forever, daemon=True
        )
        backend_thread.start()
        state.listen_port = self.backend.server_address[1]

        self.gateway_server = GatewayServer(
            GatewayConfig(host="127.0.0.1", port=0, min_port=60000, max_port=60001)
        )
        self.addCleanup(self.gateway_server.server_close)
        self.addCleanup(self.gateway_server.shutdown)
        gateway_thread = threading.Thread(
            target=self.gateway_server.serve_forever, daemon=True
        )
        gateway_thread.start()
        return self.gateway_server

    def request(self, raw: bytes) -> bytes:
        address = self.gateway_server.server_address
        with socket.create_connection(address, timeout=5) as client:
            client.sendall(raw)
            chunks = []
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)


class NameTests(unittest.TestCase):
    def test_normalise_location_query(self) -> None:
        self.assertEqual(normalise_location_query("US/LAS-VEGAS"), "US/LAS-VEGAS")
        self.assertEqual(normalise_location_query("/US/LAS-VEGAS/"), "US/LAS-VEGAS")
        self.assertEqual(normalise_location_query("US-LAS-VEGAS"), "US/LAS-VEGAS")
        self.assertEqual(normalise_location_query("id_jakarta"), "ID/jakarta")
        self.assertEqual(normalise_location_query("Las Vegas"), "Las Vegas")

    def test_instance_name(self) -> None:
        location = Location(
            iso="US",
            country="United States",
            city="Las Vegas",
            ping=10,
        )
        self.assertEqual(instance_name(location), "us-las-vegas")


class ConnectTests(unittest.TestCase):
    def test_retries_closed_backend_once(self) -> None:
        state = sample_state(61000)
        gateway = Gateway(GatewayConfig(min_port=61000, max_port=61001))
        upstream = mock.Mock()
        with mock.patch(
            "adguard_cli_proxy.gateway.socks5_connect",
            side_effect=[
                SocksError("SOCKS proxy closed the connection unexpectedly"),
                upstream,
            ],
        ) as connect, mock.patch("adguard_cli_proxy.gateway.time.sleep"):
            self.assertIs(gateway.connect(state, "example.com", 443), upstream)
            self.assertEqual(connect.call_count, 2)

    def test_does_not_retry_target_refusal(self) -> None:
        state = sample_state(61000)
        gateway = Gateway(GatewayConfig(min_port=61000, max_port=61001))
        with mock.patch(
            "adguard_cli_proxy.gateway.socks5_connect",
            side_effect=SocksError(
                "SOCKS proxy refused the connection: host unreachable"
            ),
        ) as connect:
            with self.assertRaises(SocksError):
                gateway.connect(state, "example.com", 443)
            self.assertEqual(connect.call_count, 1)


class PoolUrlTests(unittest.TestCase):
    def test_pool_url_is_pathless_and_uses_username(self) -> None:
        state = sample_state(9112)
        gateway = Gateway(GatewayConfig(port=9111))
        self.assertEqual(
            gateway.pool_url(state), "http://US-LAS-VEGAS:x@127.0.0.1:9111"
        )
        self.assertEqual(
            gateway.nine_router_pool(state),
            {
                "name": "AG/US/LAS-VEGAS",
                "proxyUrl": "http://US-LAS-VEGAS:x@127.0.0.1:9111",
                "noProxy": "",
                "type": "http",
            },
        )

    def test_pool_url_uses_loopback_for_wildcard_bind(self) -> None:
        state = sample_state(9112)
        gateway = Gateway(GatewayConfig(host="0.0.0.0", port=9111))
        self.assertEqual(
            gateway.pool_url(state), "http://US-LAS-VEGAS:x@127.0.0.1:9111"
        )


class PortTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.environment = mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": self.temporary.name}
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_allocator_skips_used_state_port(self) -> None:
        save_state(sample_state(61000, "other"))
        gateway = Gateway(GatewayConfig(min_port=61000, max_port=61002))
        with mock.patch(
            "adguard_cli_proxy.gateway.probe_tcp", return_value=False
        ), mock.patch("socket.socket.bind"):
            self.assertEqual(gateway.allocate_port(), 61001)


class GatewayRoutingTests(GatewayServerMixin, unittest.TestCase):
    def test_socks_username_selects_location(self) -> None:
        state = sample_state(0)
        server = self.start_gateway(state)
        with mock.patch.object(server.gateway, "ensure", return_value=state) as ensure:
            client = socks5_connect(
                "127.0.0.1",
                server.server_address[1],
                "example.com",
                80,
                username="US-LAS-VEGAS",
                password="",
            )
            self.addCleanup(client.close)
            client.sendall(b"ping")
            self.assertEqual(client.recv(100), b"echo:ping")
            ensure.assert_called_once_with("US-LAS-VEGAS")

    def test_socks_defaults_when_no_auth(self) -> None:
        state = sample_state(0)
        server = self.start_gateway(state)
        with mock.patch.object(server.gateway, "ensure", return_value=state) as ensure:
            client = socks5_connect(
                "127.0.0.1", server.server_address[1], "example.com", 80
            )
            self.addCleanup(client.close)
            client.sendall(b"ping")
            self.assertEqual(client.recv(100), b"echo:ping")
            ensure.assert_called_once_with("US/LAS-VEGAS")

    def test_http_connect_uses_basic_username(self) -> None:
        state = sample_state(0)
        server = self.start_gateway(state)
        encoded = base64.b64encode(b"ID-JAKARTA:secret").decode()
        with mock.patch.object(server.gateway, "ensure", return_value=state) as ensure:
            address = server.server_address
            with socket.create_connection(address, timeout=5) as client:
                client.sendall(
                    (
                        "CONNECT example.com:443 HTTP/1.1\r\n"
                        "Host: example.com:443\r\n"
                        f"Proxy-Authorization: Basic {encoded}\r\n"
                        "\r\n"
                    ).encode()
                )
                response = client.recv(4096)
                self.assertIn(b"200 Connection Established", response)
                client.sendall(b"ping")
                self.assertEqual(client.recv(100), b"echo:ping")
            ensure.assert_called_once_with("ID/JAKARTA")

    def test_api_returns_mapped_endpoint(self) -> None:
        state = sample_state(0)
        server = self.start_gateway(state)
        with mock.patch.object(server.gateway, "ensure", return_value=state):
            response = self.request(
                b"GET /US/LAS-VEGAS HTTP/1.1\r\nHost: localhost\r\n\r\n"
            )
        self.assertIn(b"200 OK", response)
        body = response.split(b"\r\n\r\n", 1)[1]
        payload = json.loads(body)
        self.assertEqual(payload["mapped_port"], state.listen_port)
        self.assertEqual(payload["endpoint"], state.endpoint)

    def test_api_text_format(self) -> None:
        state = sample_state(0)
        server = self.start_gateway(state)
        with mock.patch.object(server.gateway, "ensure", return_value=state):
            response = self.request(
                b"GET /US/LAS-VEGAS?format=text HTTP/1.1\r\nHost: localhost\r\n\r\n"
            )
        self.assertIn(b"200 OK", response)
        self.assertEqual(
            response.split(b"\r\n\r\n", 1)[1].decode().strip(), state.endpoint
        )

    def test_api_9router_format(self) -> None:
        state = sample_state(0)
        server = self.start_gateway(state)
        expected = (
            f"http://US-LAS-VEGAS:x@127.0.0.1:{server.server_address[1]}\n"
        )
        with mock.patch.object(server.gateway, "ensure", return_value=state):
            response = self.request(
                b"GET /US/LAS-VEGAS?format=9router HTTP/1.1\r\n"
                b"Host: localhost\r\n\r\n"
            )
        self.assertIn(b"200 OK", response)
        self.assertEqual(response.split(b"\r\n\r\n", 1)[1].decode(), expected)

    def test_api_9router_pools(self) -> None:
        state = sample_state(0)
        server = self.start_gateway(state)
        with mock.patch.object(
            server.gateway, "active_states", return_value=[state]
        ):
            response = self.request(
                b"GET /_9router HTTP/1.1\r\nHost: localhost\r\n\r\n"
            )
        self.assertIn(b"200 OK", response)
        payload = json.loads(response.split(b"\r\n\r\n", 1)[1])
        self.assertEqual(
            payload["pools"],
            [
                {
                    "name": "AG/US/LAS-VEGAS",
                    "proxyUrl": (
                        f"http://US-LAS-VEGAS:x@127.0.0.1:"
                        f"{server.server_address[1]}"
                    ),
                    "noProxy": "",
                    "type": "http",
                }
            ],
        )

    def test_health(self) -> None:
        state = sample_state(0)
        self.start_gateway(state)
        response = self.request(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
        self.assertIn(b"200 OK", response)
        self.assertEqual(json.loads(response.split(b"\r\n\r\n", 1)[1]), {"ok": True})


class ChunkedTests(unittest.TestCase):
    def test_chunked_preserves_wire_bytes(self) -> None:
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        payload = b"5\r\nhello\r\n0\r\n\r\n"
        right.sendall(payload)
        self.assertEqual(_read_chunked(left, b""), payload)


if __name__ == "__main__":
    unittest.main()
