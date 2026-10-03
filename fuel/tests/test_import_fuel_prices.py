from pathlib import Path

import pytest
from django.core.management import call_command

from fuel.models import FuelPrice, FuelStation

FIXTURE = Path(__file__).parent / "fixtures" / "sample_fuel_prices.csv"


@pytest.mark.django_db
def test_import_creates_expected_stations_and_prices():
    call_command("import_fuel_prices", str(FIXTURE))

    # 5 distinct physical stations: opis 7, 9, 20, 105, 128
    assert FuelStation.objects.count() == 5

    # 8 rows in, 1 exact (station, price) duplicate (opis 20) collapses -> 7 FuelPrice rows
    assert FuelPrice.objects.count() == 7


@pytest.mark.django_db
def test_duplicate_opis_id_collapses_to_one_station_with_different_names():
    call_command("import_fuel_prices", str(FIXTURE))

    station = FuelStation.objects.get(opis_id=20)
    # only one FuelStation despite two rows with different name strings
    assert FuelStation.objects.filter(opis_id=20).count() == 1
    # last-seen display name is retained; identity didn't depend on it
    assert station.name in {"PILOT TRAVEL CENTER #1243", "PILOT #1243"}


@pytest.mark.django_db
def test_exact_duplicate_row_does_not_create_two_prices():
    call_command("import_fuel_prices", str(FIXTURE))

    station = FuelStation.objects.get(opis_id=20)
    assert station.prices.count() == 1
    assert float(station.prices.first().retail_price) == pytest.approx(3.899)


@pytest.mark.django_db
def test_effective_price_is_minimum_of_distinct_prices():
    call_command("import_fuel_prices", str(FIXTURE))

    station_105 = FuelStation.objects.get(opis_id=105)
    assert station_105.prices.count() == 2
    assert float(station_105.effective_price_per_gallon) == pytest.approx(3.269)

    station_128 = FuelStation.objects.get(opis_id=128)
    assert float(station_128.effective_price_per_gallon) == pytest.approx(3.349)


@pytest.mark.django_db
def test_whitespace_variant_city_normalizes_to_same_station():
    call_command("import_fuel_prices", str(FIXTURE))

    # opis 128's two rows differ only by trailing whitespace in City —
    # must resolve to the same FuelStation, not two.
    assert FuelStation.objects.filter(opis_id=128).count() == 1


@pytest.mark.django_db
def test_import_is_idempotent_on_rerun():
    call_command("import_fuel_prices", str(FIXTURE))
    call_command("import_fuel_prices", str(FIXTURE))

    assert FuelStation.objects.count() == 5
    assert FuelPrice.objects.count() == 7


@pytest.mark.django_db
def test_single_price_station_effective_price_equals_its_only_price():
    call_command("import_fuel_prices", str(FIXTURE))

    station = FuelStation.objects.get(opis_id=7)
    assert float(station.effective_price_per_gallon) == pytest.approx(3.00733333)
