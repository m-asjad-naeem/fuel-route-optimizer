from decimal import Decimal

import pytest

from routes.domain.types import StationLocation
from routes.services.geospatial import InvalidRouteGeometryError, RouteGeospatialService

# A straight, roughly east-west route across the CONUS midlatitudes — well
# inside EPSG:5070's intended scope. Three points / two segments, so tests
# also exercise a route with more than a single straight segment.
STRAIGHT_ROUTE = {
    "type": "LineString",
    "coordinates": [[-100.0, 39.0], [-95.0, 39.0], [-90.0, 39.0]],
}

SERVICE = RouteGeospatialService(corridor_miles=5.0)


def station(station_id, lat, lon, price="3.50"):
    return StationLocation(
        station_id=station_id, latitude=lat, longitude=lon, price_per_gallon=Decimal(price)
    )


def exact_segments(route_geometry, service=SERVICE):
    """Per-segment distances that exactly match the projected polyline's own
    segment lengths — i.e. simulating an OSRM response with ZERO
    discrepancy from the projected geometry. Used by every test that isn't
    specifically exercising the discrepancy case, so travel_distance_miles
    equals route_position_miles and all the original position/corridor
    assertions keep meaning what they said before Phase 6.1."""
    projected = service._project_route(route_geometry)
    vertex_distances = projected.cumulative_vertex_distances_meters
    return [
        (vertex_distances[i + 1] - vertex_distances[i]) / 1609.344
        for i in range(len(vertex_distances) - 1)
    ]


# ---- corridor membership / perpendicular distance ----------------------


def test_station_exactly_on_route_has_zero_distance():
    segments = exact_segments(STRAIGHT_ROUTE)
    candidates = SERVICE.select_candidates(STRAIGHT_ROUTE, [station(1, 39.0, -95.0)], segments)
    assert len(candidates) == 1
    assert candidates[0].distance_from_route_miles == pytest.approx(0.0, abs=1e-6)


def test_station_just_inside_corridor_is_included():
    # ~0.05 deg latitude offset at this longitude is well under 5 miles
    segments = exact_segments(STRAIGHT_ROUTE)
    candidates = SERVICE.select_candidates(STRAIGHT_ROUTE, [station(1, 39.05, -95.0)], segments)
    assert len(candidates) == 1
    assert candidates[0].distance_from_route_miles < 5.0


def test_station_just_outside_corridor_is_excluded():
    # ~0.15 deg latitude offset is comfortably over 5 miles
    segments = exact_segments(STRAIGHT_ROUTE)
    candidates = SERVICE.select_candidates(STRAIGHT_ROUTE, [station(1, 39.15, -95.0)], segments)
    assert candidates == []


def test_corridor_width_is_configurable():
    far_station = station(1, 39.15, -95.0)
    narrow = RouteGeospatialService(corridor_miles=5.0)
    wide = RouteGeospatialService(corridor_miles=20.0)
    segments = exact_segments(STRAIGHT_ROUTE, narrow)

    assert narrow.select_candidates(STRAIGHT_ROUTE, [far_station], segments) == []
    assert len(wide.select_candidates(STRAIGHT_ROUTE, [far_station], segments)) == 1


# ---- route-relative position (ordering only) -----------------------------


def test_station_near_route_start_has_position_near_zero():
    segments = exact_segments(STRAIGHT_ROUTE)
    candidates = SERVICE.select_candidates(STRAIGHT_ROUTE, [station(1, 39.0, -99.9)], segments)
    assert candidates[0].route_position_miles < 10.0


def test_station_near_route_end_has_position_near_total_length():
    total_length_miles = SERVICE._project_route(STRAIGHT_ROUTE).length_miles
    segments = exact_segments(STRAIGHT_ROUTE)
    candidates = SERVICE.select_candidates(STRAIGHT_ROUTE, [station(1, 39.0, -90.1)], segments)
    assert candidates[0].route_position_miles > total_length_miles - 10.0


def test_station_before_route_start_clamps_position_to_zero():
    # due west of the route's start point, collinear with it
    wide_service = RouteGeospatialService(corridor_miles=1000.0)
    segments = exact_segments(STRAIGHT_ROUTE, wide_service)
    candidates = wide_service.select_candidates(
        STRAIGHT_ROUTE, [station(1, 39.0, -110.0)], segments
    )
    assert candidates[0].route_position_miles == pytest.approx(0.0, abs=1e-6)


def test_station_after_route_end_clamps_position_to_total_length():
    service = RouteGeospatialService(corridor_miles=1000.0)
    total_length_miles = service._project_route(STRAIGHT_ROUTE).length_miles
    segments = exact_segments(STRAIGHT_ROUTE, service)
    candidates = service.select_candidates(STRAIGHT_ROUTE, [station(1, 39.0, -80.0)], segments)
    assert candidates[0].route_position_miles == pytest.approx(total_length_miles, rel=1e-6)


def test_multiple_stations_ordered_correctly_along_route():
    stations = [
        station(3, 39.0, -91.0),  # near the end
        station(1, 39.0, -99.0),  # near the start
        station(2, 39.0, -95.0),  # middle
    ]
    segments = exact_segments(STRAIGHT_ROUTE)
    candidates = SERVICE.select_candidates(STRAIGHT_ROUTE, stations, segments)

    assert [c.station_id for c in candidates] == [1, 2, 3]
    positions = [c.route_position_miles for c in candidates]
    assert positions == sorted(positions)


def test_route_with_multiple_segments_still_orders_monotonically():
    dense_route = {
        "type": "LineString",
        "coordinates": [
            [-100.0, 39.0],
            [-98.0, 39.0],
            [-96.0, 39.0],
            [-94.0, 39.0],
            [-92.0, 39.0],
            [-90.0, 39.0],
        ],
    }
    stations = [station(i, 39.0, lon) for i, lon in enumerate([-99, -97, -95, -93, -91])]
    segments = exact_segments(dense_route)
    candidates = SERVICE.select_candidates(dense_route, stations, segments)

    positions = [c.route_position_miles for c in candidates]
    assert positions == sorted(positions)


def test_duplicate_nearby_stations_both_retained_with_stable_tiebreak():
    stations = [station(2, 39.0, -95.0), station(1, 39.0, -95.0)]
    segments = exact_segments(STRAIGHT_ROUTE)
    candidates = SERVICE.select_candidates(STRAIGHT_ROUTE, stations, segments)

    assert len(candidates) == 2
    # same route position -> tie-broken by station_id ascending
    assert [c.station_id for c in candidates] == [1, 2]


# ---- exclusion rules -----------------------------------------------------


def test_missing_latitude_excludes_station():
    s = StationLocation(
        station_id=1, latitude=None, longitude=-95.0, price_per_gallon=Decimal("3.5")
    )
    segments = exact_segments(STRAIGHT_ROUTE)
    assert SERVICE.select_candidates(STRAIGHT_ROUTE, [s], segments) == []


def test_missing_longitude_excludes_station():
    s = StationLocation(
        station_id=1, latitude=39.0, longitude=None, price_per_gallon=Decimal("3.5")
    )
    segments = exact_segments(STRAIGHT_ROUTE)
    assert SERVICE.select_candidates(STRAIGHT_ROUTE, [s], segments) == []


def test_missing_effective_price_excludes_station():
    s = StationLocation(station_id=1, latitude=39.0, longitude=-95.0, price_per_gallon=None)
    segments = exact_segments(STRAIGHT_ROUTE)
    assert SERVICE.select_candidates(STRAIGHT_ROUTE, [s], segments) == []


def test_out_of_range_latitude_excludes_station():
    segments = exact_segments(STRAIGHT_ROUTE)
    assert SERVICE.select_candidates(STRAIGHT_ROUTE, [station(1, 200.0, -95.0)], segments) == []


def test_out_of_range_longitude_excludes_station():
    segments = exact_segments(STRAIGHT_ROUTE)
    assert SERVICE.select_candidates(STRAIGHT_ROUTE, [station(1, 39.0, -400.0)], segments) == []


def test_nan_coordinates_excludes_station():
    segments = exact_segments(STRAIGHT_ROUTE)
    result = SERVICE.select_candidates(STRAIGHT_ROUTE, [station(1, float("nan"), -95.0)], segments)
    assert result == []


# ---- geometry validation ---------------------------------------------------


def test_empty_coordinates_raises_invalid_route_geometry():
    with pytest.raises(InvalidRouteGeometryError):
        SERVICE.select_candidates({"type": "LineString", "coordinates": []}, [], [])


def test_single_point_coordinates_raises_invalid_route_geometry():
    with pytest.raises(InvalidRouteGeometryError):
        SERVICE.select_candidates({"type": "LineString", "coordinates": [[-95.0, 39.0]]}, [], [])


def test_wrong_geometry_type_raises_invalid_route_geometry():
    with pytest.raises(InvalidRouteGeometryError):
        SERVICE.select_candidates({"type": "Point", "coordinates": [-95.0, 39.0]}, [], [])


def test_multilinestring_geometry_raises_invalid_route_geometry():
    multi = {
        "type": "MultiLineString",
        "coordinates": [[[-100.0, 39.0], [-95.0, 39.0]], [[-95.0, 39.0], [-90.0, 39.0]]],
    }
    with pytest.raises(InvalidRouteGeometryError):
        SERVICE.select_candidates(multi, [], [])


def test_malformed_coordinate_pair_raises_invalid_route_geometry():
    bad = {"type": "LineString", "coordinates": [[-100.0, 39.0], ["not-a-number", 39.0]]}
    with pytest.raises(InvalidRouteGeometryError):
        SERVICE.select_candidates(bad, [], [])


def test_non_dict_geometry_raises_invalid_route_geometry():
    with pytest.raises(InvalidRouteGeometryError):
        SERVICE.select_candidates(None, [], [])


# ---- segment_distances_miles validation -----------------------------------


def test_wrong_segment_count_raises_invalid_route_geometry():
    # STRAIGHT_ROUTE has 2 segments; supplying only 1 distance is invalid.
    with pytest.raises(InvalidRouteGeometryError):
        SERVICE.select_candidates(STRAIGHT_ROUTE, [], [100.0])


def test_negative_segment_distance_raises_invalid_route_geometry():
    with pytest.raises(InvalidRouteGeometryError):
        SERVICE.select_candidates(STRAIGHT_ROUTE, [], [100.0, -5.0])


def test_non_list_segment_distances_raises_invalid_route_geometry():
    with pytest.raises(InvalidRouteGeometryError):
        SERVICE.select_candidates(STRAIGHT_ROUTE, [], "not-a-list")


# ---- travel_distance_miles: authoritative, not a route-wide scale factor -


def test_travel_distance_matches_route_position_when_segments_agree_exactly():
    # Baseline: when segment_distances_miles exactly matches the projected
    # polyline's own segment lengths (no discrepancy), travel_distance_miles
    # and route_position_miles must agree.
    segments = exact_segments(STRAIGHT_ROUTE)
    candidates = SERVICE.select_candidates(STRAIGHT_ROUTE, [station(1, 39.0, -95.0)], segments)
    stop = candidates[0]
    assert stop.travel_distance_miles == pytest.approx(stop.route_position_miles, rel=1e-6)


def test_travel_distance_uses_authoritative_segments_not_projected_length():
    # Deliberately make the "OSRM" segment distances LARGER than the
    # projected polyline's own segment lengths (simulating OSRM reporting a
    # longer real-world road distance than the simplified polyline
    # represents). travel_distance_miles must track the authoritative
    # (larger) figure, NOT the projected route_position_miles.
    exact = exact_segments(STRAIGHT_ROUTE)
    inflated = [d * 1.5 for d in exact]  # every OSRM segment 50% longer

    candidates = SERVICE.select_candidates(STRAIGHT_ROUTE, [station(1, 39.0, -95.0)], inflated)
    stop = candidates[0]

    # the station sits on the route's first segment's far end (midpoint of
    # the whole route, which for this 2-segment route is exactly vertex 1)
    assert stop.travel_distance_miles == pytest.approx(inflated[0], rel=1e-3)
    assert stop.route_position_miles == pytest.approx(exact[0], rel=1e-3)
    # the two numbers must now clearly disagree — proving travel_distance
    # is NOT silently derived from route_position_miles (or any uniform
    # scale factor applied to it)
    assert stop.travel_distance_miles != pytest.approx(stop.route_position_miles, rel=1e-3)


def test_travel_distance_interpolates_locally_within_one_segment_only():
    # A station roughly a quarter of the way along the first segment. Only
    # that ONE segment's authoritative distance should be interpolated —
    # the second (inflated) segment must have ZERO effect on this station's
    # travel_distance_miles, proving the interpolation is local, not a
    # route-wide ratio.
    exact = exact_segments(STRAIGHT_ROUTE)
    first_segment_only_inflated = [exact[0], exact[1] * 10.0]  # wildly inflate 2nd segment only

    near_quarter_point = station(1, 39.0, -98.75)  # within the first segment
    candidates_baseline = SERVICE.select_candidates(STRAIGHT_ROUTE, [near_quarter_point], exact)
    candidates_inflated = SERVICE.select_candidates(
        STRAIGHT_ROUTE, [near_quarter_point], first_segment_only_inflated
    )

    # travel_distance for a station on segment 1 is unaffected by segment 2
    # being wildly different, since segment 1's own distance is unchanged
    assert candidates_baseline[0].travel_distance_miles == pytest.approx(
        candidates_inflated[0].travel_distance_miles, rel=1e-3
    )


# ---- OSRM distance stays authoritative / no scale factor ------------------


def test_projected_length_discrepancy_is_diagnostic_only():
    projected_length = SERVICE._project_route(STRAIGHT_ROUTE).length_miles

    discrepancy = SERVICE.projected_length_discrepancy_miles(
        STRAIGHT_ROUTE, route_distance_miles=projected_length + 12.5
    )

    assert discrepancy == pytest.approx(12.5, abs=0.01)
