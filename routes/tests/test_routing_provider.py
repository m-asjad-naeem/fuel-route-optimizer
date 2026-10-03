from unittest.mock import patch

import pytest
import requests

from routes.domain.types import Coordinates
from routes.services.exceptions import RouteNotFoundError, RoutingProviderError
from routes.services.routing import OSRMRoutingProvider
from routes.tests.helpers import make_response

PROVIDER = OSRMRoutingProvider(base_url="https://osrm.example.test")
ORIGIN = Coordinates(latitude=40.7128, longitude=-74.0060)
DESTINATION = Coordinates(latitude=41.8781, longitude=-87.6298)


def osrm_ok_body(distance=1270000.0, duration=43200.0, coordinates=None, segment_distances=None):
    if coordinates is None:
        coordinates = [[-74.0060, 40.7128], [-80.0, 41.0], [-87.6298, 41.8781]]
    if segment_distances is None:
        # one distance per pair of consecutive coordinates (annotations=distance)
        segment_distances = [600000.0] * max(0, len(coordinates) - 1)
    return {
        "code": "Ok",
        "routes": [
            {
                "distance": distance,
                "duration": duration,
                "geometry": {"type": "LineString", "coordinates": coordinates},
                "legs": [{"annotation": {"distance": segment_distances}}],
            }
        ],
    }


@patch("routes.services.routing.requests.get")
def test_route_success_returns_distance_duration_geometry(mock_get):
    mock_get.return_value = make_response(200, json_body=osrm_ok_body())

    route = PROVIDER.route(ORIGIN, DESTINATION)

    assert route.distance_miles == pytest.approx(1270000.0 / 1609.344)
    assert route.duration_minutes == pytest.approx(43200.0 / 60.0)
    assert route.geometry["type"] == "LineString"
    assert len(route.geometry["coordinates"]) == 3
    assert route.segment_distances_miles == pytest.approx(
        [600000.0 / 1609.344, 600000.0 / 1609.344]
    )

    # exactly one call, requesting full geojson geometry + per-segment
    # distance annotations on the driving profile
    args, kwargs = mock_get.call_args
    assert "/route/v1/driving/" in args[0]
    assert kwargs["params"]["overview"] == "full"
    assert kwargs["params"]["geometries"] == "geojson"
    assert kwargs["params"]["annotations"] == "distance"


@patch("routes.services.routing.requests.get")
def test_route_no_route_raises_route_not_found(mock_get):
    mock_get.return_value = make_response(200, json_body={"code": "NoRoute", "routes": []})

    with pytest.raises(RouteNotFoundError):
        PROVIDER.route(ORIGIN, DESTINATION)


@patch("routes.services.routing.requests.get")
def test_route_http_400_with_no_route_code_raises_route_not_found(mock_get):
    # Confirmed live against the real OSRM public server: a genuinely
    # unreachable pair of points (e.g. across an ocean) returns HTTP 400,
    # not 200, with {"code": "NoRoute", ...} in the body. This must still
    # surface as the application's documented route_not_found/422, not a
    # 502 provider error — the HTTP status alone doesn't distinguish "OSRM
    # is down" from "OSRM looked and there genuinely is no route."
    mock_get.return_value = make_response(
        400, json_body={"message": "Impossible route between points", "code": "NoRoute"}
    )

    with pytest.raises(RouteNotFoundError):
        PROVIDER.route(ORIGIN, DESTINATION)


@patch("routes.services.routing.requests.get")
def test_route_http_400_with_invalid_query_code_raises_routing_provider_error(mock_get):
    # A non-NoRoute error code under a 4xx status reflects a problem with
    # our own request (malformed query), not the user's route — this must
    # stay a provider error, not be misread as "no route".
    mock_get.return_value = make_response(
        400, json_body={"message": "bad query", "code": "InvalidQuery"}
    )

    with pytest.raises(RoutingProviderError):
        PROVIDER.route(ORIGIN, DESTINATION)


@patch("routes.services.routing.requests.get")
def test_route_http_error_raises_routing_provider_error_not_raw_requests_exception(mock_get):
    mock_get.return_value = make_response(503, json_body={"error": "unavailable"})

    with pytest.raises(RoutingProviderError) as exc_info:
        PROVIDER.route(ORIGIN, DESTINATION)

    assert not isinstance(exc_info.value, requests.exceptions.RequestException)


@patch("routes.services.routing.requests.get")
def test_route_timeout_raises_routing_provider_error(mock_get):
    mock_get.side_effect = requests.exceptions.Timeout("simulated timeout")

    with pytest.raises(RoutingProviderError):
        PROVIDER.route(ORIGIN, DESTINATION)


@patch("routes.services.routing.requests.get")
def test_route_malformed_json_raises_routing_provider_error(mock_get):
    mock_get.return_value = make_response(200, raw_body=b"{not valid json")

    with pytest.raises(RoutingProviderError):
        PROVIDER.route(ORIGIN, DESTINATION)


@patch("routes.services.routing.requests.get")
def test_route_missing_geometry_raises_routing_provider_error(mock_get):
    body = osrm_ok_body()
    del body["routes"][0]["geometry"]

    mock_get.return_value = make_response(200, json_body=body)

    with pytest.raises(RoutingProviderError):
        PROVIDER.route(ORIGIN, DESTINATION)


@patch("routes.services.routing.requests.get")
def test_route_wrong_geometry_type_raises_routing_provider_error(mock_get):
    body = osrm_ok_body()
    body["routes"][0]["geometry"] = {"type": "Point", "coordinates": [-74.0, 40.7]}

    mock_get.return_value = make_response(200, json_body=body)

    with pytest.raises(RoutingProviderError):
        PROVIDER.route(ORIGIN, DESTINATION)


@patch("routes.services.routing.requests.get")
def test_route_empty_coordinates_raises_routing_provider_error(mock_get):
    body = osrm_ok_body(coordinates=[])

    mock_get.return_value = make_response(200, json_body=body)

    with pytest.raises(RoutingProviderError):
        PROVIDER.route(ORIGIN, DESTINATION)


@patch("routes.services.routing.requests.get")
def test_route_missing_distance_raises_routing_provider_error(mock_get):
    body = osrm_ok_body()
    del body["routes"][0]["distance"]

    mock_get.return_value = make_response(200, json_body=body)

    with pytest.raises(RoutingProviderError):
        PROVIDER.route(ORIGIN, DESTINATION)


@patch("routes.services.routing.requests.get")
def test_route_empty_routes_list_raises_route_not_found(mock_get):
    mock_get.return_value = make_response(200, json_body={"code": "Ok", "routes": []})

    with pytest.raises(RouteNotFoundError):
        PROVIDER.route(ORIGIN, DESTINATION)


@patch("routes.services.routing.requests.get")
def test_route_missing_legs_raises_routing_provider_error(mock_get):
    body = osrm_ok_body()
    del body["routes"][0]["legs"]

    mock_get.return_value = make_response(200, json_body=body)

    with pytest.raises(RoutingProviderError):
        PROVIDER.route(ORIGIN, DESTINATION)


@patch("routes.services.routing.requests.get")
def test_route_missing_annotation_distance_raises_routing_provider_error(mock_get):
    body = osrm_ok_body()
    body["routes"][0]["legs"] = [{"annotation": {}}]

    mock_get.return_value = make_response(200, json_body=body)

    with pytest.raises(RoutingProviderError):
        PROVIDER.route(ORIGIN, DESTINATION)


@patch("routes.services.routing.requests.get")
def test_route_segment_distance_count_mismatch_raises_routing_provider_error(mock_get):
    # 3 coordinates need exactly 2 segment distances, not 1.
    body = osrm_ok_body(segment_distances=[600000.0])

    mock_get.return_value = make_response(200, json_body=body)

    with pytest.raises(RoutingProviderError):
        PROVIDER.route(ORIGIN, DESTINATION)


@patch("routes.services.routing.requests.get")
def test_route_non_numeric_segment_distance_raises_routing_provider_error(mock_get):
    body = osrm_ok_body(segment_distances=[600000.0, "not-a-number"])

    mock_get.return_value = make_response(200, json_body=body)

    with pytest.raises(RoutingProviderError):
        PROVIDER.route(ORIGIN, DESTINATION)
