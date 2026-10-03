"""Framework-independent geospatial station selection.

Turns a route's GeoJSON geometry plus a list of candidate stations into an
ordered list of `StationCandidate`s, using Shapely + pyproj for all distance
and position math. No Django ORM, no HTTP, no PostGIS — this module can be
unit tested with nothing but synthetic coordinates.

This module produces two distinct distance values that must never be
confused with each other:

- `route_position_miles` — the station's position along the Shapely-
  projected polyline. Used only for ordering and corridor-membership
  (perpendicular distance). Not guaranteed to agree with OSRM's
  authoritative road distance, since OSRM's geometry is a simplified
  overview polyline whose own length can differ from the real road
  distance it represents.
- `travel_distance_miles` — the station's authoritative distance from the
  route's start, derived from OSRM's own per-segment distances
  (`Route.segment_distances_miles`, requested via `annotations=distance` on
  the same single routing call — see routes/services/routing.py). This is
  the only distance the fuel optimizer may use.

`travel_distance_miles` is computed without a route-wide scale factor: a
station's nearest point on the polyline falls between two consecutive
geometry vertices, i and i+1. OSRM gives the exact road distance covered
by every full segment (`segment_distances_miles[i]`). Only the station's
fractional position within that one segment needs any approximation,
computed from the local ratio of projected distances to vertex i and i+1:

    travel_distance_miles = cumulative_osrm_distance[i]
                             + local_fraction * segment_distances_miles[i]

Every full segment behind the station uses OSRM's exact number; only the
remainder within one (typically short) segment is interpolated. The error
is bounded by the length of one polyline segment, not by the route's
total length — unlike a single `osrm_total / projected_total` ratio
applied across the whole route, which assumes any discrepancy is
uniformly distributed along it.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import Any, Iterable

import pyproj
from shapely.geometry import LineString, Point
from shapely.ops import transform as shapely_transform

from routes.domain.types import StationCandidate, StationLocation

METERS_PER_MILE = 1609.344
WGS84 = "EPSG:4326"


class InvalidRouteGeometryError(ValueError):
    """The route geometry is missing, malformed, or an unsupported type.

    Distinct from the provider exceptions in exceptions.py: this validates
    geometry that may have come from a cache or any other source, not
    specifically a fresh OSRM response (OSRM's own response is already
    validated in routing.py at fetch time — this is a second, independent
    guard for whatever geometry this service is actually handed).
    """


@dataclass(frozen=True)
class _ProjectedRoute:
    line: LineString
    length_miles: float
    # Cumulative projected (EPSG:5070, meters) distance to each vertex of
    # `line`, same indexing as the original geometry's coordinate list.
    # Used ONLY to locate which segment a station's nearest point falls
    # into — a purely local lookup, never a route-wide ratio.
    cumulative_vertex_distances_meters: list[float]


class RouteGeospatialService:
    """Projects a route + candidate stations into EPSG:5070 and derives
    corridor membership, perpendicular distance, and route-relative position
    for each station.

    EPSG:5070 (NAD83 / Conus Albers) is a practical, equal-area, meter-based
    CRS appropriate for continental-US routes — not a claim that it is
    universally geographically exact (it would need reconsidering for
    Alaska, Hawaii, or international routes). It is passed in, not
    hardcoded, so it can be swapped without touching the rest of this class.
    """

    def __init__(self, projected_crs: str = "EPSG:5070", corridor_miles: float = 5.0):
        self.projected_crs = projected_crs
        self.corridor_miles = corridor_miles
        self._transformer = pyproj.Transformer.from_crs(WGS84, projected_crs, always_xy=True)

    def select_candidates(
        self,
        route_geometry: dict[str, Any],
        stations: Iterable[StationLocation],
        segment_distances_miles: list[float],
    ) -> list[StationCandidate]:
        """Returns StationCandidates within `corridor_miles` of the route,
        sorted ascending by `route_position_miles` (ordering only).

        `segment_distances_miles` is OSRM's authoritative per-segment road
        distance array (`Route.segment_distances_miles`) — REQUIRED, not
        optional: without it there is no sound way to derive
        `travel_distance_miles`, and silently falling back to the
        projected polyline's own length is exactly the conflation this
        parameter exists to prevent.
        """
        projected_route = self._project_route(route_geometry)
        self._validate_segment_distances(projected_route, segment_distances_miles)
        cumulative_osrm_miles = self._cumulative(segment_distances_miles)

        candidates = []
        for station in stations:
            if not self._has_valid_data(station):
                continue

            point = self._project_point(station.latitude, station.longitude)
            distance_miles = projected_route.line.distance(point) / METERS_PER_MILE
            if distance_miles > self.corridor_miles:
                continue

            position_meters = projected_route.line.project(point)
            position_miles = position_meters / METERS_PER_MILE
            travel_distance_miles = self._travel_distance_miles(
                position_meters, projected_route, segment_distances_miles, cumulative_osrm_miles
            )

            candidates.append(
                StationCandidate(
                    station_id=station.station_id,
                    route_position_miles=position_miles,
                    travel_distance_miles=travel_distance_miles,
                    distance_from_route_miles=distance_miles,
                    price_per_gallon=station.price_per_gallon,
                )
            )

        candidates.sort(key=lambda c: (c.route_position_miles, c.station_id))
        return candidates

    def projected_length_discrepancy_miles(
        self, route_geometry: dict[str, Any], route_distance_miles: float
    ) -> float:
        """Diagnostic only: how far this polyline's own projected length is
        from OSRM's authoritative TOTAL distance. Never consumed by
        `select_candidates` — exists so a caller that wants to log/alert on
        a large discrepancy (e.g. a badly simplified geometry) can do so
        explicitly. Per-station travel distance no longer depends on this
        aggregate figure at all (see `travel_distance_miles`, which uses
        OSRM's per-segment data instead).
        """
        projected_route = self._project_route(route_geometry)
        return abs(projected_route.length_miles - route_distance_miles)

    # -- internals ---------------------------------------------------

    def _project_route(self, route_geometry: dict[str, Any]) -> _ProjectedRoute:
        self._validate_geometry(route_geometry)
        wgs84_line = LineString(route_geometry["coordinates"])
        projected_line = shapely_transform(self._transformer.transform, wgs84_line)

        vertices = list(projected_line.coords)
        cumulative = [0.0]
        for (x1, y1), (x2, y2) in zip(vertices, vertices[1:]):
            segment_length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
            cumulative.append(cumulative[-1] + segment_length)

        return _ProjectedRoute(
            line=projected_line,
            length_miles=projected_line.length / METERS_PER_MILE,
            cumulative_vertex_distances_meters=cumulative,
        )

    def _project_point(self, latitude: float, longitude: float) -> Point:
        x, y = self._transformer.transform(longitude, latitude)
        return Point(x, y)

    @staticmethod
    def _cumulative(values: list[float]) -> list[float]:
        cumulative = [0.0]
        for v in values:
            cumulative.append(cumulative[-1] + v)
        return cumulative

    @staticmethod
    def _travel_distance_miles(
        position_meters: float,
        projected_route: _ProjectedRoute,
        segment_distances_miles: list[float],
        cumulative_osrm_miles: list[float],
    ) -> float:
        """The authoritative distance along the route to a point that
        projects to `position_meters` along the projected polyline.

        Finds which single segment [i, i+1) the point falls into (using the
        projected polyline's own per-vertex cumulative distances — a purely
        local lookup), then interpolates ONLY within that one segment using
        OSRM's exact distance for it. Every segment before it contributes
        its exact OSRM distance, never an estimate.
        """
        vertex_distances = projected_route.cumulative_vertex_distances_meters
        # bisect_right - 1 gives the last vertex index whose cumulative
        # distance is <= position_meters, i.e. the segment this point is in.
        segment_index = bisect_right(vertex_distances, position_meters) - 1
        segment_index = max(0, min(segment_index, len(segment_distances_miles) - 1))

        segment_start_meters = vertex_distances[segment_index]
        segment_length_meters = (
            vertex_distances[segment_index + 1] - vertex_distances[segment_index]
        )

        if segment_length_meters <= 0:
            local_fraction = 0.0
        else:
            local_fraction = (position_meters - segment_start_meters) / segment_length_meters
            local_fraction = max(0.0, min(1.0, local_fraction))

        return (
            cumulative_osrm_miles[segment_index]
            + local_fraction * segment_distances_miles[segment_index]
        )

    @staticmethod
    def _validate_segment_distances(
        projected_route: _ProjectedRoute, segment_distances_miles: list[float]
    ) -> None:
        expected = len(projected_route.cumulative_vertex_distances_meters) - 1
        if not isinstance(segment_distances_miles, list):
            raise InvalidRouteGeometryError(
                f"segment_distances_miles must be a list, got {type(segment_distances_miles)!r}"
            )
        if len(segment_distances_miles) != expected:
            raise InvalidRouteGeometryError(
                f"segment_distances_miles must have {expected} entries "
                f"(one per geometry segment), got {len(segment_distances_miles)}"
            )
        if any((not isinstance(d, (int, float))) or d < 0 for d in segment_distances_miles):
            raise InvalidRouteGeometryError("segment_distances_miles must be non-negative numbers")

    @staticmethod
    def _validate_geometry(route_geometry: dict[str, Any]) -> None:
        if not isinstance(route_geometry, dict):
            raise InvalidRouteGeometryError("Route geometry must be a GeoJSON-shaped dict")

        geometry_type = route_geometry.get("type")
        if geometry_type != "LineString":
            raise InvalidRouteGeometryError(
                f"Unsupported geometry type {geometry_type!r} — only a single "
                "LineString is supported in v1 (a MultiLineString route is not "
                "supported; OSRM's overview=full driving geometry is always a "
                "single LineString)"
            )

        coordinates = route_geometry.get("coordinates")
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            raise InvalidRouteGeometryError(
                "Route geometry must have at least 2 coordinate pairs"
            )

        for pair in coordinates:
            if (
                not isinstance(pair, (list, tuple))
                or len(pair) != 2
                or not all(isinstance(v, (int, float)) for v in pair)
            ):
                raise InvalidRouteGeometryError(f"Malformed coordinate pair: {pair!r}")

    @staticmethod
    def _has_valid_data(station: StationLocation) -> bool:
        if station.latitude is None or station.longitude is None:
            return False
        if station.price_per_gallon is None:
            return False
        try:
            lat = float(station.latitude)
            lon = float(station.longitude)
        except (TypeError, ValueError):
            return False
        if lat != lat or lon != lon:  # NaN check, without importing math
            return False
        if not (-90.0 <= lat <= 90.0):
            return False
        if not (-180.0 <= lon <= 180.0):
            return False
        return True
