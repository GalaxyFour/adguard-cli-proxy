from __future__ import annotations

import socket
import threading
import unittest

from adguard_cli_proxy.net import SocksError, socks5_connect

HOST = "127.0.0.1"


def recv_exact(connection: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


class FakeSocksServer:
    def __init__(self, *, username: str | None = None, password: str | None = None) -> None:
        self.username = username
        self.password = password
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((HOST, 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.listener.close()
        self.thread.join(timeout=2)
        if self.error:
            raise self.error

    def _serve(self) -> None:
        try:
            connection, _ = self.listener.accept()
            with connection:
                _version, count = recv_exact(connection, 2)
                methods = recv_exact(connection, count)
                if self.username is not None:
                    if 0x02 not in methods:
                        connection.sendall(b"\x05\xff")
                        return
                    connection.sendall(b"\x05\x02")
                    _, name_length = recv_exact(connection, 2)
                    username = recv_exact(connection, name_length).decode()
                    password_length = recv_exact(connection, 1)[0]
                    password = recv_exact(connection, password_length).decode()
                    if (username, password) != (self.username, self.password):
                        connection.sendall(b"\x01\x01")
                        return
                    connection.sendall(b"\x01\x00")
                else:
                    connection.sendall(b"\x05\x00")

                _version, _command, _, address_type = recv_exact(connection, 4)
                if address_type == 0x01:
                    recv_exact(connection, 4)
                elif address_type == 0x03:
                    recv_exact(connection, recv_exact(connection, 1)[0])
                elif address_type == 0x04:
                    recv_exact(connection, 16)
                recv_exact(connection, 2)
                connection.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")

                while True:
                    payload = connection.recv(1024)
                    if not payload:
                        return
                    connection.sendall(payload)
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - surfaced in __exit__
            self.error = exc


class SocksClientTests(unittest.TestCase):
    def test_no_auth(self) -> None:
        with FakeSocksServer() as server:
            client = socks5_connect(HOST, server.port, "example.com", 80)
            try:
                client.sendall(b"ping")
                self.assertEqual(recv_exact(client, 4), b"ping")
            finally:
                client.close()

    def test_username_password(self) -> None:
        with FakeSocksServer(username="adg", password="secret") as server:
            client = socks5_connect(
                HOST,
                server.port,
                "example.com",
                443,
                username="adg",
                password="secret",
            )
            try:
                client.sendall(b"pong")
                self.assertEqual(recv_exact(client, 4), b"pong")
            finally:
                client.close()

    def test_bad_password(self) -> None:
        with FakeSocksServer(username="adg", password="secret") as server, self.assertRaises(
            SocksError
        ):
            socks5_connect(
                HOST,
                server.port,
                "example.com",
                443,
                username="adg",
                password="wrong",
            )


if __name__ == "__main__":
    unittest.main()
