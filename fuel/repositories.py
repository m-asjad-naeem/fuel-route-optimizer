"""Thin Django ORM adapter between FuelStation and the pure geospatial
service. Contains no geospatial math — it only loads rows and maps them to
the framework-independent StationLocation type.
"""
from __future__ import annotations

from fuel.models import FuelStation
from routes.domain.types import StationLocation


def load_enriched_stations() -> list[StationLocation]:
    """All stations with persisted coordinates and a known effective price.

    Filtering on missing/null fields happens here at the SQL level (cheap,
    indexed) as a first pass; the geospatial service independently
    validates (range checks, NaN, etc.) whatever it's handed, since it must
    not assume the only caller is this adapter.
    """
    rows = FuelStation.objects.filter(
        latitude__isnull=False,
        longitude__isnull=False,
        effective_price_per_gallon__isnull=False,
    ).values_list(
        "id", "latitude", "longitude", "effective_price_per_gallon",
        "name", "address", "city", "state",
    )

    return [
        StationLocation(
            station_id=station_id,
            latitude=latitude,
            longitude=longitude,
            price_per_gallon=price_per_gallon,
            name=name,
            address=address,
            city=city,
            state=state,
        )
        for station_id, latitude, longitude, price_per_gallon, name, address, city, state in rows
    ]
