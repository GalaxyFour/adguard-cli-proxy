from __future__ import annotations

import unittest

from adguard_cli_proxy.adguard import Location
from adguard_cli_proxy.endpoint import (
    LocationError,
    connect_host,
    format_endpoint,
    format_proxy_url,
    is_loopback,
    is_wildcard,
    resolve_location,
)

LOCATIONS = [
    Location("US", "United States", "Las Vegas", 254),
    Location("US", "United States", "Los Angeles", 189),
    Location("GB", "United Kingdom", "London", 178),
    Location("CA", "Canada", "Toronto", 190),
    Location("CA", "Canada", "Vancouver", 200),
]


class ResolveTests(unittest.TestCase):
    def test_full_slug(self) -> None:
        self.assertEqual(resolve_location(LOCATIONS, "US/LAS-VEGAS").city, "Las Vegas")

    def test_city_name(self) -> None:
        self.assertEqual(resolve_location(LOCATIONS, "las vegas").city, "Las Vegas")

    def test_slug_without_country(self) -> None:
        self.assertEqual(resolve_location(LOCATIONS, "LOS-ANGELES").city, "Los Angeles")

    def test_country_selects_fastest(self) -> None:
        self.assertEqual(resolve_location(LOCATIONS, "US").city, "Los Angeles")

    def test_country_name(self) -> None:
        self.assertEqual(resolve_location(LOCATIONS, "Canada/Vancouver").city, "Vancouver")

    def test_unknown(self) -> None:
        with self.assertRaises(LocationError):
            resolve_location(LOCATIONS, "US/ATLANTIS")

    def test_unknown_country(self) -> None:
        with self.assertRaises(LocationError):
            resolve_location(LOCATIONS, "ZZ/LONDON")


class FormatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.location = Location("US", "United States", "Las Vegas", 254)

    def test_endpoint(self) -> None:
        self.assertEqual(
            format_endpoint("203.0.113.10", 9111, self.location),
            "203.0.113.10:9111/US/LAS-VEGAS",
        )

    def test_ipv6_endpoint(self) -> None:
        self.assertEqual(
            format_endpoint("2001:db8::1", 9111, self.location),
            "[2001:db8::1]:9111/US/LAS-VEGAS",
        )

    def test_proxy_url(self) -> None:
        self.assertEqual(
            format_proxy_url("127.0.0.1", 9111),
            "socks5h://127.0.0.1:9111",
        )

    def test_proxy_url_auth_is_encoded(self) -> None:
        self.assertEqual(
            format_proxy_url("127.0.0.1", 9111, "adg", "p@ss word"),
            "socks5h://adg:p%40ss%20word@127.0.0.1:9111",
        )

    def test_host_helpers(self) -> None:
        self.assertTrue(is_wildcard("0.0.0.0"))
        self.assertTrue(is_loopback("127.0.0.1"))
        self.assertFalse(is_loopback("0.0.0.0"))
        self.assertEqual(connect_host("0.0.0.0"), "127.0.0.1")
        self.assertEqual(connect_host("::"), "::1")


if __name__ == "__main__":
    unittest.main()
