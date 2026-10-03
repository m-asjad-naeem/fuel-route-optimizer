from unittest.mock import MagicMock

import pytest

from routes.domain.types import Coordinates, Route
from routes.models import GeocodeCacheEntry, RouteCacheEntry
from routes.services.cached_providers import CachedGeocodingService, CachedRoutingService
from routes.services.exceptions import LocationNotFoundError

ORIGIN = Coordinates(latitude=40.7128, longitude=-74.0060)
DESTINATION = Coordinates(latitude=41.8781, longitude=-87.6298)
SAMPLE_ROUTE = Route(
    distance_miles=790.4,
    duration_minutes=720.0,
    geometry={"type": "LineString", "coordinates": [[-74.006, 40.7128], [-87.6298, 41.8781]]},
    segment_distances_miles=[790.4],
)


@pytest.mark.django_db
def test_geocode_cache_miss_then_hit_calls_provider_exactly_once():
    mock_provider = MagicMock()
    mock_provider.geocode.return_value = Coordinates(latitude=41.8781, longitude=-87.6298)
    service = CachedGeocodingService(mock_provider, schema_version="v1")

    first = service.geocode("Chicago, IL")
    assert mock_provider.geocode.call_count == 1
    assert GeocodeCacheEntry.objects.count() == 1

    second = service.geocode("Chicago, IL")
    # provider must NOT be called again on a cache hit
    assert mock_provider.geocode.call_count == 1
    assert GeocodeCacheEntry.objects.count() == 1

    assert first == second


@pytest.mark.django_db
def test_geocode_failure_is_not_cached():
    mock_provider = MagicMock()
    mock_provider.geocode.side_effect = LocationNotFoundError("nope")
    service = CachedGeocodingService(mock_provider, schema_version="v1")

    with pytest.raises(LocationNotFoundError):
        service.geocode("Nowhereville")

    assert GeocodeCacheEntry.objects.count() == 0

    # a second attempt must call the provider again, since nothing was cached
    with pytest.raises(LocationNotFoundError):
        service.geocode("Nowhereville")
    assert mock_provider.geocode.call_count == 2


@pytest.mark.django_db
def test_geocode_cache_race_is_handled_safely():
    mock_provider = MagicMock()
    mock_provider.geocode.return_value = Coordinates(latitude=41.8781, longitude=-87.6298)
    service = CachedGeocodingService(mock_provider, schema_version="v1")

    # Simulate a concurrent request having already written the same key
    # between our cache-miss check and our own write.
    from routes.services.cache_keys import geocode_cache_key

    key = geocode_cache_key("Chicago, IL", "v1")
    GeocodeCacheEntry.objects.create(cache_key=key, latitude=41.8781, longitude=-87.6298)

    # create() on an existing unique cache_key raises IntegrityError — the
    # service must swallow it and still return a valid result, not crash.
    result = service.geocode("Chicago, IL")
    assert result.latitude == pytest.approx(41.8781)
    assert GeocodeCacheEntry.objects.count() == 1


@pytest.mark.django_db
def test_route_cache_miss_then_hit_calls_provider_exactly_once():
    mock_provider = MagicMock()
    mock_provider.route.return_value = SAMPLE_ROUTE
    service = CachedRoutingService(mock_provider, profile="driving", schema_version="v1")

    first = service.route(ORIGIN, DESTINATION)
    assert mock_provider.route.call_count == 1
    assert RouteCacheEntry.objects.count() == 1

    second = service.route(ORIGIN, DESTINATION)
    assert mock_provider.route.call_count == 1
    assert RouteCacheEntry.objects.count() == 1

    assert first.distance_miles == second.distance_miles
    assert first.geometry == second.geometry
    assert first.segment_distances_miles == second.segment_distances_miles == [790.4]


@pytest.mark.django_db
def test_route_cache_distinguishes_different_destinations():
    mock_provider = MagicMock()
    mock_provider.route.return_value = SAMPLE_ROUTE
    service = CachedRoutingService(mock_provider, profile="driving", schema_version="v1")

    other_destination = Coordinates(latitude=34.0522, longitude=-118.2437)
    service.route(ORIGIN, DESTINATION)
    service.route(ORIGIN, other_destination)

    assert mock_provider.route.call_count == 2
    assert RouteCacheEntry.objects.count() == 2
