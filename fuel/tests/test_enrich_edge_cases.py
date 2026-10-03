import csv
from pathlib import Path

import pytest
from django.core.management import call_command

from fuel.models import FuelStation

IMPORT_FIXTURE = Path(__file__).parent / "fixtures" / "sample_fuel_prices.csv"


@pytest.mark.django_db
def test_enrich_from_file_skips_malformed_coordinate_rows_safely(tmp_path):
    call_command("import_fuel_prices", str(IMPORT_FIXTURE))
    station_7 = FuelStation.objects.get(opis_id=7)
    station_9 = FuelStation.objects.get(opis_id=9)

    coords_csv = tmp_path / "station_coordinates.csv"
    with open(coords_csv, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["normalized_key", "latitude", "longitude"])
        writer.writerow([station_7.normalized_key, "not-a-number", "-95.2097"])
        writer.writerow([station_9.normalized_key, "36.1", "-95.9"])

    call_command(
        "enrich_station_coordinates", "--provider", "file", "--from-file", str(coords_csv)
    )

    station_7.refresh_from_db()
    station_9.refresh_from_db()
    # malformed row for station_7 must not crash the command and must not
    # leave a half-written coordinate
    assert station_7.latitude is None
    assert station_9.latitude == pytest.approx(36.1)


@pytest.mark.django_db
def test_enrich_from_file_is_idempotent(tmp_path):
    call_command("import_fuel_prices", str(IMPORT_FIXTURE))
    station_7 = FuelStation.objects.get(opis_id=7)

    coords_csv = tmp_path / "station_coordinates.csv"
    with open(coords_csv, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["normalized_key", "latitude", "longitude"])
        writer.writerow([station_7.normalized_key, "36.5333", "-95.2097"])

    call_command("enrich_station_coordinates", "--provider", "file", "--from-file", str(coords_csv))
    station_7.refresh_from_db()
    first_updated_at = station_7.coordinates_updated_at

    call_command("enrich_station_coordinates", "--provider", "file", "--from-file", str(coords_csv))
    station_7.refresh_from_db()

    assert station_7.latitude == pytest.approx(36.5333)
    assert station_7.longitude == pytest.approx(-95.2097)
    assert station_7.coordinates_updated_at >= first_updated_at


@pytest.mark.django_db
def test_enrich_from_file_rejects_out_of_range_coordinates(tmp_path, capsys):
    call_command("import_fuel_prices", str(IMPORT_FIXTURE))
    station_7 = FuelStation.objects.get(opis_id=7)

    coords_csv = tmp_path / "station_coordinates.csv"
    with open(coords_csv, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["normalized_key", "latitude", "longitude"])
        writer.writerow([station_7.normalized_key, "200.0", "-95.2097"])  # lat out of [-90, 90]

    call_command("enrich_station_coordinates", "--provider", "file", "--from-file", str(coords_csv))
    station_7.refresh_from_db()

    assert station_7.latitude is None
    assert "invalid_coordinates: 1" in capsys.readouterr().out


@pytest.mark.django_db
def test_enrich_from_file_rejects_nan_and_infinite_coordinates(tmp_path):
    call_command("import_fuel_prices", str(IMPORT_FIXTURE))
    station_7 = FuelStation.objects.get(opis_id=7)

    coords_csv = tmp_path / "station_coordinates.csv"
    with open(coords_csv, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["normalized_key", "latitude", "longitude"])
        writer.writerow([station_7.normalized_key, "nan", "-95.2097"])

    call_command("enrich_station_coordinates", "--provider", "file", "--from-file", str(coords_csv))
    station_7.refresh_from_db()

    assert station_7.latitude is None


@pytest.mark.django_db
def test_enrich_from_file_reports_unmatched_identity(tmp_path, capsys):
    call_command("import_fuel_prices", str(IMPORT_FIXTURE))

    coords_csv = tmp_path / "station_coordinates.csv"
    with open(coords_csv, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["normalized_key", "latitude", "longitude"])
        writer.writerow(["this-key-matches-no-station", "36.1", "-95.9"])

    call_command("enrich_station_coordinates", "--provider", "file", "--from-file", str(coords_csv))

    out = capsys.readouterr().out
    assert "unmatched_identities: 1" in out
    assert "matched_stations: 0" in out


@pytest.mark.django_db
def test_enrich_from_file_never_overwrites_valid_coordinates_with_invalid_data(tmp_path):
    call_command("import_fuel_prices", str(IMPORT_FIXTURE))
    station_7 = FuelStation.objects.get(opis_id=7)

    valid_csv = tmp_path / "valid.csv"
    with open(valid_csv, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["normalized_key", "latitude", "longitude"])
        writer.writerow([station_7.normalized_key, "36.5333", "-95.2097"])
    call_command("enrich_station_coordinates", "--provider", "file", "--from-file", str(valid_csv))

    invalid_csv = tmp_path / "invalid.csv"
    with open(invalid_csv, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["normalized_key", "latitude", "longitude"])
        writer.writerow([station_7.normalized_key, "999.0", "-95.2097"])
    call_command(
        "enrich_station_coordinates", "--provider", "file", "--from-file", str(invalid_csv)
    )

    station_7.refresh_from_db()
    # the earlier VALID coordinates must still be in place
    assert station_7.latitude == pytest.approx(36.5333)
    assert station_7.longitude == pytest.approx(-95.2097)


@pytest.mark.django_db
def test_enrich_from_file_reports_coordinates_unchanged_on_rerun(tmp_path, capsys):
    call_command("import_fuel_prices", str(IMPORT_FIXTURE))
    station_7 = FuelStation.objects.get(opis_id=7)

    coords_csv = tmp_path / "station_coordinates.csv"
    with open(coords_csv, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["normalized_key", "latitude", "longitude"])
        writer.writerow([station_7.normalized_key, "36.5333", "-95.2097"])

    call_command("enrich_station_coordinates", "--provider", "file", "--from-file", str(coords_csv))
    capsys.readouterr()  # discard first run's output

    call_command("enrich_station_coordinates", "--provider", "file", "--from-file", str(coords_csv))
    out = capsys.readouterr().out
    assert "coordinates_updated: 0" in out
    assert "coordinates_unchanged: 1" in out


@pytest.mark.django_db
def test_enrich_from_file_raises_for_missing_fixture_file():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command(
            "enrich_station_coordinates", "--provider", "file", "--from-file", "/no/such/file.csv"
        )
