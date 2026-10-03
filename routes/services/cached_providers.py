"""Caching decorators around GeocodingProvider/RoutingProvider.

On a cache hit, the wrapped provider is never called — tests assert this
directly via call counts on a mock provider. On a miss, the provider is
called once, the result is validated (providers raise before returning
anything malformed — see routing.py/geocoding.py), and only then stored.

Race safety: two concurrent cache misses both call the provider and both
attempt to write the same cache_key. The resulting unique-constraint
violation is caught and ignored rather than requiring explicit locking.
"""
from __future__ import annotations

from django.db import IntegrityError

from routes.domain.types import Coordinates, Route
from routes.models import GeocodeCacheEntry, RouteCacheEntry
from routes.services.cache_keys import geocode_cache_key, route_cache_key
from routes.services.geocoding import GeocodingProvider
from routes.services.routing import RoutingProvider


class CachedGeocodingService:
    def __init__(self, provider: GeocodingProvider, schema_version: str):
        self._provider = provider
        self._schema_version = schema_version

    def geocode(self, location: str) -> Coordinates:
        key = geocode_cache_key(location, self._schema_version)

        cached = GeocodeCacheEntry.objects.filter(cache_key=key).first()
        if cached is not None:
            return Coordinates(latitude=cached.latitude, longitude=cached.longitude)

        # Cache miss — exactly one provider call. If the provider raises
        # (including LocationNotFoundError), nothing is cached, which is
        # correct: a failure to resolve a location shouldn't be remembered
        # as if it were a stable fact.
        result = self._provider.geocode(location)

        try:
            GeocodeCacheEntry.objects.create(
                cache_key=key, latitude=result.latitude, longitude=result.longitude
            )
        except IntegrityError:
            # Lost a race with a concurrent request that cached the same key
            # first — that's fine, our freshly-fetched result is equivalent.
            pass

        return result


class CachedRoutingService:
    def __init__(self, provider: RoutingProvider, profile: str, schema_version: str):
        self._provider = provider
        self._profile = profile
        self._schema_version = schema_version

    def route(self, origin: Coordinates, destination: Coordinates) -> Route:
        key = route_cache_key(origin, destination, self._profile, self._schema_version)

        cached = RouteCacheEntry.objects.filter(cache_key=key).first()
        if cached is not None:
            return Route(
                distance_miles=cached.distance_miles,
                duration_minutes=cached.duration_minutes,
                geometry=cached.geometry_geojson,
                segment_distances_miles=cached.segment_distances_miles,
            )

        result = self._provider.route(origin, destination)

        try:
            RouteCacheEntry.objects.create(
                cache_key=key,
                distance_miles=result.distance_miles,
                duration_minutes=result.duration_minutes,
                geometry_geojson=result.geometry,
                segment_distances_miles=result.segment_distances_miles,
            )
        except IntegrityError:
            pass

        return result
