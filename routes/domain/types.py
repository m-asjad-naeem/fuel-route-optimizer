"""Framework-independent domain objects.

Nothing in this module may import Django, DRF, requests, OSRM-specific
shapes, or database models. These types are the vocabulary providers and
services speak in; application code never passes raw HTTP response data
around.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class Coordinates:
    latitude: float
    longitude: float


@dataclass(frozen=True)
class Route:
    distance_miles: float
    duration_minutes: float
    # GeoJSON LineString: {"type": "LineString", "coordinates": [[lon, lat], ...]}
    geometry: dict[str, Any]
    # OSRM's own per-segment road distances (miles), one entry per pair of
    # consecutive points in geometry["coordinates"] — len(coordinates) - 1
    # entries. This is what lets travel distance along the route be
    # computed authoritatively (see routes/services/geospatial.py) without
    # a route-wide scale factor and without a routing call per station.
    segment_distances_miles: list[float]


@dataclass(frozen=True)
class StationLocation:
    """A station as raw input to the geospatial service.

    Deliberately permissive (fields may be None) — the geospatial service is
    responsible for filtering out stations with missing/invalid data; the
    Django adapter that produces these is not expected to have already
    validated them.
    """

    station_id: int
    latitude: float | None
    longitude: float | None
    price_per_gallon: Decimal | None
    # Display-only metadata, never used in any geospatial/optimizer math —
    # carried through purely so the API response can show a human-readable
    # station instead of a bare id.
    name: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None


@dataclass(frozen=True)
class StationCandidate:
    """A station that passed corridor filtering, ready for the fuel
    optimizer. The optimizer must depend on nothing but this type — no
    coordinates, no Shapely, no Django, no HTTP.

    Two deliberately distinct distance fields — never use one in place of
    the other:

    - `route_position_miles`: the station's position along the
      Shapely-projected route polyline. Ordering/corridor purposes only.
      Not guaranteed to agree with OSRM's authoritative distance when the
      projected polyline's own length differs from it.
    - `travel_distance_miles`: the station's authoritative road distance
      from the route's start, derived from OSRM's per-segment distances
      (see Route.segment_distances_miles). This is the only distance the
      fuel optimizer may use for fuel consumption or feasibility math.
    """

    station_id: int
    route_position_miles: float
    travel_distance_miles: float
    distance_from_route_miles: float
    price_per_gallon: Decimal


@dataclass(frozen=True)
class VehicleProfile:
    """Vehicle constants for the fuel optimizer.

    `starting_fuel_gallons` is kept as its own explicit field — distinct
    from `tank_capacity_gallons` — even though the business assumption is
    always "starts full" (starting_fuel_gallons == tank_capacity_gallons),
    so the concepts of "how much fuel exists right now" and "how much the
    tank can ever hold" are never conflated in the optimizer's arithmetic.
    """

    mpg: float
    max_range_miles: float
    tank_capacity_gallons: float
    starting_fuel_gallons: float


@dataclass(frozen=True)
class FuelStop:
    station_id: int
    # Authoritative (OSRM-derived) distance from route start — sourced from
    # StationCandidate.travel_distance_miles, never from route_position_miles.
    route_position_miles: float
    distance_from_route_miles: float
    price_per_gallon: Decimal
    fuel_before_gallons: float
    fuel_purchased_gallons: float
    fuel_after_gallons: float
    fuel_cost: Decimal
    # Round-trip (there and back) distance/fuel to actually leave the route
    # and reach this station's pump — always 2x distance_from_route_miles,
    # exposed so a client can see this wasn't a free teleport. Already
    # reserved internally (split across the arriving and departing legs;
    # see fuel_optimizer.py), not an additional charge on top of the gallon
    # figures above.
    detour_distance_miles: float = 0.0
    detour_fuel_gallons: float = 0.0
    # Display-only metadata attached by RoutePlanService after the optimizer
    # runs (see RoutePlanService._attach_station_details) — never read or
    # produced by the optimizer itself, which only ever knows station_id.
    station_name: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    latitude: float | None = None
    longitude: float | None = None


@dataclass(frozen=True)
class FuelPlan:
    feasible: bool
    stops: list[FuelStop]
    starting_fuel_gallons: float
    fuel_consumed_gallons: float
    total_gallons_purchased: float
    total_cost: Decimal
    infeasibility_reason: str | None = None
