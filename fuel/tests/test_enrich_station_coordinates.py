import csv
from pathlib import Path

import pytest
from django.core.management import call_command

from fuel.models import FuelStation
from fuel.normalization import station_normalized_key

IMPORT_FIXTURE = Path(__file__).parent / "fixtures" / "sample_fuel_prices.csv"


@pytest.mark.django_db
def test_enrich_from_file_updates_matching_stations(tmp_path):
    call_command("import_fuel_prices", str(IMPORT_FIXTURE))

    station_7 = FuelStation.objects.get(opis_id=7)
    station_9 = FuelStation.objects.get(opis_id=9)

    coords_csv = tmp_path / "station_coordinates.csv"
    with open(coords_csv, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["normalized_key", "latitude", "longitude"])
        writer.writerow([station_7.normalized_key, "36.5333", "-95.2097"])
        # station_9 intentionally omitted from the fixture

    call_command(
        "enrich_station_coordinates", "--provider", "file", "--from-file", str(coords_csv)
    )

    station_7.refresh_from_db()
    station_9.refresh_from_db()

    assert station_7.latitude == pytest.approx(36.5333)
    assert station_7.longitude == pytest.approx(-95.2097)
    assert station_7.coordinate_source == "precomputed_file"
    assert station_7.coordinates_updated_at is not None

    # station absent from the fixture is left unenriched, not errored
    assert station_9.latitude is None


@pytest.mark.django_db
def test_enrich_from_file_requires_from_file_argument():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("enrich_station_coordinates", "--provider", "file")


def test_station_normalized_key_is_stable_for_whitespace_variants():
    key_a = station_normalized_key(128, "I-57 & I-70, EXIT 159", "Effingham", "IL")
    key_b = station_normalized_key(
        128, "I-57 & I-70, EXIT 159", "Effingham                               ", "IL"
    )
    assert key_a == key_b
