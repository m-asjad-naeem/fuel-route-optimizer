"""Test-only helpers for building synthetic route geometries and placing
stations precisely ON those geometries (using the real projection/Shapely
stack, not guesswork), so corridor-distance assertions in integration tests
are exact rather than approximate.
"""
from __future__ import annotations

import pyproj
from shapely.geometry import LineString
from shapely.ops import transform as shapely_transform

_TRANSFORMER_TO = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
_TRANSFORMER_FROM = pyproj.Transformer.from_crs("EPSG:5070", "EPSG:4326", always_xy=True)


def point_on_route_at_fraction(
    coordinates: list[list[float]], fraction: float
) -> tuple[float, float]:
    """Returns (latitude, longitude) of the point exactly ON the given
    GeoJSON LineString coordinates (lon, lat pairs), at `fraction` of its
    total projected length. fraction=0.5 -> exact midpoint of the line.
    """
    wgs84_line = LineString(coordinates)
    projected_line = shapely_transform(_TRANSFORMER_TO.transform, wgs84_line)
    point = projected_line.interpolate(fraction, normalized=True)
    longitude, latitude = _TRANSFORMER_FROM.transform(point.x, point.y)
    return latitude, longitude
