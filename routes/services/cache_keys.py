"""Deterministic SHA-256 cache key construction for geocode/route caching.

Pure functions — no Django/ORM dependency — so key derivation can be unit
tested in isolation from the database.
"""
from __future__ import annotations

import hashlib

from routes.domain.types import Coordinates

COORDINATE_PRECISION = 5  # ~1.1m at this latitude range — plenty for cache-key stability


def _normalize_location_text(location: str) -> str:
    return " ".join(location.strip().lower().split())


def _format_coordinates(coordinates: Coordinates) -> str:
    return (
        f"{round(coordinates.latitude, COORDINATE_PRECISION)},"
        f"{round(coordinates.longitude, COORDINATE_PRECISION)}"
    )


def geocode_cache_key(location: str, schema_version: str) -> str:
    raw = f"{schema_version}:{_normalize_location_text(location)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def route_cache_key(
    origin: Coordinates,
    destination: Coordinates,
    profile: str,
    schema_version: str,
) -> str:
    raw = (
        f"{schema_version}:"
        f"{_format_coordinates(origin)}:"
        f"{_format_coordinates(destination)}:"
        f"{profile}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
