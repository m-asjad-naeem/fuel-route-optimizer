from django.db import models


class GeocodeCacheEntry(models.Model):
    """DB-backed cache for geocoding results — no Redis, deliberately. One
    small, infrequently-written table is simpler than adding infrastructure
    for this assessment's scope, and survives process restarts.
    """

    cache_key = models.CharField(max_length=64, unique=True, db_index=True)
    latitude = models.FloatField()
    longitude = models.FloatField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.cache_key


class RouteCacheEntry(models.Model):
    """DB-backed cache for routing results."""

    cache_key = models.CharField(max_length=64, unique=True, db_index=True)
    distance_miles = models.FloatField()
    duration_minutes = models.FloatField()
    geometry_geojson = models.JSONField()
    # OSRM's authoritative per-segment distances (miles) — see
    # Route.segment_distances_miles. Cached alongside the geometry so a
    # cache hit still has everything needed for travel-distance math.
    segment_distances_miles = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.cache_key
