"""End-to-end API tests: real URL routing, real RoutePlanService wiring
(via the real factory), real DB-backed caches, real FuelStation rows — the
ONLY thing mocked is the raw HTTP layer (`requests.get` for both Nominatim
and OSRM), via the same `make_response` helper used in the Phase 4 provider
tests. No live Nominatim/OSRM call is ever made.
"""
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from rest_framework.test import APIClient

from fuel.models import FuelStation
from routes.models import GeocodeCacheEntry, RouteCacheEntry
from routes.tests.geometry_helpers import point_on_route_at_fraction
from routes.tests.helpers import make_response

URL = "/api/v1/routes/plan"
ADDRESS_PAYLOAD = {"start": "Chicago, IL", "destination": "Detroit, MI"}

CHICAGO = {"lat": "41.8781", "lon": "-87.6298"}
DETROIT = {"lat": "42.3314", "lon": "-83.0458"}
STRAIGHT_COORDINATES = [
    [float(CHICAGO["lon"]), float(CHICAGO["lat"])],
    [float(DETROIT["lon"]), float(DETROIT["lat"])],
]


def nominatim_body(lat, lon):
    return [{"lat": lat, "lon": lon, "display_name": "test"}]


def osrm_body(distance_miles, coordinates=None, segment_distances_miles=None):
    coordinates = coordinates or STRAIGHT_COORDINATES
    distance_meters = distance_miles * 1609.344
    if segment_distances_miles is None:
        segment_distances_miles = [distance_miles] * (len(coordinates) - 1)
    segment_meters = [d * 1609.344 for d in segment_distances_miles]
    return {
        "code": "Ok",
        "routes": [
            {
                "distance": distance_meters,
                "duration": 12345.0,
                "geometry": {"type": "LineString", "coordinates": coordinates},
                "legs": [{"annotation": {"distance": segment_meters}}],
            }
        ],
    }


def make_station(opis_id, lat, lon, price, normalized_key=None):
    return FuelStation.objects.create(
        opis_id=opis_id,
        normalized_key=normalized_key or f"key-{opis_id}",
        name=f"Station {opis_id}",
        address="123 Main St",
        city="Testville",
        state="IL",
        country="US",
        latitude=lat,
        longitude=lon,
        effective_price_per_gallon=Decimal(str(price)),
    )


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def mocked_http():
    """`routes.services.geocoding` and `routes.services.routing` both do
    `import requests`, so they share the exact same module object —
    patching `requests.get` via two different module paths in the same
    `with` statement makes the second patch silently clobber the first
    (both end up resolving to the same attribute). Patch the single shared
    `requests.get` once, with a dispatcher that routes to whichever mock
    matches the URL (Nominatim's `/search` vs OSRM's `/route/v1/...`), so
    `mock_geocode`/`mock_route` keep behaving like independent mocks with
    their own `side_effect`/`return_value`/`call_count`.
    """
    mock_geocode = MagicMock()
    mock_route = MagicMock()

    def dispatch(url, *args, **kwargs):
        if "/search" in url:
            return mock_geocode(url, *args, **kwargs)
        if "/route/v1/" in url:
            return mock_route(url, *args, **kwargs)
        raise AssertionError(f"mocked_http: unexpected URL {url!r}")

    with patch("requests.get", side_effect=dispatch):
        yield mock_geocode, mock_route


# ---- 1-3: request shape variants ------------------------------------------


@pytest.mark.django_db
def test_valid_address_address_request(client, mocked_http):
    mock_geocode, mock_route = mocked_http
    mock_geocode.side_effect = [
        make_response(200, json_body=nominatim_body(CHICAGO["lat"], CHICAGO["lon"])),
        make_response(200, json_body=nominatim_body(DETROIT["lat"], DETROIT["lon"])),
    ]
    mock_route.return_value = make_response(200, json_body=osrm_body(300.0))

    response = client.post(URL, ADDRESS_PAYLOAD, format="json")

    assert response.status_code == 200
    assert mock_geocode.call_count == 2
    assert mock_route.call_count == 1


@pytest.mark.django_db
def test_valid_coordinate_coordinate_request(client, mocked_http):
    mock_geocode, mock_route = mocked_http
    mock_route.return_value = make_response(200, json_body=osrm_body(300.0))

    payload = {
        "start": {"latitude": 41.8781, "longitude": -87.6298},
        "destination": {"latitude": 42.3314, "longitude": -83.0458},
    }
    response = client.post(URL, payload, format="json")

    assert response.status_code == 200
    assert mock_geocode.call_count == 0  # item 5/19: zero geocoding calls
    assert mock_route.call_count == 1


@pytest.mark.django_db
def test_mixed_address_coordinate_request(client, mocked_http):
    mock_geocode, mock_route = mocked_http
    detroit_body = nominatim_body(DETROIT["lat"], DETROIT["lon"])
    mock_geocode.return_value = make_response(200, json_body=detroit_body)
    mock_route.return_value = make_response(200, json_body=osrm_body(300.0))

    payload = {
        "start": {"latitude": 41.8781, "longitude": -87.6298},
        "destination": "Detroit, MI",
    }
    response = client.post(URL, payload, format="json")

    assert response.status_code == 200
    assert mock_geocode.call_count == 1
    assert mock_route.call_count == 1


# ---- 4-6: validation -------------------------------------------------------


@pytest.mark.django_db
def test_missing_start_returns_400(client):
    response = client.post(URL, {"destination": "Detroit, MI"}, format="json")
    assert response.status_code == 400
    assert "start" in response.json()


@pytest.mark.django_db
def test_missing_destination_returns_400(client):
    response = client.post(URL, {"start": "Chicago, IL"}, format="json")
    assert response.status_code == 400
    assert "destination" in response.json()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "bad_payload",
    [
        {"start": "", "destination": "Detroit, MI"},
        {"start": {"latitude": 91.0, "longitude": -87.6}, "destination": "Detroit, MI"},
        {"start": {"latitude": 41.8, "longitude": -200.0}, "destination": "Detroit, MI"},
        {"start": {"latitude": 41.8}, "destination": "Detroit, MI"},
        {"start": {"latitude": "not-a-number", "longitude": -87.6}, "destination": "Detroit, MI"},
        {"start": [41.8, -87.6], "destination": "Detroit, MI"},
        {
            "start": {"latitude": 41.8, "longitude": -87.6, "extra": "field"},
            "destination": "Detroit, MI",
        },
    ],
)
def test_invalid_start_shapes_return_400(client, bad_payload):
    response = client.post(URL, bad_payload, format="json")
    assert response.status_code == 400


# ---- 7-10: provider / resolution failures ---------------------------------


@pytest.mark.django_db
def test_geocoding_not_found_returns_422(client, mocked_http):
    mock_geocode, _ = mocked_http
    mock_geocode.return_value = make_response(200, json_body=[])

    payload = {"start": "Nowhereville", "destination": "Detroit, MI"}
    response = client.post(URL, payload, format="json")

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "location_not_found"
    assert body["field"] == "start"


@pytest.mark.django_db
def test_geocoding_provider_failure_returns_502(client, mocked_http):
    mock_geocode, _ = mocked_http
    mock_geocode.return_value = make_response(500, json_body={"error": "down"})

    response = client.post(URL, ADDRESS_PAYLOAD, format="json")

    assert response.status_code == 502
    body = response.json()
    assert body["code"] == "geocoding_provider_error"
    assert "requests" not in str(body).lower()
    assert "traceback" not in str(body).lower()


@pytest.mark.django_db
def test_routing_provider_failure_returns_502(client, mocked_http):
    mock_geocode, mock_route = mocked_http
    mock_geocode.side_effect = [
        make_response(200, json_body=nominatim_body(CHICAGO["lat"], CHICAGO["lon"])),
        make_response(200, json_body=nominatim_body(DETROIT["lat"], DETROIT["lon"])),
    ]
    mock_route.return_value = make_response(503, json_body={"error": "down"})

    response = client.post(URL, ADDRESS_PAYLOAD, format="json")

    assert response.status_code == 502
    assert response.json()["code"] == "routing_provider_error"


@pytest.mark.django_db
def test_route_not_found_returns_422(client, mocked_http):
    mock_geocode, mock_route = mocked_http
    mock_geocode.side_effect = [
        make_response(200, json_body=nominatim_body(CHICAGO["lat"], CHICAGO["lon"])),
        make_response(200, json_body=nominatim_body(DETROIT["lat"], DETROIT["lon"])),
    ]
    mock_route.return_value = make_response(200, json_body={"code": "NoRoute", "routes": []})

    response = client.post(URL, ADDRESS_PAYLOAD, format="json")

    assert response.status_code == 422
    assert response.json()["code"] == "route_not_found"


# ---- 11-14: fuel plan outcomes ---------------------------------------------


@pytest.mark.django_db
def test_infeasible_fuel_route_returns_422(client, mocked_http):
    _, mock_route = mocked_http
    mock_route.return_value = make_response(200, json_body=osrm_body(1000.0))  # no stations

    payload = {
        "start": {"latitude": 41.8781, "longitude": -87.6298},
        "destination": {"latitude": 42.3314, "longitude": -83.0458},
    }
    response = client.post(URL, payload, format="json")

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "route_infeasible"
    assert "detail" in body


@pytest.mark.django_db
def test_successful_route_with_no_fuel_stops(client, mocked_http):
    _, mock_route = mocked_http
    mock_route.return_value = make_response(200, json_body=osrm_body(300.0))  # within max_range

    payload = {
        "start": {"latitude": 41.8781, "longitude": -87.6298},
        "destination": {"latitude": 42.3314, "longitude": -83.0458},
    }
    response = client.post(URL, payload, format="json")

    assert response.status_code == 200
    body = response.json()
    fuel = body["fuel"]
    # B2 semantics: a route the starting full tank alone covers must never
    # be charged for — fuel_consumed_gallons is nonzero (fuel was burned),
    # but total_gallons_purchased and total_cost stay at zero because
    # nothing was actually bought. total_cost must never be computed as
    # fuel_consumed_gallons * price, which would incorrectly bill for fuel
    # that was already in the tank at trip start.
    assert fuel["feasible"] is True
    assert fuel["starting_fuel_gallons"] == 50.0
    assert fuel["fuel_consumed_gallons"] == pytest.approx(300.0 / 10.0)
    assert fuel["total_gallons_purchased"] == 0
    assert fuel["total_cost"] == "0.00"
    assert body["stops"] == []


@pytest.mark.django_db
def test_successful_route_requiring_fuel(client, mocked_http):
    _, mock_route = mocked_http
    mock_route.return_value = make_response(200, json_body=osrm_body(600.0))  # exceeds max_range

    lat, lon = point_on_route_at_fraction(STRAIGHT_COORDINATES, 0.5)
    make_station(1, lat, lon, "2.75")

    payload = {
        "start": {"latitude": 41.8781, "longitude": -87.6298},
        "destination": {"latitude": 42.3314, "longitude": -83.0458},
    }
    response = client.post(URL, payload, format="json")

    assert response.status_code == 200
    body = response.json()
    fuel = body["fuel"]
    assert fuel["feasible"] is True
    assert len(body["stops"]) == 1
    assert body["stops"][0]["station_id"] is not None
    expected_price = Decimal("2.75").quantize(Decimal("0.00000001"))
    assert Decimal(body["stops"][0]["price_per_gallon"]) == expected_price

    # B2 semantics: total_cost is exactly the purchase (price * gallons
    # bought), not fuel_consumed_gallons * price — the starting tank's
    # portion of the trip is never charged for.
    assert fuel["starting_fuel_gallons"] == 50.0
    assert fuel["fuel_consumed_gallons"] == pytest.approx(600.0 / 10.0)
    assert fuel["total_gallons_purchased"] == pytest.approx(
        float(body["stops"][0]["fuel_purchased_gallons"])
    )
    assert fuel["total_gallons_purchased"] < fuel["fuel_consumed_gallons"]
    expected_cost = float(expected_price) * body["stops"][0]["fuel_purchased_gallons"]
    assert float(fuel["total_cost"]) == pytest.approx(expected_cost, abs=0.01)

    # station display details needed to render the stop on a map, not just
    # its bare id
    stop = body["stops"][0]
    assert stop["station_name"] == "Station 1"
    assert stop["address"] == "123 Main St"
    assert stop["city"] == "Testville"
    assert stop["state"] == "IL"
    assert stop["coordinates"]["latitude"] == pytest.approx(lat)
    assert stop["coordinates"]["longitude"] == pytest.approx(lon)


@pytest.mark.django_db
def test_multiple_fuel_stops(client, mocked_http):
    _, mock_route = mocked_http
    # A longer route needing two stops, each cheaper than the one before.
    coordinates = STRAIGHT_COORDINATES
    mock_route.return_value = make_response(
        200, json_body=osrm_body(1400.0, coordinates=coordinates, segment_distances_miles=[1400.0])
    )

    lat1, lon1 = point_on_route_at_fraction(coordinates, 0.3)  # travel_distance ~420mi
    lat2, lon2 = point_on_route_at_fraction(coordinates, 0.65)  # travel_distance ~910mi
    # gaps: 0->420 (420), 420->910 (490), 910->1400 (490) — all <= max_range (500)
    make_station(1, lat1, lon1, "3.00")
    make_station(2, lat2, lon2, "2.00")

    payload = {
        "start": {"latitude": 41.8781, "longitude": -87.6298},
        "destination": {"latitude": 42.3314, "longitude": -83.0458},
    }
    response = client.post(URL, payload, format="json")

    assert response.status_code == 200
    body = response.json()
    fuel = body["fuel"]
    assert fuel["feasible"] is True
    assert len(body["stops"]) >= 1  # at minimum the cheaper station is used

    # B2 semantics, multi-stop case: total_cost must equal the sum of each
    # individual stop's own fuel_cost — never the total trip consumption
    # priced at any single station's rate, and never double-counting the
    # starting tank.
    stop_cost_sum = sum(float(s["fuel_cost"]) for s in body["stops"])
    assert float(fuel["total_cost"]) == pytest.approx(stop_cost_sum, abs=0.01)
    stop_gallons_sum = sum(s["fuel_purchased_gallons"] for s in body["stops"])
    assert fuel["total_gallons_purchased"] == pytest.approx(stop_gallons_sum)
    assert fuel["fuel_consumed_gallons"] == pytest.approx(1400.0 / 10.0)


# ---- 15-16: cache behavior / provider call counts --------------------------


@pytest.mark.django_db
def test_cache_hit_avoids_second_provider_call(client, mocked_http):
    mock_geocode, mock_route = mocked_http
    mock_geocode.side_effect = [
        make_response(200, json_body=nominatim_body(CHICAGO["lat"], CHICAGO["lon"])),
        make_response(200, json_body=nominatim_body(DETROIT["lat"], DETROIT["lon"])),
    ]
    mock_route.return_value = make_response(200, json_body=osrm_body(300.0))

    payload = {"start": "Chicago, IL", "destination": "Detroit, MI"}
    first = client.post(URL, payload, format="json")
    second = client.post(URL, payload, format="json")

    assert first.status_code == 200
    assert second.status_code == 200
    # exactly one geocode call PER location, and one route call — repeated
    # on the second, identical request — must not trigger NEW provider calls
    assert mock_geocode.call_count == 2
    assert mock_route.call_count == 1
    assert GeocodeCacheEntry.objects.count() == 2
    assert RouteCacheEntry.objects.count() == 1


@pytest.mark.django_db
def test_coordinate_request_cache_hit_avoids_second_routing_call(client, mocked_http):
    _, mock_route = mocked_http
    mock_route.return_value = make_response(200, json_body=osrm_body(300.0))

    payload = {
        "start": {"latitude": 41.8781, "longitude": -87.6298},
        "destination": {"latitude": 42.3314, "longitude": -83.0458},
    }
    client.post(URL, payload, format="json")
    client.post(URL, payload, format="json")

    assert mock_route.call_count == 1
    assert RouteCacheEntry.objects.count() == 1


# ---- 17-20: serialization / status codes / isolation / geometry -----------


@pytest.mark.django_db
def test_decimal_money_serialized_as_strings_not_floats(client, mocked_http):
    _, mock_route = mocked_http
    mock_route.return_value = make_response(200, json_body=osrm_body(600.0))
    lat, lon = point_on_route_at_fraction(STRAIGHT_COORDINATES, 0.5)
    make_station(1, lat, lon, "2.75")

    payload = {
        "start": {"latitude": 41.8781, "longitude": -87.6298},
        "destination": {"latitude": 42.3314, "longitude": -83.0458},
    }
    response = client.post(URL, payload, format="json")
    body = response.json()

    assert isinstance(body["fuel"]["total_cost"], str)
    assert isinstance(body["stops"][0]["price_per_gallon"], str)
    assert isinstance(body["stops"][0]["fuel_cost"], str)


@pytest.mark.django_db
def test_route_geometry_returned_correctly(client, mocked_http):
    _, mock_route = mocked_http
    mock_route.return_value = make_response(200, json_body=osrm_body(300.0))

    payload = {
        "start": {"latitude": 41.8781, "longitude": -87.6298},
        "destination": {"latitude": 42.3314, "longitude": -83.0458},
    }
    response = client.post(URL, payload, format="json")
    body = response.json()

    assert body["route"]["geometry"]["type"] == "LineString"
    assert body["route"]["geometry"]["coordinates"] == STRAIGHT_COORDINATES
    assert body["route"]["distance_miles"] == pytest.approx(300.0)


@pytest.mark.django_db
def test_no_raw_provider_exception_text_in_any_error_response(client, mocked_http):
    import requests

    mock_geocode, _ = mocked_http
    mock_geocode.side_effect = requests.exceptions.ConnectionError(
        "Connection refused: https://internal-nominatim.example.test/search?q=secret"
    )

    response = client.post(URL, ADDRESS_PAYLOAD, format="json")

    assert response.status_code == 502
    body_text = response.content.decode().lower()
    assert "requests.exceptions" not in body_text
    assert "internal-nominatim.example.test" not in body_text
    assert "traceback" not in body_text


# ---- 21-22: geospatial + optimizer data reaches correctly ------------------


@pytest.mark.django_db
def test_segment_distance_information_reaches_geospatial_selection(client, mocked_http):
    _, mock_route = mocked_http
    # route.distance_miles authoritative total of 600, single segment
    mock_route.return_value = make_response(200, json_body=osrm_body(600.0))
    lat, lon = point_on_route_at_fraction(STRAIGHT_COORDINATES, 0.5)
    make_station(1, lat, lon, "2.75")

    payload = {
        "start": {"latitude": 41.8781, "longitude": -87.6298},
        "destination": {"latitude": 42.3314, "longitude": -83.0458},
    }
    response = client.post(URL, payload, format="json")
    body = response.json()

    # the stop's route_position_miles must reflect the authoritative
    # travel distance (~half of 600 = 300), not an uncalibrated raw
    # projected-geometry figure
    assert response.status_code == 200
    assert body["stops"][0]["route_position_miles"] == pytest.approx(300.0, rel=0.05)


@pytest.mark.django_db
def test_optimizer_receives_travel_distance_via_full_stack(client, mocked_http):
    # A deliberately mismatched per-segment distance (second segment
    # reported much larger than the midpoint-based projected geometry would
    # suggest) proves the full stack — not just the unit-level service test
    # — carries travel_distance_miles through to the optimizer correctly.
    _, mock_route = mocked_http
    midpoint_lat, midpoint_lon = point_on_route_at_fraction(STRAIGHT_COORDINATES, 0.5)
    coordinates = [STRAIGHT_COORDINATES[0], [midpoint_lon, midpoint_lat], STRAIGHT_COORDINATES[1]]
    # segments: 0->300 (reachable from start), 300->750 (reachable from the
    # station) — both <= max_range (500); total authoritative distance 750
    mock_route.return_value = make_response(
        200,
        json_body=osrm_body(
            750.0, coordinates=coordinates, segment_distances_miles=[300.0, 450.0]
        ),
    )
    make_station(1, midpoint_lat, midpoint_lon, "2.0")

    payload = {
        "start": {"latitude": 41.8781, "longitude": -87.6298},
        "destination": {"latitude": 42.3314, "longitude": -83.0458},
    }
    response = client.post(URL, payload, format="json")
    body = response.json()

    assert response.status_code == 200
    assert body["fuel"]["fuel_consumed_gallons"] == pytest.approx(750.0 / 10.0)
    # the station (at the midpoint vertex) should be positioned at the
    # authoritative 300mi mark, not an uncalibrated geometric midpoint of 750
    assert body["stops"][0]["route_position_miles"] == pytest.approx(300.0, rel=0.05)


# ---------------------------------------------------------------------
# Regression: production reported a real request crashing with a raw
# Django DEBUG traceback (InvalidOptimizerInputError was never caught by
# the view). Verifies the API now returns a clean, structured 500 instead.
# ---------------------------------------------------------------------


@pytest.mark.django_db
def test_optimizer_input_error_returns_clean_500_not_a_traceback(client):
    from routes.domain.fuel_optimizer import InvalidOptimizerInputError

    mock_service = MagicMock()
    mock_service.plan.side_effect = InvalidOptimizerInputError(
        "station 380 travel_distance_miles (544.7709855445179) exceeds "
        "route_distance_miles (544.7709750059652)"
    )

    with patch("routes.api.views.build_route_plan_service", return_value=mock_service):
        response = client.post(URL, ADDRESS_PAYLOAD, format="json")

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "internal_error"
    assert "detail" in body

    # the raw exception message/internal details must never reach the client
    body_text = response.content.decode().lower()
    assert "invalidoptimizerinputerror" not in body_text
    assert "544.77" not in body_text
    assert "traceback" not in body_text
    assert "<html" not in body_text  # not Django's DEBUG HTML page


@pytest.mark.django_db
def test_unhandled_exception_with_debug_false_does_not_leak_traceback(settings):
    """A production-like config guard: even an exception we didn't
    anticipate must not render Django's DEBUG HTML traceback page when
    DEBUG=False (the config real deployments should run with).

    Uses raise_request_exception=False: Django's test client re-raises
    server exceptions by default as a debugging convenience, which would
    mask the actual HTTP response a real (production, DEBUG=False) client
    receives — exactly the behavior this test needs to observe.
    """
    settings.DEBUG = False
    no_raise_client = APIClient(raise_request_exception=False)

    mock_service = MagicMock()
    mock_service.plan.side_effect = RuntimeError("completely unexpected failure")

    with patch("routes.api.views.build_route_plan_service", return_value=mock_service):
        response = no_raise_client.post(URL, ADDRESS_PAYLOAD, format="json")

    assert response.status_code == 500
    body_text = response.content.decode().lower()
    assert "runtimeerror" not in body_text
    assert "traceback" not in body_text
    assert "<html" not in body_text or "server error" in body_text
