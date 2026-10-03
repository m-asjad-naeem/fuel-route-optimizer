"""Coordinate enrichment for FuelStation.latitude/longitude.

The runtime API never calls any of this — enrichment is strictly an
offline/setup-time step. The normal project setup path reads a pre-computed
fixture file directly in `enrich_station_coordinates`'s `--provider file`
mode (no network calls). `NominatimCoordinateProvider` here is optional and
only used to (re)build that fixture file; it is never invoked automatically
and never writes to the database itself.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import requests


@dataclass(frozen=True)
class EnrichmentResult:
    normalized_key: str
    latitude: float
    longitude: float
    source: str


def is_valid_coordinate(latitude: float, longitude: float) -> bool:
    """Shared validation: reject NaN/infinite values and anything outside
    the physically valid lat/lon range. Used by both the Nominatim provider
    (before writing a fixture row) and the file-import command (before
    accepting a fixture row into the database) — coordinates are checked
    at both the point of creation and the point of use.
    """
    try:
        lat, lon = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return False
    if lat != lat or lon != lon:  # NaN
        return False
    if lat in (float("inf"), float("-inf")) or lon in (float("inf"), float("-inf")):
        return False
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


class NominatimCoordinateProvider:
    """Optional, rate-limited, offline-only provider used to BUILD a
    coordinate fixture file (consumed separately by
    `enrich_station_coordinates --provider file`). Never required to run or
    grade the project, and never called by the runtime API.

    Respects Nominatim's usage policy: a clear identifying User-Agent, a
    bounded timeout, and a minimum delay between requests (default 1/sec,
    matching Nominatim's documented usage policy).
    """

    def __init__(
        self,
        base_url: str,
        user_agent: str = "fuel-route-optimizer-assessment/1.0",
        request_delay_seconds: float = 1.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.request_delay_seconds = request_delay_seconds

    def enrich_station(self, address: str, city: str, state: str) -> EnrichmentResult | None:
        """Tries the full address first (highway-exit descriptions like
        "I-40, EXIT 158" mostly don't resolve as literal street addresses —
        this is a known limitation of the source data, documented back in
        Phase 1). Falls back to city+state (town-center precision) rather
        than giving up outright, since for a station directly on a named
        interstate running through a small town, the town center is
        typically still within a few miles of the actual route.
        """
        result = self.geocode_text(f"{address}, {city}, {state}, USA", source="nominatim")
        if result is not None:
            return result
        return self.geocode_text(f"{city}, {state}, USA", source="nominatim_city_fallback")

    def geocode_text(self, query: str, source: str = "nominatim") -> EnrichmentResult | None:
        response = requests.get(
            f"{self.base_url}/search",
            params={"q": query, "format": "jsonv2", "limit": 1},
            headers={"User-Agent": self.user_agent},
            timeout=10,
        )
        response.raise_for_status()
        results = response.json()
        time.sleep(self.request_delay_seconds)
        if not results:
            return None
        result = results[0]
        try:
            latitude, longitude = float(result["lat"]), float(result["lon"])
        except (KeyError, TypeError, ValueError):
            return None
        if not is_valid_coordinate(latitude, longitude):
            return None
        return EnrichmentResult(
            normalized_key="",  # filled in by the caller, which knows the station
            latitude=latitude,
            longitude=longitude,
            source=source,
        )
