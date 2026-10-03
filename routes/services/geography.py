"""USA country-boundary validation.

Replaces a plain lat/lon rectangle (which incorrectly accepts points in
southern Canada and northern Mexico that happen to fall inside the box —
confirmed independently: Toronto (43.65, -79.38) and Monterrey (25.67,
-100.31) both pass a 24-50 lat / -125..-66 lon rectangle) with an actual
point-in-polygon test against the real US country boundary.

`data/usa_boundary.geojson` is a simplified (~450-vertex) MultiPolygon
covering the continental US, Alaska, and Hawaii, derived from the public-
domain Natural Earth admin-0 countries dataset (via
github.com/johan/world.geo.json). It is a one-time, offline, bundled
reference file — never fetched at request time — so this adds no network
call and no new runtime dependency (Shapely is already a project
dependency for route/station geometry).

This check is independent of the EPSG:5070 Albers projection used
elsewhere (routes/services/geospatial.py) for route/station distance math:
that projection is CONUS-only and stays CONUS-only; this module only
answers "is this point inside the USA," in plain WGS84 degrees, which is
valid everywhere the boundary data itself covers (including Alaska and
Hawaii).
"""
from __future__ import annotations

import json
from pathlib import Path

from shapely.geometry import Point, shape
from shapely.prepared import prep

_BOUNDARY_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "usa_boundary.geojson"


def _load_usa_geometry():
    with open(_BOUNDARY_PATH, encoding="utf-8") as fh:
        geometry = shape(json.load(fh))
    if not geometry.is_valid:
        geometry = geometry.buffer(0)
    return prep(geometry), geometry.bounds  # (minx, miny, maxx, maxy)


_PREPARED_USA, _USA_BOUNDS = _load_usa_geometry()


def is_within_usa(latitude: float, longitude: float) -> bool:
    """True if (latitude, longitude) falls inside the USA (all 50 states +
    DC, per the bundled boundary data), using a cheap bounding-box check
    first (free for the overwhelming majority of out-of-scope requests)
    before the exact point-in-polygon test.
    """
    min_lon, min_lat, max_lon, max_lat = _USA_BOUNDS
    if not (min_lat <= latitude <= max_lat and min_lon <= longitude <= max_lon):
        return False
    return _PREPARED_USA.contains(Point(longitude, latitude))
