"""Request validation and response formatting for POST /api/v1/routes/plan.

Kept deliberately separate from RoutePlanService: this module only knows
about HTTP-shaped input/output. It never talks to a provider, the database,
or the optimizer directly — it either produces a validated `start`/
`destination` (each a `str` or `Coordinates`) for the service to consume,
or formats a `RoutePlanResult` into the JSON-serializable dict the view
returns.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from rest_framework import serializers

from routes.domain.types import Coordinates
from routes.services.route_plan_service import RoutePlanResult

PRICE_DECIMAL_PLACES = Decimal("0.00000001")  # matches fuel.models precision (8 places)
MONEY_DECIMAL_PLACES = Decimal("0.01")


class LocationField(serializers.Field):
    """Accepts EITHER a non-empty string (to be geocoded) OR a
    `{"latitude": ..., "longitude": ...}` object — never both, and never
    anything else. Returns a `str` or a `Coordinates`, so RoutePlanService
    never has to re-inspect the raw request shape.
    """

    def to_internal_value(self, data):
        if isinstance(data, str):
            if not data.strip():
                raise serializers.ValidationError("This field may not be blank.")
            return data

        if isinstance(data, dict):
            allowed_keys = {"latitude", "longitude"}
            if set(data.keys()) != allowed_keys:
                raise serializers.ValidationError(
                    "A coordinate location must have exactly 'latitude' and "
                    "'longitude' and no other fields."
                )

            latitude = self._as_float(data.get("latitude"), "latitude")
            longitude = self._as_float(data.get("longitude"), "longitude")

            if not (-90.0 <= latitude <= 90.0):
                raise serializers.ValidationError("latitude must be between -90 and 90.")
            if not (-180.0 <= longitude <= 180.0):
                raise serializers.ValidationError("longitude must be between -180 and 180.")

            return Coordinates(latitude=latitude, longitude=longitude)

        raise serializers.ValidationError(
            "A location must be either a non-empty string or an object with "
            "'latitude' and 'longitude'."
        )

    @staticmethod
    def _as_float(value, field_name: str) -> float:
        if value is None:
            raise serializers.ValidationError(f"{field_name} is required.")
        if isinstance(value, bool):  # bool is an int subclass — reject explicitly
            raise serializers.ValidationError(f"{field_name} must be a number.")
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise serializers.ValidationError(f"{field_name} must be a number.") from exc

    def to_representation(self, value):
        # This field is request-only; the response never echoes it back.
        raise NotImplementedError


class RoutePlanRequestSerializer(serializers.Serializer):
    start = LocationField()
    destination = LocationField()


def _format_money(value: Decimal) -> str:
    return str(Decimal(value).quantize(MONEY_DECIMAL_PLACES, rounding=ROUND_HALF_UP))


def _format_price(value: Decimal) -> str:
    return str(Decimal(value).quantize(PRICE_DECIMAL_PLACES, rounding=ROUND_HALF_UP))


def serialize_route_plan_result(result: RoutePlanResult) -> dict:
    route = result.route
    vehicle = result.vehicle
    plan = result.fuel_plan

    return {
        "route": {
            "distance_miles": route.distance_miles,
            "duration_minutes": route.duration_minutes,
            "geometry": route.geometry,
        },
        "vehicle": {
            "mpg": vehicle.mpg,
            "max_range_miles": vehicle.max_range_miles,
            "tank_capacity_gallons": vehicle.tank_capacity_gallons,
            "starting_fuel_gallons": vehicle.starting_fuel_gallons,
        },
        "fuel": {
            "feasible": plan.feasible,
            "starting_fuel_gallons": plan.starting_fuel_gallons,
            "fuel_consumed_gallons": plan.fuel_consumed_gallons,
            "total_gallons_purchased": plan.total_gallons_purchased,
            "total_cost": _format_money(plan.total_cost),
        },
        "stops": [
            {
                "station_id": stop.station_id,
                "station_name": stop.station_name,
                "address": stop.address,
                "city": stop.city,
                "state": stop.state,
                "coordinates": (
                    {"latitude": stop.latitude, "longitude": stop.longitude}
                    if stop.latitude is not None and stop.longitude is not None
                    else None
                ),
                "route_position_miles": stop.route_position_miles,
                "distance_from_route_miles": stop.distance_from_route_miles,
                "detour_distance_miles": stop.detour_distance_miles,
                "detour_fuel_gallons": stop.detour_fuel_gallons,
                "price_per_gallon": _format_price(stop.price_per_gallon),
                "fuel_before_gallons": stop.fuel_before_gallons,
                "fuel_purchased_gallons": stop.fuel_purchased_gallons,
                "fuel_after_gallons": stop.fuel_after_gallons,
                "fuel_cost": _format_money(stop.fuel_cost),
            }
            for stop in plan.stops
        ],
    }
