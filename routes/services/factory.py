"""Application-level construction boundary.

This is the ONLY place real provider/service instances get wired together
from Django settings. Pure/application modules (fuel_optimizer.py,
route_plan_service.py, geospatial.py) never read settings or construct
their own dependencies — everything is passed in, which is what lets tests
inject mocks instead of calling this factory at all.
"""
from __future__ import annotations

from django.conf import settings

from fuel.repositories import load_enriched_stations
from routes.domain.types import VehicleProfile
from routes.services.cached_providers import CachedGeocodingService, CachedRoutingService
from routes.services.geocoding import NominatimGeocodingProvider
from routes.services.geospatial import RouteGeospatialService
from routes.services.route_plan_service import RoutePlanService
from routes.services.routing import OSRMRoutingProvider


def build_geocoding_service() -> CachedGeocodingService:
    provider = NominatimGeocodingProvider(
        base_url=settings.NOMINATIM_BASE_URL,
        user_agent=settings.NOMINATIM_USER_AGENT,
    )
    return CachedGeocodingService(provider, schema_version=settings.GEOCODE_CACHE_SCHEMA_VERSION)


def build_routing_service() -> CachedRoutingService:
    provider = OSRMRoutingProvider(
        base_url=settings.OSRM_BASE_URL,
        profile=settings.OSRM_ROUTING_PROFILE,
    )
    return CachedRoutingService(
        provider,
        profile=settings.OSRM_ROUTING_PROFILE,
        schema_version=settings.ROUTE_CACHE_SCHEMA_VERSION,
    )


def build_geospatial_service() -> RouteGeospatialService:
    return RouteGeospatialService(
        projected_crs=settings.GEOSPATIAL_PROJECTED_CRS,
        corridor_miles=settings.FUEL_STATION_ROUTE_BUFFER_MILES,
    )


def build_vehicle_profile() -> VehicleProfile:
    return VehicleProfile(
        mpg=settings.VEHICLE_MPG,
        max_range_miles=settings.VEHICLE_MAX_RANGE_MILES,
        tank_capacity_gallons=settings.VEHICLE_TANK_CAPACITY_GALLONS,
        # Business assumption: the vehicle always starts with a full tank —
        # not independently configurable, since it isn't a vehicle constant,
        # it's a trip-start assumption.
        starting_fuel_gallons=settings.VEHICLE_TANK_CAPACITY_GALLONS,
    )


def build_route_plan_service() -> RoutePlanService:
    return RoutePlanService(
        geocoding_service=build_geocoding_service(),
        routing_service=build_routing_service(),
        station_repository=load_enriched_stations,
        geospatial_service=build_geospatial_service(),
        vehicle=build_vehicle_profile(),
    )
