from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adguard_cli_proxy.state import State, clear_state, load_state, save_state


def sample_state() -> State:
    return State(
        iso="US",
        country="United States",
        city="Las Vegas",
        slug="US/LAS-VEGAS",
        listen_host="127.0.0.1",
        listen_port=9111,
        advertise_host="127.0.0.1",
        endpoint="127.0.0.1:9111/US/LAS-VEGAS",
        proxy_url="socks5h://127.0.0.1:9111",
        username=None,
        password=None,
        egress_ip="185.242.5.198",
        connected_at="2026-10-21T00:00:00+00:00",
    )


class StateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.environment = mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": self.temporary.name}
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_round_trip(self) -> None:
        state = sample_state()
        path = save_state(state)
        self.assertEqual(load_state(), state)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_clear(self) -> None:
        save_state(sample_state())
        clear_state()
        self.assertIsNone(load_state())

    def test_corrupt_state(self) -> None:
        root = Path(self.temporary.name) / "adguard-cli-proxy"
        root.mkdir(parents=True)
        (root / "state.json").write_text("{broken", encoding="utf-8")
        self.assertIsNone(load_state())


if __name__ == "__main__":
    unittest.main()
