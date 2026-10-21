"""Location lookup and endpoint formatting."""

from __future__ import annotations

import re
from urllib.parse import quote

from .adguard import Location, slugify


class LocationError(ValueError):
    """Raised when a location cannot be resolved unambiguously."""


def _normalise(value: str) -> str:
    value = value.strip().upper().replace("&", " AND ")
    value = re.sub(r"[^A-Z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _split_query(query: str) -> tuple[str | None, str]:
    query = query.strip()
    if "/" in query:
        country, city = query.split("/", 1)
        return country.strip(), city.strip()
    return None, query


def _matches_country(location: Location, value: str) -> bool:
    return value.upper() == location.iso or _normalise(value) == _normalise(location.country)


def _matches_city(location: Location, value: str) -> bool:
    normalised = _normalise(value)
    return normalised in {
        _normalise(location.city),
        _normalise(slugify(location.city)),
        _normalise(location.slug),
    }


def resolve_location(locations: list[Location], query: str) -> Location:
    """Resolve ``US/LAS-VEGAS``, ``Las Vegas``, ``US`` or a country name."""
    if not query or not query.strip():
        raise LocationError("Location must not be empty")

    country_query, city_query = _split_query(query)

    if country_query is None:
        country_matches = [item for item in locations if _matches_country(item, city_query)]
        if country_matches:
            # A country/ISO query selects the best (lowest-ping) city.
            return min(country_matches, key=lambda item: (item.ping, item.city))

    candidates = locations
    if country_query is not None:
        country_matches = [item for item in candidates if _matches_country(item, country_query)]
        if not country_matches:
            raise LocationError(f"Unknown country: {country_query!r}")
        candidates = country_matches

    city_matches = [item for item in candidates if _matches_city(item, city_query)]
    if not city_matches:
        available = ", ".join(item.slug for item in candidates[:8])
        suffix = ", ..." if len(candidates) > 8 else ""
        raise LocationError(
            f"Unknown location: {query!r}. Try one of: {available}{suffix}"
        )

    exact = [
        item
        for item in city_matches
        if _normalise(item.city) == _normalise(city_query)
        or _normalise(slugify(item.city)) == _normalise(city_query)
    ]
    if len(exact) == 1:
        return exact[0]
    if len(city_matches) == 1:
        return city_matches[0]

    options = ", ".join(item.slug for item in city_matches)
    raise LocationError(
        f"Location {query!r} is ambiguous. Use one of: {options}"
    )


def is_wildcard(host: str) -> bool:
    return host.strip() in {"0.0.0.0", "::", "[::]", ""}


def is_loopback(host: str) -> bool:
    host = host.strip().lower()
    return host in {"localhost", "127.0.0.1", "::1", "[::1]"} or host.startswith("127.")


def connect_host(host: str) -> str:
    """Address to use when probing a listener bound to ``host`` locally."""
    host = host.strip()
    if host == "0.0.0.0":
        return "127.0.0.1"
    if host in {"::", "[::]"}:
        return "::1"
    if host.lower() == "localhost":
        return "127.0.0.1"
    return host


def format_host(host: str) -> str:
    host = host.strip()
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def format_endpoint(host: str, port: int, location: Location) -> str:
    """Return the canonical ``{ip}:{port}/{ISO}/{CITY-SLUG}`` label."""
    return f"{format_host(host)}:{port}/{location.slug}"


def format_proxy_url(
    host: str,
    port: int,
    username: str | None = None,
    password: str | None = None,
    *,
    scheme: str = "socks5h",
) -> str:
    credentials = ""
    if username:
        credentials = quote(username, safe="")
        if password is not None:
            credentials += ":" + quote(password, safe="")
        credentials += "@"
    return f"{scheme}://{credentials}{format_host(host)}:{port}"
