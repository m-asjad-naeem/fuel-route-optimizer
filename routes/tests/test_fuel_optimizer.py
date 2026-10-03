from decimal import Decimal

import pytest

from routes.domain.fuel_optimizer import (
    InvalidOptimizerInputError,
    _compute_destination_reachability,
    plan_fuel_stops,
)
from routes.domain.types import StationCandidate, VehicleProfile

TOLERANCE = 1e-6


def vehicle(**overrides):
    defaults = dict(
        mpg=10.0, max_range_miles=500.0, tank_capacity_gallons=50.0, starting_fuel_gallons=50.0
    )
    defaults.update(overrides)
    return VehicleProfile(**defaults)


def candidate(station_id, position, price, distance=0.0, travel_distance=None):
    """`travel_distance` defaults to `position` — most tests don't care
    about the route_position_miles/travel_distance_miles distinction and
    can treat them as the same number. Tests that specifically exercise the
    distinction (the Phase 6.1 regression tests) pass a different value."""
    return StationCandidate(
        station_id=station_id,
        route_position_miles=position,
        travel_distance_miles=position if travel_distance is None else travel_distance,
        distance_from_route_miles=distance,
        price_per_gallon=Decimal(str(price)),
    )


DEFAULT_VEHICLE = vehicle()

THREE_TIER_PRICING = [
    candidate(1, 400.0, "5.0"),
    candidate(2, 450.0, "4.0"),
    candidate(3, 480.0, "3.0"),
]


def assert_plan_invariants(plan, vehicle_profile):
    """Generic structural checks any feasible plan must satisfy (items 16, 21-24)."""
    assert plan.feasible
    max_gallons = vehicle_profile.tank_capacity_gallons + TOLERANCE
    for stop in plan.stops:
        assert -TOLERANCE <= stop.fuel_before_gallons <= max_gallons
        assert -TOLERANCE <= stop.fuel_purchased_gallons <= max_gallons
        assert -TOLERANCE <= stop.fuel_after_gallons <= max_gallons
        assert stop.fuel_cost == Decimal(str(stop.fuel_purchased_gallons)) * stop.price_per_gallon
    assert plan.total_cost == sum((s.fuel_cost for s in plan.stops), Decimal("0"))
    assert plan.total_gallons_purchased == pytest.approx(
        sum(s.fuel_purchased_gallons for s in plan.stops)
    )


# 1. Destination reachable on starting fuel.
def test_destination_reachable_on_starting_fuel():
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 400.0, [])
    assert plan.feasible
    assert plan.stops == []
    assert plan.total_cost == Decimal("0")
    assert plan.total_gallons_purchased == 0.0
    assert plan.fuel_consumed_gallons == pytest.approx(40.0)
    assert plan.starting_fuel_gallons == 50.0


# 2. One station is required.
def test_one_station_required_completes_without_crashing():
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 600.0, [candidate(1, 300.0, "3.0")])
    assert plan.feasible
    assert len(plan.stops) == 1
    stop = plan.stops[0]
    assert stop.station_id == 1
    assert stop.fuel_before_gallons == pytest.approx(20.0)
    assert stop.fuel_purchased_gallons == pytest.approx(10.0)
    assert stop.fuel_after_gallons == pytest.approx(0.0)
    assert stop.fuel_cost == Decimal("30.0")
    assert plan.total_cost == Decimal("30.0")


# regression test: single-required-station is the scenario a naive
# "stations only, destination never a waypoint" implementation gets wrong.
def test_single_required_station_completes_without_crashing():
    # Identical construction to test 2 — named explicitly as the regression
    # case for the "farthest reachable STATION is not the same as farthest
    # reachable POINT" bug described in the module docstring.
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 600.0, [candidate(1, 300.0, "3.0")])
    assert plan.feasible
    assert len(plan.stops) == 1


# 3. Multiple stations with progressively cheaper prices.
def test_multiple_progressively_cheaper_stations():
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 900.0, THREE_TIER_PRICING)
    assert plan.feasible
    # only the cheapest (station 3) is ever worth buying at
    assert [s.station_id for s in plan.stops] == [3]
    stop = plan.stops[0]
    assert stop.fuel_before_gallons == pytest.approx(2.0)
    assert stop.fuel_purchased_gallons == pytest.approx(40.0)
    assert stop.fuel_cost == Decimal("120.0")
    assert plan.total_cost == Decimal("120.0")


# 4. Cheaper station within range -> buy only enough to reach it.
def test_cheaper_station_buys_only_enough_to_reach_it():
    candidates = [candidate(1, 50.0, "5.0"), candidate(2, 450.0, "2.0")]
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 600.0, candidates)
    assert plan.feasible
    # station 1 is visited but never bought at (0 purchase there)
    assert [s.station_id for s in plan.stops] == [2]
    stop = plan.stops[0]
    assert stop.fuel_before_gallons == pytest.approx(5.0)
    assert stop.fuel_purchased_gallons == pytest.approx(10.0)
    assert stop.fuel_cost == Decimal("20.0")


# 5. No cheaper reachable station -> fill tank.
def test_no_cheaper_station_fills_tank():
    candidates = [candidate(1, 100.0, "2.0"), candidate(2, 550.0, "3.0")]
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 900.0, candidates)
    assert plan.feasible
    assert [s.station_id for s in plan.stops] == [1, 2]
    first = plan.stops[0]
    assert first.fuel_before_gallons == pytest.approx(40.0)
    assert first.fuel_purchased_gallons == pytest.approx(10.0)  # fills to 50
    assert first.fuel_after_gallons == pytest.approx(5.0)


# 6. Equal-price station is NOT considered cheaper.
def test_equal_price_station_is_not_cheaper():
    candidates = [candidate(1, 100.0, "3.0"), candidate(2, 300.0, "3.0")]
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 700.0, candidates)
    assert plan.feasible
    # station 2 has the SAME price as station 1, so station 1 fills the tank
    # (treating station 2 as "not cheaper") rather than buying a minimal
    # top-up expecting a price break that doesn't exist.
    first = plan.stops[0]
    assert first.station_id == 1
    assert first.fuel_purchased_gallons == pytest.approx(10.0)  # filled to 50, not a minimal top-up


# 7. Destination reachable -> do not fill tank unnecessarily.
def test_destination_reachable_does_not_fill_tank_unnecessarily():
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 450.0, [candidate(1, 100.0, "2.0")])
    assert plan.feasible
    # 450 miles needs 45 gallons; starting with 50 already covers it, so the
    # vehicle should never even need to stop.
    assert plan.stops == []


# 8. Starting full tank -> initial purchase is zero.
def test_starting_full_tank_initial_purchase_is_zero():
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 600.0, [candidate(1, 50.0, "2.0")])
    # the only stop (if any) must not reflect a wasted purchase just to
    # reach the first station, since the tank was already full enough
    for stop in plan.stops:
        if stop.route_position_miles == 50.0:
            assert stop.fuel_purchased_gallons == pytest.approx(0.0)


# 9. Station requiring partial refill.
def test_station_requiring_partial_refill():
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 600.0, [candidate(1, 300.0, "3.0")])
    stop = plan.stops[0]
    assert 0 < stop.fuel_purchased_gallons < 50.0


# 10. Station requiring tank to be filled.
def test_station_requiring_tank_to_be_filled():
    candidates = [candidate(1, 100.0, "2.0"), candidate(2, 550.0, "3.0")]
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 900.0, candidates)
    first = plan.stops[0]
    assert first.fuel_after_gallons == pytest.approx(5.0)
    assert first.fuel_purchased_gallons == pytest.approx(
        DEFAULT_VEHICLE.tank_capacity_gallons - first.fuel_before_gallons
    )


# 11. Maximum 500-mile segment.
def test_maximum_500_mile_segment_is_feasible():
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 500.0, [])
    assert plan.feasible
    assert plan.stops == []


# 12. Segment just over 500 miles -> infeasible.
def test_segment_just_over_max_range_is_infeasible():
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 500.1, [])
    assert not plan.feasible
    assert plan.infeasibility_reason is not None


# 13. No stations and destination beyond range -> infeasible.
def test_no_stations_destination_beyond_range_is_infeasible():
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 1000.0, [])
    assert not plan.feasible
    assert plan.stops == []
    assert plan.total_cost == Decimal("0")


# 14. Multiple possible feasible chains.
def test_multiple_feasible_chains_picks_the_cheapest():
    # Two independent bridging chains exist; the cheap one should be used.
    candidates = [
        candidate(1, 400.0, "4.0"),  # expensive chain's bridge
        candidate(2, 420.0, "1.5"),  # cheap chain's bridge, reachable from start too
    ]
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 900.0, candidates)
    assert plan.feasible
    assert [s.station_id for s in plan.stops] == [2]


# 15. Feasibility correctly excludes a genuine dead-end candidate.
def test_feasibility_excludes_a_genuine_dead_end_candidate():
    # Station at 490 is reachable from start (490 <= 500) but the gap from
    # it to the destination (1000 - 490 = 510) exceeds max_range, and
    # nothing else bridges that gap -> the whole route is infeasible, even
    # though a naive "any reachable candidate must be fine" rule would have
    # accepted this station without checking onward feasibility.
    dead_end = candidate(1, 490.0, "1.0")
    reachable = _compute_destination_reachability([dead_end], 1000.0, 500.0)
    assert reachable == [False]

    plan = plan_fuel_stops(DEFAULT_VEHICLE, 1000.0, [dead_end])
    assert not plan.feasible


# 16. A cheaper station exists but is not physically reachable.
def test_cheaper_station_out_of_range_is_ignored():
    candidates = [
        candidate(1, 400.0, "4.0"),  # reachable, more expensive
        candidate(2, 850.0, "1.0"),  # cheaper, but beyond max_range of start (850 > 500)
    ]
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 1300.0, candidates)
    # station 2 is unreachable directly from start (850 > 500); the plan
    # must still work via station 1 first, never "jumping" straight to 2.
    assert plan.feasible
    assert plan.stops[0].station_id == 1


# 17. Candidate station near route end.
def test_candidate_station_near_route_end():
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 520.0, [candidate(1, 480.0, "2.0")])
    assert plan.feasible
    assert plan.stops[0].station_id == 1
    assert plan.stops[0].route_position_miles == pytest.approx(480.0)


# 18. Duplicate route positions.
def test_duplicate_route_positions_picks_cheapest_deterministically():
    candidates = [candidate(1, 300.0, "4.0"), candidate(2, 300.0, "2.0")]
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 600.0, candidates)
    assert plan.feasible
    assert len(plan.stops) == 1
    assert plan.stops[0].station_id == 2  # the cheaper of the co-located pair
    assert plan.stops[0].price_per_gallon == Decimal("2.0")


# 19. Deterministic output ordering.
def test_output_is_deterministic_across_repeated_runs():
    plan_a = plan_fuel_stops(DEFAULT_VEHICLE, 900.0, THREE_TIER_PRICING)
    plan_b = plan_fuel_stops(DEFAULT_VEHICLE, 900.0, list(reversed(THREE_TIER_PRICING)))
    assert [s.station_id for s in plan_a.stops] == [s.station_id for s in plan_b.stops]
    assert plan_a.total_cost == plan_b.total_cost


# 20. Decimal money arithmetic.
def test_money_fields_are_decimal():
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 600.0, [candidate(1, 300.0, "3.0")])
    assert isinstance(plan.total_cost, Decimal)
    for stop in plan.stops:
        assert isinstance(stop.fuel_cost, Decimal)
        assert isinstance(stop.price_per_gallon, Decimal)


# 21. Total purchased gallons equals sum of stop purchases.
def test_total_purchased_equals_sum_of_stops():
    candidates = [candidate(1, 100.0, "2.0"), candidate(2, 550.0, "3.0")]
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 900.0, candidates)
    assert plan.total_gallons_purchased == pytest.approx(
        sum(s.fuel_purchased_gallons for s in plan.stops)
    )


# 22. Total cost equals sum of stop costs.
def test_total_cost_equals_sum_of_stop_costs():
    candidates = [candidate(1, 100.0, "2.0"), candidate(2, 550.0, "3.0")]
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 900.0, candidates)
    assert plan.total_cost == sum((s.fuel_cost for s in plan.stops), Decimal("0"))


# 23. Fuel never becomes negative.
def test_fuel_never_negative_across_scenarios():
    scenarios = [
        (400.0, []),
        (600.0, [candidate(1, 300.0, "3.0")]),
        (900.0, [candidate(1, 100.0, "2.0"), candidate(2, 550.0, "3.0")]),
    ]
    for distance, candidates in scenarios:
        plan = plan_fuel_stops(DEFAULT_VEHICLE, distance, candidates)
        for stop in plan.stops:
            assert stop.fuel_before_gallons >= -TOLERANCE
            assert stop.fuel_after_gallons >= -TOLERANCE


# 24. Fuel never exceeds tank capacity.
def test_fuel_never_exceeds_tank_capacity():
    candidates = [candidate(1, 100.0, "2.0"), candidate(2, 550.0, "3.0")]
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 900.0, candidates)
    for stop in plan.stops:
        assert stop.fuel_before_gallons <= DEFAULT_VEHICLE.tank_capacity_gallons + TOLERANCE
        assert stop.fuel_after_gallons <= DEFAULT_VEHICLE.tank_capacity_gallons + TOLERANCE
        assert stop.fuel_purchased_gallons <= DEFAULT_VEHICLE.tank_capacity_gallons + TOLERANCE


# 25. Invalid vehicle profiles.
@pytest.mark.parametrize(
    "overrides",
    [
        dict(mpg=0.0),
        dict(mpg=-5.0),
        dict(max_range_miles=0.0),
        dict(max_range_miles=-1.0),
        dict(tank_capacity_gallons=0.0),
        dict(tank_capacity_gallons=-10.0),
        dict(starting_fuel_gallons=-1.0),
        dict(starting_fuel_gallons=999.0),  # exceeds tank capacity
    ],
)
def test_invalid_vehicle_profiles_raise(overrides):
    with pytest.raises(InvalidOptimizerInputError):
        plan_fuel_stops(vehicle(**overrides), 400.0, [])


# 26. Invalid route distance.
def test_negative_route_distance_raises():
    with pytest.raises(InvalidOptimizerInputError):
        plan_fuel_stops(DEFAULT_VEHICLE, -1.0, [])


# 27. Invalid station positions.
def test_negative_station_position_raises():
    with pytest.raises(InvalidOptimizerInputError):
        plan_fuel_stops(DEFAULT_VEHICLE, 500.0, [candidate(1, -10.0, "3.0")])


def test_station_position_beyond_route_distance_raises():
    with pytest.raises(InvalidOptimizerInputError):
        plan_fuel_stops(DEFAULT_VEHICLE, 500.0, [candidate(1, 600.0, "3.0")])


def test_invalid_candidate_price_raises():
    with pytest.raises(InvalidOptimizerInputError):
        plan_fuel_stops(DEFAULT_VEHICLE, 500.0, [candidate(1, 100.0, "0")])


# 28. Empty candidate list.
def test_empty_candidate_list_within_range_is_feasible():
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 500.0, [])
    assert plan.feasible


def test_empty_candidate_list_beyond_range_is_infeasible():
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 501.0, [])
    assert not plan.feasible


# 29. Very short route.
def test_very_short_route():
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 1.0, [])
    assert plan.feasible
    assert plan.stops == []
    assert plan.fuel_consumed_gallons == pytest.approx(0.1)


# 30. Route exactly 500 miles.
def test_route_exactly_max_range_is_feasible():
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 500.0, [])
    assert plan.feasible


def test_route_exactly_max_range_with_tiny_overage_is_infeasible():
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 500.0 + 0.01, [])
    assert not plan.feasible


# -- general invariant sweep across several constructed scenarios ----------


@pytest.mark.parametrize(
    "distance,candidates",
    [
        (400.0, []),
        (600.0, [candidate(1, 300.0, "3.0")]),
        (900.0, THREE_TIER_PRICING),
        (900.0, [candidate(1, 100.0, "2.0"), candidate(2, 550.0, "3.0")]),
    ],
)
def test_plan_invariants_hold(distance, candidates):
    plan = plan_fuel_stops(DEFAULT_VEHICLE, distance, candidates)
    assert_plan_invariants(plan, DEFAULT_VEHICLE)


# ---------------------------------------------------------------------
# Phase 6.1 regression: the optimizer must use travel_distance_miles for
# ALL fuel/feasibility math, and must never be influenced by
# route_position_miles (the Shapely-projected ordering coordinate, which
# can legitimately diverge from OSRM's authoritative distance — see
# routes/services/geospatial.py).
# ---------------------------------------------------------------------


def test_optimizer_uses_travel_distance_not_route_position_for_fuel_math():
    # route_position_miles (520) would make this station look OUT of range
    # of START (max_range=500) if the optimizer mistakenly used it for the
    # reachability check — a buggy implementation would exclude it entirely
    # and report the whole route infeasible. travel_distance_miles (300) is
    # the authoritative distance and is well within range.
    station = candidate(1, position=520.0, price="3.0", travel_distance=300.0)
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 700.0, [station])

    assert plan.feasible
    assert len(plan.stops) == 1
    stop = plan.stops[0]
    # fuel required to reach the station, using the AUTHORITATIVE distance
    # (300mi/10mpg=30gal), not the projected one (520mi would need 52gal —
    # more fuel than the 50-gallon tank can even hold)
    assert stop.fuel_before_gallons == pytest.approx(50.0 - 300.0 / 10.0)


def test_optimizer_infeasible_route_position_within_range_but_travel_distance_is_not():
    # The inverse of the previous test: route_position_miles (50) looks
    # safely within max_range (500) of START, but the AUTHORITATIVE
    # travel_distance_miles (550) exceeds max_range on its own. With no
    # other candidate and a destination also beyond direct range, the
    # route must be reported infeasible — proving the optimizer checks
    # reachability using travel_distance_miles, not route_position_miles
    # (a buggy implementation using route_position_miles would treat this
    # station as reachable from START and incorrectly report feasible).
    station = candidate(1, position=50.0, price="3.0", travel_distance=550.0)
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 600.0, [station])

    assert not plan.feasible


def test_total_fuel_consumed_matches_travel_distance_not_route_position():
    # A route whose candidates have route_position_miles values that sum to
    # a materially different total than route_distance_miles (simulating a
    # projected-polyline/OSRM discrepancy) must still report fuel_consumed
    # based on route_distance_miles (OSRM's number), and every purchase
    # decision based on travel_distance_miles — never on route_position_miles.
    station = candidate(1, position=450.0, price="2.0", travel_distance=400.0)
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 700.0, [station])

    assert plan.feasible
    assert plan.fuel_consumed_gallons == pytest.approx(700.0 / 10.0)
    stop = plan.stops[0]
    # fuel_required to reach the station used travel_distance (400), not
    # route_position (450): 400/10=40 gallons needed, starting with 50 —
    # so purchase should be computed relative to 400, and the remaining
    # leg (700-400=300mi, 30gal) should require a purchase since fuel left
    # after reaching the station (50-40=10) is insufficient.
    assert stop.fuel_before_gallons == pytest.approx(10.0)
    assert stop.fuel_purchased_gallons == pytest.approx(20.0)  # need 30 total, have 10


# ---------------------------------------------------------------------
# Regression: production reported a real request crashing with
#   InvalidOptimizerInputError: station 380 travel_distance_miles
#   (544.7709855445179) exceeds route_distance_miles (544.7709750059652)
# A station essentially AT the route's endpoint, computed via the
# geospatial service's independent interpolation path, can land ~1e-5
# miles beyond OSRM's own reported total purely from floating-point/
# projection round-trip noise. The old TOLERANCE (1e-6 mi) was too tight
# to absorb this; DISTANCE_EPSILON_MILES (1e-4 mi) is the fix.
# ---------------------------------------------------------------------


def test_station_exactly_at_route_end_is_accepted():
    station = candidate(1, position=500.0, price="2.0", travel_distance=500.0)
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 500.0, [station])
    assert plan.feasible


def test_station_tiny_float_amount_beyond_route_is_accepted():
    # Reproduces the exact magnitude observed in production (~1.05e-5 mi
    # delta between a station's travel_distance_miles and the route's own
    # route_distance_miles), scaled to fit within max_range_miles (500) so
    # the scenario is feasible for the right reason — this test is about
    # the validation tolerance, not about range feasibility.
    delta = 544.7709855445179 - 544.7709750059652  # ~1.0539e-5
    route_distance = 500.0 - delta
    travel_distance = 500.0
    station = candidate(1, position=travel_distance, price="2.0", travel_distance=travel_distance)
    plan = plan_fuel_stops(DEFAULT_VEHICLE, route_distance, [station])
    assert plan.feasible


def test_station_genuinely_beyond_route_is_still_rejected():
    # 0.1 miles is three orders of magnitude larger than DISTANCE_EPSILON_MILES
    # (1e-4) — a real data error, not floating-point noise, and must still raise.
    station = candidate(1, position=500.1, price="2.0", travel_distance=500.1)
    with pytest.raises(InvalidOptimizerInputError):
        plan_fuel_stops(DEFAULT_VEHICLE, 500.0, [station])


def test_fill_tank_branch_targets_cheapest_reachable_not_farthest():
    # Found by an independent LP solver comparison: when no cheaper station
    # is in range, driving to the farthest reachable station (rather than
    # the cheapest reachable one) can force an avoidable top-up at a higher
    # price later. Exact scenario from that comparison, minimized total
    # cost $45.24 vs. $46.15 under the old (farthest-targeting) behavior.
    vehicle = DEFAULT_VEHICLE
    stations = [
        candidate(0, 86.42001128539671, "2.883"),
        candidate(1, 308.32228397191886, "3.934"),
        candidate(2, 311.47991346008763, "3.582"),
        candidate(3, 361.73471607106706, "3.599"),
        candidate(4, 381.42729560818, "3.294"),
        candidate(5, 397.63555951646373, "4.222"),
        candidate(6, 596.227830796752, "2.964"),
        candidate(7, 600.8952522363057, "2.803"),
    ]
    plan = plan_fuel_stops(vehicle, 656.9469406378371, stations)

    assert plan.feasible
    # the fill-up at station 0 must drive toward station 4 (cheapest
    # reachable: 3.294), not station 5 (farthest reachable: 4.222)
    assert [s.station_id for s in plan.stops] == [0, 4, 6, 7]
    assert float(plan.total_cost) == pytest.approx(45.24029697425328, abs=1e-6)


# ---------------------------------------------------------------------
# B3: a station is off-route by distance_from_route_miles — reaching its
# pump and returning is real driven distance, not a free teleport.
# ---------------------------------------------------------------------


def test_station_directly_on_route_is_unaffected_by_detour_logic():
    # distance=0.0 (the default): must behave identically to the
    # pre-detour-fix model — no regression for the common case.
    station = candidate(1, 300.0, "2.50", distance=0.0)
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 600.0, [station])

    assert plan.feasible
    assert plan.stops[0].detour_distance_miles == 0.0
    assert plan.stops[0].detour_fuel_gallons == 0.0
    assert plan.stops[0].fuel_before_gallons == pytest.approx(50.0 - 300.0 / 10.0)


def test_station_slightly_off_route_reserves_round_trip_detour_fuel():
    # 2 miles off-route: round trip = 4 miles = 0.4 gal at 10mpg. Reaching
    # it (300mi along-route) at the full 50-gal starting tank leaves
    # 50 - 300/10 = 20 gal; after reserving the 0.2 gal outbound detour,
    # fuel_before_gallons at the pump must be 20 - 0.2 = 19.8, not 20.
    station = candidate(1, 300.0, "2.50", distance=2.0)
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 600.0, [station])

    assert plan.feasible
    stop = plan.stops[0]
    assert stop.detour_distance_miles == pytest.approx(4.0)
    assert stop.detour_fuel_gallons == pytest.approx(0.4)
    assert stop.fuel_before_gallons == pytest.approx(50.0 - 300.0 / 10.0 - 2.0 / 10.0)


def test_station_near_corridor_maximum_detour_is_still_feasible_with_margin():
    # 5 miles off-route (the geospatial corridor's own default maximum):
    # round trip = 1.0 gal at 10mpg. Route exceeds max_range (500) so a
    # stop is actually required, with comfortable margin to spare.
    station = candidate(1, 200.0, "2.50", distance=5.0)
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 600.0, [station])

    assert plan.feasible
    assert len(plan.stops) == 1
    assert plan.stops[0].detour_fuel_gallons == pytest.approx(1.0)


def test_insufficient_fuel_to_reach_off_route_station_is_infeasible():
    # Along-route alone, this station sits exactly at max_range (500mi) —
    # reachable with zero fuel to spare. Adding ANY off-route detour makes
    # it genuinely unreachable: the route must be reported infeasible, not
    # silently teleport the vehicle the extra distance.
    station = candidate(1, 500.0, "2.50", distance=3.0)
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 900.0, [station])

    assert not plan.feasible
    assert plan.infeasibility_reason is not None


def test_multiple_off_route_stations_each_reserve_their_own_detour():
    # Two off-route stations; the optimizer must reserve each one's own
    # round-trip detour on both the leg arriving at it and the leg leaving
    # it, not just one or the other.
    s1 = candidate(1, 300.0, "3.00", distance=3.0)  # cheaper-ahead not available first
    s2 = candidate(2, 600.0, "2.00", distance=4.0)
    plan = plan_fuel_stops(DEFAULT_VEHICLE, 900.0, s_list := [s1, s2])
    assert len(s_list) == 2

    assert plan.feasible
    assert len(plan.stops) == 2
    first, second = plan.stops
    assert first.detour_fuel_gallons == pytest.approx(0.6)  # 2*3/10
    assert second.detour_fuel_gallons == pytest.approx(0.8)  # 2*4/10
    # `fuel_after_gallons` already represents fuel on arrival at the NEXT
    # stop (it brackets the whole leg: buy, then travel+detour) — so it
    # must equal the next stop's fuel_before_gallons directly.
    assert second.fuel_before_gallons == pytest.approx(first.fuel_after_gallons)
    # arriving at station 2 must have cost 300mi hop + 3mi return(s1) +
    # 4mi outbound(s2) = 307mi (30.7 gal) of the fuel bought at station 1
    assert first.fuel_after_gallons == pytest.approx(
        first.fuel_before_gallons + first.fuel_purchased_gallons - (3.0 + 300.0 + 4.0) / 10.0
    )
    # total consumption includes both round-trip detours, not just the
    # along-route distance
    assert plan.fuel_consumed_gallons == pytest.approx(
        900.0 / 10.0 + first.detour_fuel_gallons + second.detour_fuel_gallons
    )
    # overall feasibility and totals still self-consistent
    assert plan.total_gallons_purchased == pytest.approx(
        sum(s.fuel_purchased_gallons for s in plan.stops)
    )
