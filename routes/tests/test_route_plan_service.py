from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from routes.domain.types import Coordinates, Route, StationLocation, VehicleProfile
from routes.services.exceptions import LocationNotFoundError, RouteNotFoundError
from routes.services.geospatial import RouteGeospatialService
from routes.services.route_plan_exceptions import (
    RoutePlanLocationNotFoundError,
    RoutePlanLocationOutOfScopeError,
)
from routes.services.route_plan_service import RoutePlanService
from routes.tests.geometry_helpers import point_on_route_at_fraction

CHICAGO = Coordinates(latitude=41.8781, longitude=-87.6298)
DETROIT = Coordinates(latitude=42.3314, longitude=-83.0458)
STRAIGHT_GEOMETRY = {
    "type": "LineString",
    "coordinates": [[CHICAGO.longitude, CHICAGO.latitude], [DETROIT.longitude, DETROIT.latitude]],
}


def make_service(
    geocoding_service=None,
    routing_service=None,
    stations=None,
    vehicle=None,
):
    geocoding_service = geocoding_service or MagicMock()
    routing_service = routing_service or MagicMock()
    station_repository = lambda: stations if stations is not None else []  # noqa: E731
    geospatial_service = RouteGeospatialService(corridor_miles=5.0)
    vehicle = vehicle or VehicleProfile(
        mpg=10.0, max_range_miles=500.0, tank_capacity_gallons=50.0, starting_fuel_gallons=50.0
    )
    return RoutePlanService(
        geocoding_service=geocoding_service,
        routing_service=routing_service,
        station_repository=station_repository,
        geospatial_service=geospatial_service,
        vehicle=vehicle,
    )


def route_with_distance(distance_miles, coordinates=None, segment_distances=None):
    coordinates = coordinates or STRAIGHT_GEOMETRY["coordinates"]
    if segment_distances is None:
        segment_distances = [distance_miles] * (len(coordinates) - 1)
    return Route(
        distance_miles=distance_miles,
        duration_minutes=distance_miles / 60.0 * 60,
        geometry={"type": "LineString", "coordinates": coordinates},
        segment_distances_miles=segment_distances,
    )


def station_at_fraction(station_id, fraction, price, coordinates=None):
    coordinates = coordinates or STRAIGHT_GEOMETRY["coordinates"]
    lat, lon = point_on_route_at_fraction(coordinates, fraction)
    return StationLocation(
        station_id=station_id, latitude=lat, longitude=lon, price_per_gallon=Decimal(str(price))
    )


def test_coordinate_inputs_never_geocode():
    geocoding_service = MagicMock()
    routing_service = MagicMock()
    routing_service.route.return_value = route_with_distance(300.0)
    service = make_service(geocoding_service=geocoding_service, routing_service=routing_service)

    result = service.plan(CHICAGO, DETROIT)

    assert geocoding_service.geocode.call_count == 0
    assert routing_service.route.call_count == 1
    assert result.fuel_plan.feasible


def test_string_inputs_geocode_each_independently():
    geocoding_service = MagicMock()
    geocoding_service.geocode.side_effect = [CHICAGO, DETROIT]
    routing_service = MagicMock()
    routing_service.route.return_value = route_with_distance(300.0)
    service = make_service(geocoding_service=geocoding_service, routing_service=routing_service)

    service.plan("Chicago, IL", "Detroit, MI")

    assert geocoding_service.geocode.call_count == 2
    routing_service.route.assert_called_once_with(CHICAGO, DETROIT)


def test_mixed_string_and_coordinate_input():
    geocoding_service = MagicMock()
    geocoding_service.geocode.return_value = DETROIT
    routing_service = MagicMock()
    routing_service.route.return_value = route_with_distance(300.0)
    service = make_service(geocoding_service=geocoding_service, routing_service=routing_service)

    service.plan(CHICAGO, "Detroit, MI")

    assert geocoding_service.geocode.call_count == 1
    routing_service.route.assert_called_once_with(CHICAGO, DETROIT)


def test_geocoding_not_found_raises_route_plan_location_not_found_with_field():
    geocoding_service = MagicMock()
    geocoding_service.geocode.side_effect = LocationNotFoundError("no match")
    service = make_service(geocoding_service=geocoding_service)

    with pytest.raises(RoutePlanLocationNotFoundError) as exc_info:
        service.plan("Nowhereville", DETROIT)
    assert exc_info.value.field == "start"


def test_geocoding_not_found_on_destination_reports_destination_field():
    geocoding_service = MagicMock()
    geocoding_service.geocode.side_effect = LocationNotFoundError("no match")
    service = make_service(geocoding_service=geocoding_service)

    with pytest.raises(RoutePlanLocationNotFoundError) as exc_info:
        service.plan(CHICAGO, "Nowhereville")
    assert exc_info.value.field == "destination"


def test_location_outside_usa_scope_raises_out_of_scope_error():
    paris = Coordinates(latitude=48.8566, longitude=2.3522)
    service = make_service()

    with pytest.raises(RoutePlanLocationOutOfScopeError) as exc_info:
        service.plan(paris, DETROIT)
    assert exc_info.value.field == "start"


def test_toronto_is_rejected_despite_falling_inside_the_old_lat_lon_rectangle():
    # Toronto (43.65, -79.38) falls INSIDE the old rectangular bounding box
    # (lat 24-50, lon -125..-66) — a real validation gap the point-in-
    # polygon check (routes/services/geography.py) closes.
    toronto = Coordinates(latitude=43.6532, longitude=-79.3832)
    service = make_service()

    with pytest.raises(RoutePlanLocationOutOfScopeError):
        service.plan(toronto, DETROIT)


def test_monterrey_is_rejected_despite_falling_inside_the_old_lat_lon_rectangle():
    monterrey = Coordinates(latitude=25.6866, longitude=-100.3161)
    service = make_service()

    with pytest.raises(RoutePlanLocationOutOfScopeError):
        service.plan(monterrey, DETROIT)


def test_hawaii_coordinates_are_accepted_as_within_usa_scope():
    # The old CONUS-only rectangle rejected Hawaii/Alaska outright; the
    # real USA boundary check accepts them (station-level coverage there
    # remains a separate, honestly-documented data limitation, not a
    # validation one).
    honolulu = Coordinates(latitude=21.3099, longitude=-157.8581)
    routing_service = MagicMock()
    routing_service.route.return_value = route_with_distance(
        50.0, coordinates=[[honolulu.longitude, honolulu.latitude], [-157.85, 21.31]]
    )
    service = make_service(routing_service=routing_service)

    result = service.plan(honolulu, Coordinates(latitude=21.31, longitude=-157.85))

    assert result.fuel_plan.feasible


def test_route_not_found_propagates_uncaught():
    routing_service = MagicMock()
    routing_service.route.side_effect = RouteNotFoundError("no route")
    service = make_service(routing_service=routing_service)

    with pytest.raises(RouteNotFoundError):
        service.plan(CHICAGO, DETROIT)


def test_feasible_plan_with_no_stops_needed():
    routing_service = MagicMock()
    routing_service.route.return_value = route_with_distance(300.0)  # within max_range (500)
    service = make_service(routing_service=routing_service, stations=[])

    result = service.plan(CHICAGO, DETROIT)

    assert result.fuel_plan.feasible
    assert result.fuel_plan.stops == []


def test_feasible_plan_requiring_one_stop():
    routing_service = MagicMock()
    routing_service.route.return_value = route_with_distance(600.0)  # exceeds max_range (500)
    station = station_at_fraction(1, 0.5, "2.50")
    service = make_service(routing_service=routing_service, stations=[station])

    result = service.plan(CHICAGO, DETROIT)

    assert result.fuel_plan.feasible
    assert len(result.fuel_plan.stops) == 1
    assert result.fuel_plan.stops[0].station_id == 1


def test_infeasible_plan_reports_feasible_false_not_an_exception():
    routing_service = MagicMock()
    routing_service.route.return_value = route_with_distance(1000.0)  # no stations to bridge it
    service = make_service(routing_service=routing_service, stations=[])

    result = service.plan(CHICAGO, DETROIT)

    assert result.fuel_plan.feasible is False
    assert result.fuel_plan.infeasibility_reason is not None


def test_station_data_is_not_mutated_by_the_request():
    routing_service = MagicMock()
    routing_service.route.return_value = route_with_distance(600.0)
    station = station_at_fraction(1, 0.5, "2.50")
    service = make_service(routing_service=routing_service, stations=[station])

    service.plan(CHICAGO, DETROIT)

    assert station.price_per_gallon == Decimal("2.50")
    assert station.latitude is not None and station.longitude is not None


# ---- Phase 6.1 regression at the integration boundary ---------------------


def test_fuel_plan_follows_travel_distance_not_route_position_at_integration_boundary():
    # Three-point geometry (two segments) with the SECOND segment reported
    # by "OSRM" as much longer than the projected polyline's own length for
    # that segment — simulating exactly the discrepancy Phase 6.1 fixed.
    midpoint_lat, midpoint_lon = point_on_route_at_fraction(STRAIGHT_GEOMETRY["coordinates"], 0.5)
    coordinates = [
        [CHICAGO.longitude, CHICAGO.latitude],
        [midpoint_lon, midpoint_lat],
        [DETROIT.longitude, DETROIT.latitude],
    ]
    # exact (no-discrepancy) segment lengths, then inflate the second
    exact_service = RouteGeospatialService()
    projected = exact_service._project_route({"type": "LineString", "coordinates": coordinates})
    vd = projected.cumulative_vertex_distances_meters
    exact_segments_miles = [(vd[i + 1] - vd[i]) / 1609.344 for i in range(len(vd) - 1)]

    inflated_segments = [exact_segments_miles[0], exact_segments_miles[1] * 3]
    authoritative_total = sum(inflated_segments)

    routing_service = MagicMock()
    routing_service.route.return_value = Route(
        distance_miles=authoritative_total,
        duration_minutes=authoritative_total / 60.0 * 60,
        geometry={"type": "LineString", "coordinates": coordinates},
        segment_distances_miles=inflated_segments,
    )

    # station sitting exactly at the midpoint vertex (end of segment 1)
    station = StationLocation(
        station_id=1,
        latitude=coordinates[1][1],
        longitude=coordinates[1][0],
        price_per_gallon=Decimal("2.0"),
    )
    service = make_service(routing_service=routing_service, stations=[station])

    result = service.plan(CHICAGO, DETROIT)

    assert result.fuel_plan.feasible
    # fuel_consumed must be based on route.distance_miles (authoritative),
    # not on the projected polyline's own (shorter) total length
    assert result.fuel_plan.fuel_consumed_gallons == pytest.approx(
        authoritative_total / service._vehicle.mpg
    )


def test_stops_are_decorated_with_station_display_details():
    routing_service = MagicMock()
    routing_service.route.return_value = route_with_distance(600.0)

    lat, lon = point_on_route_at_fraction(STRAIGHT_GEOMETRY["coordinates"], 0.5)
    station = StationLocation(
        station_id=42,
        latitude=lat,
        longitude=lon,
        price_per_gallon=Decimal("2.50"),
        name="Flying J #42",
        address="I-90 EXIT 12",
        city="Gary",
        state="IN",
    )
    service = make_service(routing_service=routing_service, stations=[station])

    result = service.plan(CHICAGO, DETROIT)

    assert result.fuel_plan.feasible
    assert len(result.fuel_plan.stops) == 1
    stop = result.fuel_plan.stops[0]
    assert stop.station_id == 42
    assert stop.station_name == "Flying J #42"
    assert stop.address == "I-90 EXIT 12"
    assert stop.city == "Gary"
    assert stop.state == "IN"
    assert stop.latitude == pytest.approx(lat)
    assert stop.longitude == pytest.approx(lon)
