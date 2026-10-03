import csv
import uuid
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Min
from django.utils import timezone

from fuel.models import FuelPrice, FuelStation
from fuel.normalization import (
    canonical_price_hash,
    canonicalize_price,
    country_for_state,
    normalize_display_text,
    normalize_text,
    station_normalized_key,
)

REQUIRED_COLUMNS = {
    "OPIS Truckstop ID",
    "Truckstop Name",
    "Address",
    "City",
    "State",
    "Rack ID",
    "Retail Price",
}


class Command(BaseCommand):
    help = "Import fuel station prices from the supplied OPIS-format CSV."

    def add_arguments(self, parser):
        parser.add_argument("csv_path", type=str)
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
            help="Number of FuelPrice rows to bulk_create per batch (default: 500).",
        )

    def handle(self, csv_path: str, batch_size: int, **options):
        stats = {
            "rows_read": 0,
            "rows_invalid": 0,
            "stations_created": 0,
            "stations_seen": 0,
            "prices_created": 0,
            "prices_duplicate_skipped": 0,
            "stations_with_multiple_prices": 0,
        }

        try:
            fh = open(csv_path, newline="", encoding="utf-8-sig")
        except OSError as exc:
            raise CommandError(f"Could not open {csv_path}: {exc}") from exc

        import_batch = uuid.uuid4().hex

        with fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None or not REQUIRED_COLUMNS.issubset(
                set(reader.fieldnames)
            ):
                raise CommandError(
                    f"CSV is missing required columns. Found: {reader.fieldnames}"
                )

            station_cache: dict[str, FuelStation] = {}
            touched_station_keys: set[str] = set()

            with transaction.atomic():
                for row in reader:
                    stats["rows_read"] += 1
                    station, price, error = self._parse_row(row)
                    if error:
                        stats["rows_invalid"] += 1
                        self.stderr.write(self.style.WARNING(f"Skipping row: {error}"))
                        continue

                    normalized_key = station["normalized_key"]
                    touched_station_keys.add(normalized_key)

                    fuel_station = station_cache.get(normalized_key)
                    if fuel_station is None:
                        fuel_station, created = FuelStation.objects.get_or_create(
                            normalized_key=normalized_key,
                            defaults={
                                "opis_id": station["opis_id"],
                                "name": station["name"],
                                "address": station["address"],
                                "city": station["city"],
                                "state": station["state"],
                                "country": station["country"],
                                "rack_id": station["rack_id"],
                            },
                        )
                        if created:
                            stats["stations_created"] += 1
                        stats["stations_seen"] += 1
                        station_cache[normalized_key] = fuel_station

                    row_hash = canonical_price_hash(normalized_key, price)
                    _, price_created = FuelPrice.objects.get_or_create(
                        canonical_price_hash=row_hash,
                        defaults={
                            "station": fuel_station,
                            "retail_price": price,
                            "import_batch": import_batch,
                        },
                    )
                    if price_created:
                        stats["prices_created"] += 1
                    else:
                        stats["prices_duplicate_skipped"] += 1

                # Recompute the effective (minimum) price for every station touched
                # by this import run — done once per station, not per row.
                now = timezone.now()
                stations_to_update = FuelStation.objects.filter(
                    normalized_key__in=touched_station_keys
                )
                for fuel_station in stations_to_update:
                    agg = fuel_station.prices.aggregate(min_price=Min("retail_price"))
                    distinct_prices = fuel_station.prices.values_list(
                        "retail_price", flat=True
                    ).distinct()
                    if len(set(distinct_prices)) > 1:
                        stats["stations_with_multiple_prices"] += 1
                    fuel_station.effective_price_per_gallon = agg["min_price"]
                    fuel_station.effective_price_updated_at = now
                    fuel_station.save(
                        update_fields=[
                            "effective_price_per_gallon",
                            "effective_price_updated_at",
                        ]
                    )

        self._report(stats)

    def _parse_row(self, row: dict) -> tuple[dict | None, Decimal | None, str | None]:
        try:
            opis_id = int(row["OPIS Truckstop ID"].strip())
        except (ValueError, AttributeError):
            return None, None, f"invalid OPIS Truckstop ID: {row.get('OPIS Truckstop ID')!r}"

        address = normalize_display_text(row["Address"])
        city = normalize_display_text(row["City"])
        state = normalize_text(row["State"])
        name = normalize_display_text(row["Truckstop Name"])

        if not state:
            return None, None, f"missing State for OPIS ID {opis_id}"

        try:
            rack_id = int(row["Rack ID"].strip()) if row["Rack ID"].strip() else None
        except ValueError:
            rack_id = None

        try:
            price = canonicalize_price(row["Retail Price"].strip())
        except (InvalidOperation, AttributeError):
            bad_value = row.get("Retail Price")
            return None, None, f"invalid Retail Price for OPIS ID {opis_id}: {bad_value!r}"

        if price <= 0:
            return None, None, f"non-positive Retail Price for OPIS ID {opis_id}: {price}"

        normalized_key = station_normalized_key(opis_id, address, city, state)

        station = {
            "opis_id": opis_id,
            "name": name,
            "address": address,
            "city": city,
            "state": state,
            "country": country_for_state(state),
            "rack_id": rack_id,
            "normalized_key": normalized_key,
        }
        return station, price, None

    def _report(self, stats: dict):
        self.stdout.write(self.style.SUCCESS("Import complete:"))
        for key, value in stats.items():
            self.stdout.write(f"  {key}: {value}")
