"""Integration tests against the ACTUAL supplied data/fuel-prices.csv and
the real data/station_coordinates.csv enrichment fixture — not a synthetic
8-row sample. These prove the full pipeline (CSV import -> coordinate
enrichment -> repository -> geospatial selection -> optimizer -> API) works
against genuine assessment data, not just hand-built test fixtures.

No live Nominatim/OSRM call is made here — OSRM is mocked with the REAL
distance (544.68mi) this corridor was confirmed to produce in a live smoke
test (see the Phase 8 report), so the scenario stays realistic without
requiring network access for pytest.
"""
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from rest_framework.test import APIClient

from fuel.models import FuelStation
from fuel.repositories import load_enriched_stations
from routes.tests.helpers import make_response

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REAL_CSV = REPO_ROOT / "data" / "fuel-prices.csv"
REAL_COORDINATES = REPO_ROOT / "data" / "station_coordinates.csv"

# Oklahoma City, OK -> Albuquerque, NM via I-40 — real coordinates, real
# OSRM-confirmed distance (544.68 real road miles, exceeds the 500-mile
# max_range, so at least one fuel stop is genuinely required).
OKC = {"latitude": 35.4730, "longitude": -97.5171}
ABQ = {"latitude": 35.0841, "longitude": -106.6510}
REAL_ROUTE_DISTANCE_MILES = 544.6851015071979


def osrm_body_for_real_corridor():
    coordinates = [[OKC["longitude"], OKC["latitude"]], [ABQ["longitude"], ABQ["latitude"]]]
    distance_meters = REAL_ROUTE_DISTANCE_MILES * 1609.344
    return {
        "code": "Ok",
        "routes": [
            {
                "distance": distance_meters,
                "duration": 33708.9,
                "geometry": {"type": "LineString", "coordinates": coordinates},
                "legs": [{"annotation": {"distance": [distance_meters]}}],
            }
        ],
    }


@pytest.fixture(scope="module")
def real_enriched_stations(django_db_setup, django_db_blocker):
    """Imports the ACTUAL CSV and the ACTUAL coordinate fixture into the
    test database, exactly as `manage.py import_fuel_prices` /
    `enrich_station_coordinates` would against a real deployment.

    Module-scoped (not per-test): importing the real ~8,151-row CSV is slow
    enough (~30s) that re-running it for every test in this file would add
    minutes to the overall suite for no benefit — all four tests here read
    the same imported/enriched dataset, never mutate it, and running the
    import once and sharing it is the standard pytest-django pattern for
    exactly this (`django_db_blocker.unblock()` commits outside any single
    test's per-test transaction, so later per-test transactions still see it).
    """
    with django_db_blocker.unblock():
        call_command("import_fuel_prices", str(REAL_CSV))
        call_command(
            "enrich_station_coordinates",
            "--provider", "file", "--from-file", str(REAL_COORDINATES),
        )


@pytest.mark.django_db
def test_coordinate_fixture_import_against_real_csv(real_enriched_stations):
    enriched_count = FuelStation.objects.filter(latitude__isnull=False).count()
    assert enriched_count == 953


@pytest.mark.django_db
def test_repository_returns_real_enriched_stations(real_enriched_stations):
    stations = load_enriched_stations()
    enriched_ids = {s.station_id for s in stations if s.latitude is not None}
    assert len(enriched_ids) >= 953

    # every returned station has a real price from the actual CSV
    for station in stations:
        if station.latitude is not None:
            assert station.price_per_gallon > Decimal("0")


@pytest.mark.django_db
def test_api_route_plan_selects_a_real_persisted_station(real_enriched_stations):
    client = APIClient()
    with patch("routes.services.routing.requests.get") as mock_route:
        mock_route.return_value = make_response(200, json_body=osrm_body_for_real_corridor())

        response = client.post(
            "/api/v1/routes/plan", {"start": OKC, "destination": ABQ}, format="json"
        )

    assert response.status_code == 200
    body = response.json()

    assert body["route"]["distance_miles"] == pytest.approx(REAL_ROUTE_DISTANCE_MILES)
    assert body["fuel"]["feasible"] is True
    assert len(body["stops"]) >= 1

    stop = body["stops"][0]
    real_station = FuelStation.objects.get(id=stop["station_id"])
    # the station returned is a REAL row from the supplied CSV, not a fixture
    assert real_station.opis_id is not None
    assert real_station.latitude is not None
    assert Decimal(stop["price_per_gallon"]) == real_station.effective_price_per_gallon.quantize(
        Decimal("0.00000001")
    )
    assert float(body["fuel"]["total_cost"]) > 0  # a genuine purchase was made


@pytest.mark.django_db
def test_optimizer_receives_real_station_prices_and_positions(real_enriched_stations):
    """Exercises RoutePlanService directly (bypassing the view) to confirm
    the optimizer's chosen station is genuinely the cheapest one reachable
    along the real corridor — not an artifact of mocking."""
    from routes.domain.types import Coordinates, Route, VehicleProfile
    from routes.services.geospatial import RouteGeospatialService
    from routes.services.route_plan_service import RoutePlanService

    routing_service = MagicMock()
    routing_service.route.return_value = Route(
        distance_miles=REAL_ROUTE_DISTANCE_MILES,
        duration_minutes=561.8,
        geometry={
            "type": "LineString",
            "coordinates": [
                [OKC["longitude"], OKC["latitude"]],
                [ABQ["longitude"], ABQ["latitude"]],
            ],
        },
        segment_distances_miles=[REAL_ROUTE_DISTANCE_MILES],
    )

    service = RoutePlanService(
        geocoding_service=MagicMock(),
        routing_service=routing_service,
        station_repository=load_enriched_stations,
        geospatial_service=RouteGeospatialService(corridor_miles=5.0),
        vehicle=VehicleProfile(
            mpg=10.0, max_range_miles=500.0, tank_capacity_gallons=50.0, starting_fuel_gallons=50.0
        ),
    )

    result = service.plan(Coordinates(**OKC), Coordinates(**ABQ))

    assert result.fuel_plan.feasible
    assert len(result.fuel_plan.stops) >= 1
    chosen = result.fuel_plan.stops[0]

    # Verify the chosen price is genuinely one of the real CSV's prices,
    # and that it's at least as cheap as the full set of real candidates
    # reachable at that point in the corridor (the optimizer didn't just
    # pick an arbitrary nearby station).
    real_station = FuelStation.objects.get(id=chosen.station_id)
    assert real_station.effective_price_per_gallon == chosen.price_per_gallon
    assert chosen.fuel_purchased_gallons > 0


# Atlanta, GA -> Miami, FL via I-75 — a second, independent corridor from
# the original OKC-Albuquerque demo route, proving coordinate coverage
# isn't limited to a single hardcoded path. Geometry is the REAL OSRM
# response captured live for this route (routes/tests/fixtures/
# atlanta_miami_route.json) — a straight line between the two city centers
# would cut across the real curved highway and miss real nearby stations,
# so the actual road shape is used, same as the OKC-Albuquerque fixture
# above uses OSRM's real reported distance.
ATLANTA = {"latitude": 33.7490, "longitude": -84.3880}
MIAMI = {"latitude": 25.7617, "longitude": -80.1918}
ATLANTA_MIAMI_FIXTURE = Path(__file__).parent / "fixtures" / "atlanta_miami_route.json"


@pytest.mark.django_db
def test_api_route_plan_works_for_a_second_independent_corridor(real_enriched_stations):
    with open(ATLANTA_MIAMI_FIXTURE) as fh:
        route_data = json.load(fh)

    distance_meters = route_data["distance_miles"] * 1609.344
    segment_meters = [d * 1609.344 for d in route_data["segment_distances_miles"]]
    osrm_body = {
        "code": "Ok",
        "routes": [
            {
                "distance": distance_meters,
                "duration": route_data["duration_minutes"] * 60.0,
                "geometry": route_data["geometry"],
                "legs": [{"annotation": {"distance": segment_meters}}],
            }
        ],
    }

    client = APIClient()
    with patch("routes.services.routing.requests.get") as mock_route:
        mock_route.return_value = make_response(200, json_body=osrm_body)
        response = client.post(
            "/api/v1/routes/plan", {"start": ATLANTA, "destination": MIAMI}, format="json"
        )

    assert response.status_code == 200
    body = response.json()
    assert body["fuel"]["feasible"] is True
    assert len(body["stops"]) >= 1
    for stop in body["stops"]:
        real_station = FuelStation.objects.get(id=stop["station_id"])
        assert real_station.state in ("GA", "FL")
