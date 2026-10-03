from django.db import models


class FuelStation(models.Model):
    """A physical fuel station, deduplicated from the source CSV.

    `normalized_key` — not `opis_id` — is the station's identity. The source
    data proves OPIS Truckstop ID is not unique (see docs/csv-findings.md),
    and the same physical station can appear under slightly different name
    strings, so identity is derived from (opis_id, address, city, state)
    after normalization instead.
    """

    opis_id = models.IntegerField(db_index=True)
    normalized_key = models.CharField(max_length=255, unique=True, db_index=True)

    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2)
    country = models.CharField(max_length=2)
    rack_id = models.IntegerField(null=True, blank=True)

    # Populated only by `enrich_station_coordinates` — never by the runtime API.
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    coordinate_source = models.CharField(max_length=32, null=True, blank=True)
    coordinates_updated_at = models.DateTimeField(null=True, blank=True)

    # Denormalized "effective optimization price": the minimum FuelPrice
    # recorded for this station, recomputed at import time. The dataset has
    # no fuel-grade or effective-date column, so "minimum supplied price" is
    # a documented assumption for this assessment, not a claim about what
    # the duplicate rows represent.
    effective_price_per_gallon = models.DecimalField(
        max_digits=11, decimal_places=8, null=True, blank=True
    )
    effective_price_updated_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["latitude", "longitude"]),
            models.Index(fields=["state"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state})"


class FuelPrice(models.Model):
    """One priced observation of a station, as read from the source CSV.

    `canonical_price_hash` identifies this (station, price) pair, computed
    from CANONICALIZED fields (after whitespace stripping and case
    normalization) — not a literal hash of the raw CSV row. Two source rows
    for the same station identity and price (even with a different name
    string or formatting) canonicalize to the same hash and collapse into one
    FuelPrice, which is what makes re-running the import idempotent.
    """

    station = models.ForeignKey(
        FuelStation, related_name="prices", on_delete=models.CASCADE
    )
    retail_price = models.DecimalField(max_digits=11, decimal_places=8)
    canonical_price_hash = models.CharField(max_length=64, unique=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    import_batch = models.CharField(max_length=64)

    class Meta:
        indexes = [models.Index(fields=["station"])]

    def __str__(self) -> str:
        return f"{self.station_id}: {self.retail_price}"
