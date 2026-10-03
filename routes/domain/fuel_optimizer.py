"""Pure fuel-stop planning. No Django, DRF, requests, Shapely, pyproj, or
database access anywhere in this module — it operates only on
`VehicleProfile`, `StationCandidate`, and plain numbers.

The algorithm runs in two separate steps:

1. Feasibility (`_compute_destination_reachability`) — a one-dimensional
   reachability check: ignoring price, does some chain
   START -> station -> station -> ... -> DESTINATION exist where every
   hop is <= max_range_miles?
2. Cost minimization (`plan_fuel_stops`) — given at least one feasible
   chain, greedily minimize purchase cost while only moving between points
   already proven feasible.

Feasibility is computed up front, over all candidates plus the
destination itself, rather than left implicit in the greedy walk. The
destination is always a reachable waypoint, not just stations — a greedy
that only ever considers stations as waypoints can run out of candidates
once the destination is the only point left to reach, even on an
otherwise perfectly feasible route.
"""
from __future__ import annotations

from decimal import Decimal

from routes.domain.types import FuelPlan, FuelStop, StationCandidate, VehicleProfile

# Distance/fuel quantities are plain floats; money stays Decimal. This
# tolerance absorbs floating-point noise from the optimizer's own
# arithmetic. 1e-6 miles (~1.6mm) and 1e-6 gallons are both far below any
# value that could change which station is chosen.
TOLERANCE = 1e-6

# travel_distance_miles (OSRM per-segment distance plus local interpolation,
# see routes/services/geospatial.py) and route_distance_miles (OSRM's
# single reported total) are computed by independent paths that are
# expected to agree closely but accumulate more floating-point error than
# a single arithmetic comparison — a station essentially at the route's
# endpoint can land ~1e-5 miles beyond route_distance_miles. This tolerance
# is scoped to that one comparison and stays three orders of magnitude
# below any gap that should genuinely be rejected as invalid.
DISTANCE_EPSILON_MILES = 1e-4


class InvalidOptimizerInputError(ValueError):
    """Raised for malformed/contradictory domain inputs (e.g. mpg <= 0, a
    station positioned beyond the route's own length). This is distinct
    from an infeasible `FuelPlan`: an invalid input is a contract violation
    by the caller, never a legitimate "no route exists" outcome, so it is
    raised rather than returned.
    """


def plan_fuel_stops(
    vehicle: VehicleProfile,
    route_distance_miles: float,
    candidates: list[StationCandidate],
) -> FuelPlan:
    """Build a minimum-cost fuel plan for a route of `route_distance_miles`,
    given `candidates` already filtered to the route corridor and sorted by
    `route_position_miles`.

    All fuel/feasibility arithmetic here uses
    `StationCandidate.travel_distance_miles` exclusively — the OSRM-derived
    authoritative distance — never `route_position_miles`, which is only
    the Shapely-projected ordering coordinate used to sort `candidates`.
    See routes/services/geospatial.py for why these two are kept separate.
    """
    _validate_inputs(vehicle, route_distance_miles, candidates)

    reachable = _compute_destination_reachability(
        candidates, route_distance_miles, vehicle.max_range_miles
    )
    feasible_candidates = [c for c, ok in zip(candidates, reachable) if ok]

    if not _destination_reachable_from_start(
        feasible_candidates, route_distance_miles, vehicle.max_range_miles
    ):
        gap_reason = _describe_infeasibility(
            candidates, route_distance_miles, vehicle.max_range_miles
        )
        return FuelPlan(
            feasible=False,
            stops=[],
            starting_fuel_gallons=vehicle.starting_fuel_gallons,
            fuel_consumed_gallons=0.0,
            total_gallons_purchased=0.0,
            total_cost=Decimal("0"),
            infeasibility_reason=gap_reason,
        )

    return _greedy_minimum_cost_plan(vehicle, route_distance_miles, feasible_candidates)


# ---------------------------------------------------------------------
# Feasibility (price-blind)
# ---------------------------------------------------------------------


def _compute_destination_reachability(
    candidates: list[StationCandidate], route_distance_miles: float, max_range_miles: float
) -> list[bool]:
    """For each candidate (by index), can the DESTINATION be reached from it
    through some chain of further candidates, each hop <= max_range_miles?

    A single backward scan: a candidate can reach the destination either
    directly (the remaining distance is within one tank) or by reaching
    some later candidate that itself can.

    A station is not physically ON the route — it sits
    `distance_from_route_miles` off to the side (the only geospatial signal
    available without a routing call per station; see
    routes/services/geospatial.py). Treating `travel_distance_miles` alone
    as if a stop there were free, as an earlier version of this function
    did, can report a station reachable when the vehicle would actually run
    out of fuel on the detour off the highway to the pump. Every hop
    TO a candidate therefore reserves that candidate's own one-way detour
    distance, and every hop FROM a candidate reserves its return trip back
    to the route — both added to the plain along-route distance, modeling
    the detour as real driven miles rather than a free teleport.
    """
    n = len(candidates)
    can_reach = [False] * n

    for i in range(n - 1, -1, -1):
        position_i = candidates[i].travel_distance_miles
        detour_i = candidates[i].distance_from_route_miles
        # direct to the destination: pay i's return trip, then the plain
        # along-route remainder (the destination itself has no detour)
        if detour_i + (route_distance_miles - position_i) <= max_range_miles + TOLERANCE:
            can_reach[i] = True
            continue
        for j in range(i + 1, n):
            position_j = candidates[j].travel_distance_miles
            detour_j = candidates[j].distance_from_route_miles
            required = detour_i + (position_j - position_i) + detour_j
            # NOTE: no early-exit on `required` exceeding range here — with
            # per-candidate detours, required distance is no longer
            # guaranteed monotonic in j the way a plain position gap was.
            if required <= max_range_miles + TOLERANCE and can_reach[j]:
                can_reach[i] = True
                break

    return can_reach


def _destination_reachable_from_start(
    feasible_candidates: list[StationCandidate],
    route_distance_miles: float,
    max_range_miles: float,
) -> bool:
    if route_distance_miles <= max_range_miles + TOLERANCE:
        return True
    # START sits on the route (no detour of its own) — reaching candidate c
    # still reserves c's one-way detour to its pump.
    return any(
        (c.travel_distance_miles + c.distance_from_route_miles) <= max_range_miles + TOLERANCE
        for c in feasible_candidates
    )


def _describe_infeasibility(
    candidates: list[StationCandidate], route_distance_miles: float, max_range_miles: float
) -> str:
    """Best-effort human-readable reason, pointing at the specific gap that
    exceeds max_range_miles, for debugging/API error messages."""
    positions = [0.0] + sorted(c.travel_distance_miles for c in candidates) + [route_distance_miles]
    for a, b in zip(positions, positions[1:]):
        gap = b - a
        if gap > max_range_miles + TOLERANCE:
            return (
                f"No station or destination is reachable within "
                f"{max_range_miles} miles of route position {a:.1f} "
                f"(next point is {gap:.1f} miles away)"
            )
    return "No feasible fuel plan exists for this route"


# ---------------------------------------------------------------------
# Cost minimization over the feasible candidates only
# ---------------------------------------------------------------------


def _greedy_minimum_cost_plan(
    vehicle: VehicleProfile,
    route_distance_miles: float,
    feasible_candidates: list[StationCandidate],
) -> FuelPlan:
    """Greedy cost minimization over candidates already proven feasible.

    Each iteration evaluates one purchase decision at the vehicle's
    current position (START, or a station it previously drove to), then
    picks the next waypoint: another station, or the destination itself.
    Treating the destination as a selectable waypoint (not just stations)
    matters once it becomes the only remaining reachable point.

    Decision order at each step:
      (A) destination reachable on fuel already in the tank -> stop, buy nothing.
      (B) a strictly cheaper station is reachable within one tank -> buy only
          enough to reach it.
      (C1) no cheaper station, but the destination is reachable within one
           tank -> buy only enough to finish (there's nothing beyond the
           destination to plan fuel for, so topping off would be an
           unnecessary purchase at a non-minimal price).
      (C2) no cheaper station, destination not yet in range -> fill the tank
           completely (this price is the best available before running out
           of options) and drive to the cheapest reachable station (not the
           farthest — driving past a cheaper-but-nearer station to reach a
           pricier-but-farther one can force an unnecessary top-up at the
           higher price later).

    Detour fuel: a station sits `distance_from_route_miles` off the route,
    not on it. Every leg's required fuel reserves the CURRENT position's
    one-way return-to-route trip (`current_distance_from_route`, zero at
    START) plus, when the leg ends at a station rather than the
    destination, that station's one-way trip off the route to its pump.
    Both halves of a given station's round trip are real driven miles, just
    charged on the two legs that touch it (arriving, then departing) rather
    than lumped at either one.
    """
    mpg = vehicle.mpg
    max_range = vehicle.max_range_miles
    tank_capacity = vehicle.tank_capacity_gallons

    position = 0.0
    fuel = vehicle.starting_fuel_gallons
    current_price: Decimal | None = None  # None == "at START, nothing to compare against"
    current_station_id: int | None = None
    current_distance_from_route: float = 0.0
    remaining = list(feasible_candidates)
    stops: list[FuelStop] = []

    # Defensive bound only — overall feasibility is already proven before
    # this loop runs, and each iteration strictly consumes at least one
    # candidate from `remaining` (see the position filter at the loop's
    # end) unless it terminates by reaching the destination, so this can
    # only trip if that invariant is somehow broken.
    max_iterations = len(feasible_candidates) + 2

    for _ in range(max_iterations):
        fuel = _clamp(fuel, 0.0, tank_capacity)

        # (A) — finishing from here also pays the current position's
        # return-to-route trip (zero if we're at START).
        required_to_finish = (current_distance_from_route + (route_distance_miles - position)) / mpg
        if required_to_finish <= fuel + TOLERANCE:
            return _build_plan(vehicle, route_distance_miles, stops)

        station_ahead = [
            c
            for c in remaining
            if c.travel_distance_miles > position + TOLERANCE
            and (
                current_distance_from_route
                + (c.travel_distance_miles - position)
                + c.distance_from_route_miles
            )
            <= max_range + TOLERANCE
        ]

        if current_price is None:
            cheaper = station_ahead
        else:
            cheaper = [c for c in station_ahead if c.price_per_gallon < current_price]

        destination_within_one_tank = (
            current_distance_from_route + (route_distance_miles - position) <= max_range + TOLERANCE
        )

        if cheaper:
            # (B)
            nearest_cheaper = min(cheaper, key=lambda c: (c.travel_distance_miles, c.station_id))
            station = _resolve_co_located(station_ahead, nearest_cheaper)
            target_position = station.travel_distance_miles
            fuel_required = (
                current_distance_from_route
                + (target_position - position)
                + station.distance_from_route_miles
            ) / mpg
            purchase = max(0.0, fuel_required - fuel)
            moving_to_destination = False
        elif destination_within_one_tank:
            # (C1) — strictly cheaper is not an option, so buy exactly what
            # the final leg (including the return trip off this station,
            # if any) costs at the current (best-available) price.
            fuel_required = required_to_finish
            purchase = max(0.0, fuel_required - fuel)
            station = None
            moving_to_destination = True
        elif station_ahead:
            # (C2) — drive to the cheapest reachable station, not the
            # farthest: this is the station we'll next be comparing prices
            # against, so reaching it at the lowest available price
            # minimizes what any further top-up along the way will cost.
            station = min(station_ahead, key=lambda c: (c.price_per_gallon, c.station_id))
            target_position = station.travel_distance_miles
            fuel_required = (
                current_distance_from_route
                + (target_position - position)
                + station.distance_from_route_miles
            ) / mpg
            purchase = max(0.0, tank_capacity - fuel)
            moving_to_destination = False
        else:
            # Cannot happen: feasibility was proven before this loop started.
            # A defensive exception beats silently returning a wrong plan.
            raise AssertionError(
                "fuel optimizer found no reachable feasible candidate despite "
                "proven overall feasibility — this indicates a bug, not a "
                "legitimately infeasible route"
            )

        purchase = _clamp_small(purchase)
        fuel_after = _clamp(fuel + purchase - fuel_required, 0.0, tank_capacity)

        # The purchase happens AT the position we are currently sitting at
        # (a real station) — never at START, which has no price and is
        # never recorded as a stop.
        if purchase > TOLERANCE and current_price is not None:
            stops.append(
                FuelStop(
                    station_id=current_station_id,
                    route_position_miles=position,
                    distance_from_route_miles=current_distance_from_route,
                    price_per_gallon=current_price,
                    fuel_before_gallons=fuel,
                    fuel_purchased_gallons=purchase,
                    fuel_after_gallons=fuel_after,
                    fuel_cost=(Decimal(str(purchase)) * current_price),
                    detour_distance_miles=2 * current_distance_from_route,
                    detour_fuel_gallons=2 * current_distance_from_route / mpg,
                )
            )

        fuel = fuel_after

        if moving_to_destination:
            position = route_distance_miles
            # The destination sits on the route, not off it — any pending
            # "return to the route" reservation from the last station was
            # already paid as part of required_to_finish above. Leaving it
            # set here would make the next loop's (A) check think another
            # phantom return trip were still owed after already arriving.
            current_distance_from_route = 0.0
            continue  # next loop's (A) check will now trivially succeed

        position = station.travel_distance_miles
        current_price = station.price_per_gallon
        current_station_id = station.station_id
        current_distance_from_route = station.distance_from_route_miles
        # Once passed, a candidate (and any candidate at or behind the new
        # position, including ones skipped this iteration) is never
        # reconsidered. Position is non-decreasing each iteration, which
        # combined with this filter guarantees termination.
        remaining = [c for c in remaining if c.travel_distance_miles > position + TOLERANCE]

    raise AssertionError("fuel optimizer exceeded its iteration safety bound")


def _resolve_co_located(
    station_ahead: list[StationCandidate], target: StationCandidate
) -> StationCandidate:
    """If more than one candidate sits at (effectively) the same route
    position as `target`, treat them as the same physical stop and use
    whichever is cheapest."""
    target_position = target.travel_distance_miles
    co_located = [
        c for c in station_ahead if abs(c.travel_distance_miles - target_position) <= TOLERANCE
    ]
    return min(co_located, key=lambda c: (c.price_per_gallon, c.station_id))


def _build_plan(
    vehicle: VehicleProfile, route_distance_miles: float, stops: list[FuelStop]
) -> FuelPlan:
    # Total fuel actually burned is the along-route distance PLUS every
    # stop's round-trip detour off the route to its pump and back — a
    # vehicle that leaves the highway to refuel really does burn that extra
    # fuel, so reporting only route_distance_miles/mpg would understate
    # true consumption for any stop with a nonzero distance_from_route_miles.
    fuel_consumed = route_distance_miles / vehicle.mpg + sum(
        s.detour_fuel_gallons for s in stops
    )
    total_gallons_purchased = sum(s.fuel_purchased_gallons for s in stops)
    total_cost = sum((s.fuel_cost for s in stops), Decimal("0"))
    return FuelPlan(
        feasible=True,
        stops=stops,
        starting_fuel_gallons=vehicle.starting_fuel_gallons,
        fuel_consumed_gallons=fuel_consumed,
        total_gallons_purchased=total_gallons_purchased,
        total_cost=total_cost,
    )


# ---------------------------------------------------------------------
# Validation and small numeric helpers
# ---------------------------------------------------------------------


def _validate_inputs(
    vehicle: VehicleProfile, route_distance_miles: float, candidates: list[StationCandidate]
) -> None:
    if route_distance_miles < 0:
        raise InvalidOptimizerInputError(
            f"route_distance_miles must be >= 0, got {route_distance_miles}"
        )
    if vehicle.mpg <= 0:
        raise InvalidOptimizerInputError(f"mpg must be > 0, got {vehicle.mpg}")
    if vehicle.max_range_miles <= 0:
        raise InvalidOptimizerInputError(
            f"max_range_miles must be > 0, got {vehicle.max_range_miles}"
        )
    if vehicle.tank_capacity_gallons <= 0:
        raise InvalidOptimizerInputError(
            f"tank_capacity_gallons must be > 0, got {vehicle.tank_capacity_gallons}"
        )
    if vehicle.starting_fuel_gallons < 0:
        raise InvalidOptimizerInputError(
            f"starting_fuel_gallons must be >= 0, got {vehicle.starting_fuel_gallons}"
        )
    if vehicle.starting_fuel_gallons > vehicle.tank_capacity_gallons + TOLERANCE:
        raise InvalidOptimizerInputError(
            "starting_fuel_gallons "
            f"({vehicle.starting_fuel_gallons}) cannot exceed tank_capacity_gallons "
            f"({vehicle.tank_capacity_gallons})"
        )

    for c in candidates:
        if c.travel_distance_miles < 0:
            raise InvalidOptimizerInputError(
                f"station {c.station_id} has a negative travel_distance_miles: "
                f"{c.travel_distance_miles}"
            )
        if c.travel_distance_miles > route_distance_miles + DISTANCE_EPSILON_MILES:
            raise InvalidOptimizerInputError(
                f"station {c.station_id} travel_distance_miles "
                f"({c.travel_distance_miles}) exceeds route_distance_miles "
                f"({route_distance_miles})"
            )
        if c.price_per_gallon is None or c.price_per_gallon <= 0:
            raise InvalidOptimizerInputError(
                f"station {c.station_id} has an invalid price_per_gallon: "
                f"{c.price_per_gallon!r}"
            )


def _clamp(value: float, lo: float, hi: float) -> float:
    """Hard-clamp to [lo, hi]. Any value landing outside this range by more
    than TOLERANCE indicates an arithmetic bug upstream, not a legitimate
    physical state — this function absorbs only floating-point noise."""
    return max(lo, min(hi, value))


def _clamp_small(value: float) -> float:
    return 0.0 if abs(value) < TOLERANCE else value
