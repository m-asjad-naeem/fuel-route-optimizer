"""Routing provider abstraction.

Exactly one OSRM call per uncached (origin, destination) pair — station
candidate selection and fuel optimization never trigger additional
routing calls; they operate on the single Route this returns.
"""
from __future__ import annotations

from typing import Protocol

import requests

from routes.domain.types import Coordinates, Route
from routes.services.exceptions import RouteNotFoundError, RoutingProviderError

METERS_PER_MILE = 1609.344


class RoutingProvider(Protocol):
    def route(self, origin: Coordinates, destination: Coordinates) -> Route:
        ...


class OSRMRoutingProvider:
    """OSRM implementation using the public `/route/v1/{profile}/...` API.

    Requests full-resolution GeoJSON geometry (`overview=full`,
    `geometries=geojson`) in a single call, and validates the response
    thoroughly before constructing a `Route` — callers only ever see a
    valid `Route` or a raised exception, never a partially-malformed
    payload.
    """

    def __init__(self, base_url: str, profile: str = "driving", timeout_seconds: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.profile = profile
        self.timeout_seconds = timeout_seconds

    def route(self, origin: Coordinates, destination: Coordinates) -> Route:
        url = (
            f"{self.base_url}/route/v1/{self.profile}/"
            f"{origin.longitude},{origin.latitude};"
            f"{destination.longitude},{destination.latitude}"
        )
        try:
            response = requests.get(
                url,
                params={
                    "overview": "full",
                    "geometries": "geojson",
                    "annotations": "distance",
                },
                timeout=self.timeout_seconds,
            )
        except requests.exceptions.Timeout as exc:
            raise RoutingProviderError("OSRM request timed out") from exc
        except requests.exceptions.RequestException as exc:
            raise RoutingProviderError(f"OSRM request failed: {exc}") from exc

        # OSRM reports routing-level outcomes (including a genuinely
        # impossible route) as a JSON body with a `code` field — and, for
        # some of those outcomes (confirmed live: an unreachable pair of
        # points returns HTTP 400 with `{"code": "NoRoute", ...}`), as a
        # non-2xx HTTP status at the same time. The body must be inspected
        # BEFORE deciding how to treat the HTTP status, or a genuine
        # "no route exists" result gets misread as a provider outage.
        try:
            data = response.json()
        except ValueError as exc:
            # No parseable body at all (e.g. a plain-text 5xx from a proxy
            # in front of OSRM) — this really is a provider-level failure.
            raise RoutingProviderError(
                f"OSRM returned malformed JSON (HTTP {response.status_code})"
            ) from exc

        if not isinstance(data, dict):
            raise RoutingProviderError(
                f"OSRM returned an unexpected response shape (HTTP {response.status_code})"
            )

        code = data.get("code")
        if code == "NoRoute" or code == "NoSegment":
            # A genuinely impossible route, or a point too far from any
            # road network segment — both are the client's route being
            # unreachable, not a provider malfunction, regardless of
            # whether OSRM reported it with a 200 or a 400.
            raise RouteNotFoundError("OSRM found no route between the given points")
        if code != "Ok":
            # Any other non-Ok code (malformed query, invalid options, …)
            # reflects a problem with our own request, not the user's
            # route — surfaced as a provider error rather than silently
            # treated as a clean "not found".
            raise RoutingProviderError(
                f"OSRM returned non-Ok code: {code!r} (HTTP {response.status_code})"
            )
        if response.status_code >= 400:
            # code == "Ok" should never co-occur with an error status, but
            # if it somehow does, don't trust a successful-looking body
            # behind an error status.
            raise RoutingProviderError(
                f"OSRM returned HTTP {response.status_code} with code 'Ok'"
            )

        routes = data.get("routes")
        if not routes or not isinstance(routes, list):
            raise RouteNotFoundError("OSRM response contained no routes")

        route_data = routes[0]

        try:
            distance_meters = float(route_data["distance"])
            duration_seconds = float(route_data["duration"])
            geometry = route_data["geometry"]
        except (KeyError, TypeError, ValueError) as exc:
            raise RoutingProviderError(
                "OSRM route is missing distance, duration, or geometry"
            ) from exc

        if not isinstance(geometry, dict) or geometry.get("type") != "LineString":
            raise RoutingProviderError("OSRM geometry is missing or not a LineString")

        coordinates = geometry.get("coordinates")
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            raise RoutingProviderError("OSRM geometry has empty or insufficient coordinates")

        segment_distances_miles = self._extract_segment_distances(route_data, len(coordinates))

        return Route(
            distance_miles=distance_meters / METERS_PER_MILE,
            duration_minutes=duration_seconds / 60.0,
            geometry=geometry,
            segment_distances_miles=segment_distances_miles,
        )

    @staticmethod
    def _extract_segment_distances(route_data: dict, coordinate_count: int) -> list[float]:
        """Authoritative per-segment road distances, one entry between each
        consecutive pair of geometry coordinates — requested via
        `annotations=distance` on the SAME single route call (no extra HTTP
        request). This is what lets travel distance along the route be
        computed without a route-wide scale factor and without a routing
        call per station (see routes/services/geospatial.py).
        """
        legs = route_data.get("legs")
        if not isinstance(legs, list) or not legs:
            raise RoutingProviderError("OSRM route is missing legs/annotation distance data")

        segment_meters: list[float] = []
        for leg in legs:
            annotation = leg.get("annotation") if isinstance(leg, dict) else None
            distances = annotation.get("distance") if isinstance(annotation, dict) else None
            if not isinstance(distances, list):
                raise RoutingProviderError(
                    "OSRM route is missing legs[].annotation.distance"
                )
            try:
                segment_meters.extend(float(d) for d in distances)
            except (TypeError, ValueError) as exc:
                raise RoutingProviderError(
                    "OSRM route has non-numeric segment distances"
                ) from exc

        if len(segment_meters) != coordinate_count - 1:
            raise RoutingProviderError(
                f"OSRM segment distance count ({len(segment_meters)}) does not match "
                f"geometry coordinate count ({coordinate_count}) minus one"
            )

        return [m / METERS_PER_MILE for m in segment_meters]
