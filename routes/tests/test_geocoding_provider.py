from unittest.mock import patch

import pytest
import requests

from routes.services.exceptions import GeocodingProviderError, LocationNotFoundError
from routes.services.geocoding import NominatimGeocodingProvider
from routes.tests.helpers import make_response

PROVIDER = NominatimGeocodingProvider(base_url="https://nominatim.example.test")


@patch("routes.services.geocoding.requests.get")
def test_geocode_success(mock_get):
    mock_get.return_value = make_response(
        200, json_body=[{"lat": "41.8781", "lon": "-87.6298", "display_name": "Chicago, IL"}]
    )

    coordinates = PROVIDER.geocode("Chicago, IL")

    assert coordinates.latitude == pytest.approx(41.8781)
    assert coordinates.longitude == pytest.approx(-87.6298)
    # User-Agent must be set per Nominatim's usage policy
    _, kwargs = mock_get.call_args
    assert "User-Agent" in kwargs["headers"]
    assert kwargs["timeout"] == PROVIDER.timeout_seconds
    # constrained to US results via Nominatim's own documented filter,
    # so an ambiguous/non-US match (e.g. "Paris" -> Paris, France) is
    # rejected by the provider itself rather than relying solely on the
    # downstream country-boundary check
    assert kwargs["params"]["countrycodes"] == "us"


@patch("routes.services.geocoding.requests.get")
def test_geocode_location_not_found_is_distinct_from_provider_error(mock_get):
    mock_get.return_value = make_response(200, json_body=[])

    with pytest.raises(LocationNotFoundError):
        PROVIDER.geocode("Nowhereville, ZZ")


@patch("routes.services.geocoding.requests.get")
def test_geocode_http_error_raises_provider_error_not_raw_requests_exception(mock_get):
    mock_get.return_value = make_response(500, json_body={"error": "server error"})

    with pytest.raises(GeocodingProviderError) as exc_info:
        PROVIDER.geocode("Chicago, IL")

    # the raw requests.HTTPError must not leak to the caller
    assert not isinstance(exc_info.value, requests.exceptions.RequestException)


@patch("routes.services.geocoding.requests.get")
def test_geocode_timeout_raises_provider_error(mock_get):
    mock_get.side_effect = requests.exceptions.Timeout("simulated timeout")

    with pytest.raises(GeocodingProviderError):
        PROVIDER.geocode("Chicago, IL")


@patch("routes.services.geocoding.requests.get")
def test_geocode_connection_error_raises_provider_error(mock_get):
    mock_get.side_effect = requests.exceptions.ConnectionError("simulated connection error")

    with pytest.raises(GeocodingProviderError):
        PROVIDER.geocode("Chicago, IL")


@patch("routes.services.geocoding.requests.get")
def test_geocode_malformed_json_raises_provider_error(mock_get):
    mock_get.return_value = make_response(200, raw_body=b"not json at all {{{")

    with pytest.raises(GeocodingProviderError):
        PROVIDER.geocode("Chicago, IL")


@patch("routes.services.geocoding.requests.get")
def test_geocode_unexpected_shape_raises_provider_error(mock_get):
    # Nominatim's jsonv2 format always returns a list; a dict here is malformed.
    mock_get.return_value = make_response(200, json_body={"unexpected": "shape"})

    with pytest.raises(GeocodingProviderError):
        PROVIDER.geocode("Chicago, IL")


@patch("routes.services.geocoding.requests.get")
def test_geocode_missing_lat_lon_raises_provider_error(mock_get):
    mock_get.return_value = make_response(200, json_body=[{"display_name": "no coords here"}])

    with pytest.raises(GeocodingProviderError):
        PROVIDER.geocode("Chicago, IL")
