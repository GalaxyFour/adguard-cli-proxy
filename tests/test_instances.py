from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adguard_cli_proxy.instances import (
    InstanceError,
    instance_data_dir,
    resolve_data_dir,
    seed_login,
    validate_instance,
)


class InstanceNameTests(unittest.TestCase):
    def test_valid(self) -> None:
        self.assertEqual(validate_instance("us-las-vegas"), "us-las-vegas")
        self.assertEqual(validate_instance("id_jakarta.1"), "id_jakarta.1")

    def test_invalid(self) -> None:
        for value in ("", "../x", "a/b", ".hidden", "x" * 65):
            with self.subTest(value=value), self.assertRaises(InstanceError):
                validate_instance(value)


class InstancePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.environment = mock.patch.dict(
            os.environ, {"XDG_DATA_HOME": self.temporary.name}
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_data_dir(self) -> None:
        path = instance_data_dir("us-las-vegas")
        self.assertEqual(
            path,
            Path(self.temporary.name)
            / "adguard-cli-proxy"
            / "instances"
            / "us-las-vegas",
        )

    def test_explicit_data_dir(self) -> None:
        self.assertEqual(
            resolve_data_dir("x", "~/vpn-x"),
            Path("~/vpn-x").expanduser(),
        )

    def test_seed_login(self) -> None:
        source = Path(self.temporary.name) / "source.conf"
        source.write_text("session", encoding="utf-8")
        target = instance_data_dir("us-las-vegas")

        self.assertEqual(seed_login(target, source), source)
        copied = target / "adguardvpn-cli.conf"
        self.assertEqual(copied.read_text(encoding="utf-8"), "session")
        self.assertEqual(copied.stat().st_mode & 0o777, 0o600)

        source.write_text("changed", encoding="utf-8")
        self.assertIsNone(seed_login(target, source))
        self.assertEqual(copied.read_text(encoding="utf-8"), "session")


if __name__ == "__main__":
    unittest.main()
