import csv
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command

from fuel.models import FuelStation
from routes.tests.helpers import make_response

IMPORT_FIXTURE = Path(__file__).parent / "fixtures" / "sample_fuel_prices.csv"


def nominatim_ok(lat, lon):
    return make_response(200, json_body=[{"lat": str(lat), "lon": str(lon)}])


@pytest.mark.django_db
def test_nominatim_path_requires_out_file():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("enrich_station_coordinates", "--provider", "nominatim")


@pytest.mark.django_db
@patch("fuel.coordinate_providers.requests.get")
def test_nominatim_path_writes_fixture_not_database(mock_get, tmp_path):
    call_command("import_fuel_prices", str(IMPORT_FIXTURE))
    mock_get.return_value = nominatim_ok(36.5, -95.2)

    out_file = tmp_path / "station_coordinates.csv"
    call_command(
        "enrich_station_coordinates",
        "--provider", "nominatim",
        "--out-file", str(out_file),
        "--limit", "1",
    )

    # the fixture file was written...
    assert out_file.exists()
    with open(out_file) as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    assert rows[0]["coordinate_source"] == "nominatim"

    # ...but FuelStation itself was NOT touched — this path never writes
    # to the database directly, only to the fixture file.
    assert FuelStation.objects.filter(latitude__isnull=False).count() == 0


@pytest.mark.django_db
@patch("fuel.coordinate_providers.requests.get")
def test_nominatim_path_respects_limit(mock_get, tmp_path):
    call_command("import_fuel_prices", str(IMPORT_FIXTURE))
    mock_get.return_value = nominatim_ok(36.5, -95.2)

    out_file = tmp_path / "station_coordinates.csv"
    call_command(
        "enrich_station_coordinates",
        "--provider", "nominatim",
        "--out-file", str(out_file),
        "--limit", "2",
    )

    with open(out_file) as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2


@pytest.mark.django_db
@patch("fuel.coordinate_providers.requests.get")
def test_nominatim_path_resumes_skipping_already_geocoded_keys(mock_get, tmp_path):
    call_command("import_fuel_prices", str(IMPORT_FIXTURE))
    out_file = tmp_path / "station_coordinates.csv"

    mock_get.return_value = nominatim_ok(36.5, -95.2)
    call_command(
        "enrich_station_coordinates",
        "--provider", "nominatim",
        "--out-file", str(out_file),
        "--limit", "1",
    )
    with open(out_file) as fh:
        first_run_keys = {row["normalized_key"] for row in csv.DictReader(fh)}
    assert len(first_run_keys) == 1

    # second run with a higher limit must not re-request the already-done key
    mock_get.reset_mock()
    mock_get.return_value = nominatim_ok(37.0, -96.0)
    call_command(
        "enrich_station_coordinates",
        "--provider", "nominatim",
        "--out-file", str(out_file),
        "--limit", "1",
    )

    with open(out_file) as fh:
        all_rows = list(csv.DictReader(fh))
    all_keys = {row["normalized_key"] for row in all_rows}
    assert len(all_rows) == 2
    assert first_run_keys.issubset(all_keys)
    # the originally-geocoded row's coordinates must be untouched (no
    # duplicate/overwritten row for the same key)
    first_key = next(iter(first_run_keys))
    matching = [r for r in all_rows if r["normalized_key"] == first_key]
    assert len(matching) == 1


@pytest.mark.django_db
@patch("fuel.coordinate_providers.requests.get")
def test_nominatim_path_filters_by_states(mock_get, tmp_path):
    call_command("import_fuel_prices", str(IMPORT_FIXTURE))
    mock_get.return_value = nominatim_ok(36.5, -95.2)

    out_file = tmp_path / "station_coordinates.csv"
    call_command(
        "enrich_station_coordinates",
        "--provider", "nominatim",
        "--out-file", str(out_file),
        "--states", "TX",  # none of the sample fixture's stations are in TX
    )

    with open(out_file) as fh:
        rows = list(csv.DictReader(fh))
    assert rows == []
    assert mock_get.call_count == 0


@pytest.mark.django_db
@patch("fuel.coordinate_providers.requests.get")
def test_nominatim_path_rejects_out_of_range_result(mock_get, tmp_path):
    call_command("import_fuel_prices", str(IMPORT_FIXTURE))
    mock_get.return_value = make_response(200, json_body=[{"lat": "200.0", "lon": "-95.2"}])

    out_file = tmp_path / "station_coordinates.csv"
    call_command(
        "enrich_station_coordinates",
        "--provider", "nominatim",
        "--out-file", str(out_file),
        "--limit", "1",
    )

    with open(out_file) as fh:
        rows = list(csv.DictReader(fh))
    assert rows == []  # invalid result never written to the fixture


@pytest.mark.django_db
@patch("fuel.coordinate_providers.requests.get")
def test_identical_address_is_geocoded_once_and_reused(mock_get, tmp_path):
    # Two different OPIS ids at the EXACT same (address, city, state) — the
    # CSV's own documented duplication (same physical station, different
    # rack/brand row) — should only need one Nominatim request between them.
    # This is request reuse for a genuinely identical location, never a
    # city-level approximation substituted across distinct addresses.
    same_address_csv = tmp_path / "same_address.csv"
    same_address_csv.write_text(
        "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
        "901,STATION A,123 Main St,Springfield,IL,100,3.000\n"
        "902,STATION B,123 Main St,Springfield,IL,200,3.100\n"
    )
    call_command("import_fuel_prices", str(same_address_csv))
    mock_get.return_value = nominatim_ok(39.78, -89.65)

    out_file = tmp_path / "station_coordinates.csv"
    call_command(
        "enrich_station_coordinates",
        "--provider", "nominatim",
        "--out-file", str(out_file),
    )

    assert mock_get.call_count == 1
    with open(out_file) as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2
    assert {r["coordinate_source"] for r in rows} == {"nominatim"}
    assert rows[0]["latitude"] == rows[1]["latitude"]
    assert rows[0]["longitude"] == rows[1]["longitude"]


@pytest.mark.django_db
@patch("fuel.coordinate_providers.requests.get")
def test_distinct_addresses_each_get_their_own_attempt(mock_get, tmp_path):
    # Two stations with DIFFERENT addresses in the same city must each get
    # their own full-address attempt — never silently given the same
    # coordinates just because they share a city.
    two_stations_csv = tmp_path / "two_stations.csv"
    two_stations_csv.write_text(
        "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
        "901,STATION A,I-99 EXIT 1,Springfield,IL,100,3.000\n"
        "902,STATION B,I-99 EXIT 2,Springfield,IL,100,3.100\n"
    )
    call_command("import_fuel_prices", str(two_stations_csv))
    mock_get.side_effect = [nominatim_ok(39.70, -89.60), nominatim_ok(39.80, -89.70)]

    out_file = tmp_path / "station_coordinates.csv"
    call_command(
        "enrich_station_coordinates",
        "--provider", "nominatim",
        "--out-file", str(out_file),
    )

    assert mock_get.call_count == 2
    with open(out_file) as fh:
        rows = {r["normalized_key"]: r for r in csv.DictReader(fh)}
    assert len(rows) == 2
    lats = {r["latitude"] for r in rows.values()}
    assert lats == {"39.7", "39.8"}  # each kept its own distinct result


@pytest.mark.django_db
@patch("fuel.coordinate_providers.requests.get")
def test_city_fallback_is_cached_across_stations_in_the_same_city(mock_get, tmp_path):
    # Two stations with different addresses, both of which fail to resolve
    # as a literal address: each gets its own full-address attempt (which
    # fails), but the resulting city/state fallback is requested only once
    # and reused — not re-requested per station.
    two_stations_csv = tmp_path / "two_stations.csv"
    two_stations_csv.write_text(
        "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
        "901,STATION A,I-99 EXIT 1,Springfield,IL,100,3.000\n"
        "902,STATION B,I-99 EXIT 2,Springfield,IL,100,3.100\n"
    )
    call_command("import_fuel_prices", str(two_stations_csv))
    no_match = make_response(200, json_body=[])
    city_hit = nominatim_ok(39.78, -89.65)
    # station A: address fails, city fallback succeeds (2 calls);
    # station B: address fails, city fallback is cached (1 call)
    mock_get.side_effect = [no_match, city_hit, no_match]

    out_file = tmp_path / "station_coordinates.csv"
    call_command(
        "enrich_station_coordinates",
        "--provider", "nominatim",
        "--out-file", str(out_file),
    )

    assert mock_get.call_count == 3
    with open(out_file) as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2
    assert {r["coordinate_source"] for r in rows} == {"nominatim_city_fallback"}
    assert rows[0]["latitude"] == rows[1]["latitude"]


@pytest.mark.django_db
@patch("fuel.coordinate_providers.requests.get")
def test_resume_skips_fully_covered_addresses(mock_get, tmp_path):
    same_address_csv = tmp_path / "same_address.csv"
    same_address_csv.write_text(
        "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
        "901,STATION A,123 Main St,Springfield,IL,100,3.000\n"
        "902,STATION B,123 Main St,Springfield,IL,200,3.100\n"
    )
    call_command("import_fuel_prices", str(same_address_csv))
    mock_get.return_value = nominatim_ok(39.78, -89.65)

    out_file = tmp_path / "station_coordinates.csv"
    call_command(
        "enrich_station_coordinates", "--provider", "nominatim", "--out-file", str(out_file),
    )
    assert mock_get.call_count == 1

    mock_get.reset_mock()
    call_command(
        "enrich_station_coordinates", "--provider", "nominatim", "--out-file", str(out_file),
    )
    assert mock_get.call_count == 0
