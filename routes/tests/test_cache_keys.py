from routes.domain.types import Coordinates
from routes.services.cache_keys import geocode_cache_key, route_cache_key

ORIGIN = Coordinates(latitude=40.712800, longitude=-74.006000)
DESTINATION = Coordinates(latitude=41.878100, longitude=-87.629800)


def test_geocode_cache_key_is_deterministic():
    key_a = geocode_cache_key("Chicago, IL", "v1")
    key_b = geocode_cache_key("Chicago, IL", "v1")
    assert key_a == key_b


def test_geocode_cache_key_normalizes_whitespace_and_case():
    key_a = geocode_cache_key("Chicago, IL", "v1")
    key_b = geocode_cache_key("  chicago,   il  ", "v1")
    assert key_a == key_b


def test_geocode_cache_key_differs_by_schema_version():
    key_v1 = geocode_cache_key("Chicago, IL", "v1")
    key_v2 = geocode_cache_key("Chicago, IL", "v2")
    assert key_v1 != key_v2


def test_geocode_cache_key_differs_by_location():
    key_a = geocode_cache_key("Chicago, IL", "v1")
    key_b = geocode_cache_key("New York, NY", "v1")
    assert key_a != key_b


def test_route_cache_key_is_deterministic():
    key_a = route_cache_key(ORIGIN, DESTINATION, "driving", "v1")
    key_b = route_cache_key(ORIGIN, DESTINATION, "driving", "v1")
    assert key_a == key_b


def test_route_cache_key_differs_by_schema_version():
    key_v1 = route_cache_key(ORIGIN, DESTINATION, "driving", "v1")
    key_v2 = route_cache_key(ORIGIN, DESTINATION, "driving", "v2")
    assert key_v1 != key_v2


def test_route_cache_key_differs_by_profile():
    key_driving = route_cache_key(ORIGIN, DESTINATION, "driving", "v1")
    key_walking = route_cache_key(ORIGIN, DESTINATION, "walking", "v1")
    assert key_driving != key_walking


def test_route_cache_key_tolerates_negligible_coordinate_noise():
    # Differences beyond the 5th decimal (~1m) must not change the key.
    noisy_origin = Coordinates(
        latitude=ORIGIN.latitude + 1e-7, longitude=ORIGIN.longitude - 1e-7
    )
    key_a = route_cache_key(ORIGIN, DESTINATION, "driving", "v1")
    key_b = route_cache_key(noisy_origin, DESTINATION, "driving", "v1")
    assert key_a == key_b


def test_route_cache_key_differs_for_different_coordinates():
    other_destination = Coordinates(latitude=34.0522, longitude=-118.2437)
    key_a = route_cache_key(ORIGIN, DESTINATION, "driving", "v1")
    key_b = route_cache_key(ORIGIN, other_destination, "driving", "v1")
    assert key_a != key_b
