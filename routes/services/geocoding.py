"""Geocoding provider abstraction.

The runtime geocoder is used ONLY for the start/destination locations in a
route-plan request. Fuel stations are never geocoded here or anywhere at
request time — their coordinates come exclusively from the `fuel` app's
offline enrichment pipeline (see fuel/coordinate_providers.py).
"""
from __future__ import annotations

from typing import Protocol

import requests

from routes.domain.types import Coordinates
from routes.services.exceptions import GeocodingProviderError, LocationNotFoundError


class GeocodingProvider(Protocol):
    def geocode(self, location: str) -> Coordinates:
        ...


class NominatimGeocodingProvider:
    """Nominatim (OpenStreetMap) implementation.

    Respects Nominatim's usage policy: a clear identifying User-Agent, a
    bounded timeout, and no retry loop — a failed request is surfaced as a
    `GeocodingProviderError` immediately rather than hammered.
    """

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 10.0,
        user_agent: str = "fuel-route-optimizer-assessment/1.0",
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent

    def geocode(self, location: str) -> Coordinates:
        try:
            response = requests.get(
                f"{self.base_url}/search",
                # countrycodes=us constrains Nominatim's own search to the
                # US, using its documented filter mechanism, rather than
                # letting it match an identically-named place anywhere in
                # the world and relying on a separate check afterward — an
                # ambiguous or non-US address (e.g. "Toronto", "Paris" with
                # no US match) fails cleanly here instead of silently
                # resolving to an unintended place outside the service area.
                params={"q": location, "format": "jsonv2", "limit": 1, "countrycodes": "us"},
                headers={"User-Agent": self.user_agent},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except requests.exceptions.Timeout as exc:
            raise GeocodingProviderError(
                f"Nominatim request timed out for location={location!r}"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise GeocodingProviderError(
                f"Nominatim request failed for location={location!r}: {exc}"
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise GeocodingProviderError(
                f"Nominatim returned malformed JSON for location={location!r}"
            ) from exc

        if not isinstance(data, list):
            raise GeocodingProviderError(
                f"Nominatim returned an unexpected response shape for location={location!r}"
            )

        if not data:
            raise LocationNotFoundError(f"No geocoding result found for {location!r}")

        result = data[0]
        try:
            latitude = float(result["lat"])
            longitude = float(result["lon"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GeocodingProviderError(
                f"Nominatim result missing/invalid lat or lon for location={location!r}"
            ) from exc

        return Coordinates(latitude=latitude, longitude=longitude)
