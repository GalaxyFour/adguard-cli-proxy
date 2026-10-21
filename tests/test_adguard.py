from __future__ import annotations

import unittest

from adguard_cli_proxy.adguard import (
    AdGuardCLI,
    Location,
    parse_locations,
    slugify,
    strip_ansi,
)

SAMPLE = """\
\x1b[1mISO   COUNTRY              CITY                           PING ESTIMATE\x1b[0m
US    United States        Silicon Valley                 184
US    United States        Las Vegas                      254
NL    Netherlands          Amsterdam                      150
GB    United Kingdom       London                         178
"""


class ParseTests(unittest.TestCase):
    def test_strip_ansi(self) -> None:
        self.assertEqual(strip_ansi("\x1b[1mhello\x1b[0m"), "hello")

    def test_parse_locations(self) -> None:
        locations = parse_locations(SAMPLE)
        self.assertEqual([item.slug for item in locations], [
            "US/SILICON-VALLEY",
            "US/LAS-VEGAS",
            "NL/AMSTERDAM",
            "GB/LONDON",
        ])
        self.assertEqual(locations[1].country, "United States")
        self.assertEqual(locations[1].ping, 254)
        self.assertEqual(locations[3].label, "London, United Kingdom")

    def test_parse_ignores_noise(self) -> None:
        self.assertEqual(parse_locations("not a table\n"), [])

    def test_slugify(self) -> None:
        self.assertEqual(slugify("Las Vegas"), "LAS-VEGAS")
        self.assertEqual(slugify("St. Petersburg"), "ST-PETERSBURG")
        self.assertEqual(slugify("Mumbai (Virtual)"), "MUMBAI-VIRTUAL")
        self.assertEqual(slugify("Bosnia & Herzegovina"), "BOSNIA-AND-HERZEGOVINA")

    def test_location_slug(self) -> None:
        location = Location("US", "United States", "Las Vegas", 254)
        self.assertEqual(location.slug, "US/LAS-VEGAS")


class StatusTests(unittest.TestCase):
    def test_connected(self) -> None:
        cli = AdGuardCLI(binary="/bin/true")
        cli._run = lambda *args, **kwargs: (  # type: ignore[method-assign]
            "Connected to LAS VEGAS in SOCKS mode, listening on 127.0.0.1:9111"
        )
        status = cli.status()
        self.assertTrue(status.connected)
        self.assertEqual(status.location, "LAS VEGAS")
        self.assertEqual(status.mode, "socks")
        self.assertEqual(status.port, 9111)

    def test_disconnected(self) -> None:
        cli = AdGuardCLI(binary="/bin/true")
        cli._run = lambda *args, **kwargs: "VPN is disconnected"  # type: ignore[method-assign]
        self.assertFalse(cli.status().connected)


if __name__ == "__main__":
    unittest.main()
