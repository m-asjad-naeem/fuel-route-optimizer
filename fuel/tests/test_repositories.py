from decimal import Decimal

import pytest

from fuel.models import FuelStation
from fuel.repositories import load_enriched_stations


def make_station(**overrides):
    defaults = {
        "opis_id": 1,
        "normalized_key": "key",
        "name": "Test Station",
        "address": "123 Main St",
        "city": "Testville",
        "state": "OK",
        "country": "US",
    }
    defaults.update(overrides)
    return FuelStation.objects.create(**defaults)


@pytest.mark.django_db
def test_load_enriched_stations_returns_only_fully_enriched_rows():
    enriched = make_station(
        normalized_key="enriched",
        latitude=36.5,
        longitude=-95.2,
        effective_price_per_gallon=Decimal("3.50000000"),
    )
    make_station(normalized_key="no-coords", effective_price_per_gallon=Decimal("3.50000000"))
    make_station(normalized_key="no-price", latitude=36.5, longitude=-95.2)

    results = load_enriched_stations()

    assert len(results) == 1
    assert results[0].station_id == enriched.id
    assert results[0].latitude == pytest.approx(36.5)
    assert results[0].longitude == pytest.approx(-95.2)
    assert results[0].price_per_gallon == Decimal("3.50000000")
    assert results[0].name == "Test Station"
    assert results[0].address == "123 Main St"
    assert results[0].city == "Testville"
    assert results[0].state == "OK"


@pytest.mark.django_db
def test_load_enriched_stations_returns_empty_list_when_none_qualify():
    make_station(normalized_key="bare")
    assert load_enriched_stations() == []
