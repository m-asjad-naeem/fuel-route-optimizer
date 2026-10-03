"""Canonicalization helpers for fuel station identity and price hashing.

Both `import_fuel_prices` and `enrich_station_coordinates` must derive the
same `normalized_key` for the same physical station, so this logic lives in
one place rather than being duplicated across management commands.
"""
from __future__ import annotations

import hashlib
import re
from decimal import Decimal

_WHITESPACE_RE = re.compile(r"\s+")

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}
CA_PROVINCES = {
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT",
}


def normalize_text(value: str) -> str:
    """Strip, collapse internal whitespace, uppercase — for identity fields."""
    return _WHITESPACE_RE.sub(" ", value.strip()).upper()


def normalize_display_text(value: str) -> str:
    """Strip and collapse whitespace, but preserve original casing — for
    fields kept purely for display (e.g. station name)."""
    return _WHITESPACE_RE.sub(" ", value.strip())


def country_for_state(state: str) -> str:
    state = state.strip().upper()
    if state in US_STATES:
        return "US"
    if state in CA_PROVINCES:
        return "CA"
    return "UNKNOWN"


def station_normalized_key(opis_id: str | int, address: str, city: str, state: str) -> str:
    """Deterministic station identity key.

    Deliberately excludes the station name: the source data shows the same
    physical station (same opis_id/address/city/state) recorded under
    different name strings (e.g. "PILOT TRAVEL CENTER #1243" vs
    "PILOT #1243"), so name cannot be part of identity.
    """
    parts = "|".join(
        [
            str(int(opis_id)),
            normalize_text(address),
            normalize_text(city),
            normalize_text(state),
        ]
    )
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()


PRICE_DECIMAL_PLACES = 8
_PRICE_QUANTIZE = Decimal("1e-{}".format(PRICE_DECIMAL_PLACES))


def canonicalize_price(retail_price) -> Decimal:
    """Quantize a price to the fixed precision stored in the DB, using Decimal
    arithmetic throughout (never float) so no precision is lost or corrupted
    by binary floating-point rounding. The source CSV carries up to 8 decimal
    places (e.g. 3.00733333); the schema matches that exactly.
    """
    return Decimal(str(retail_price)).quantize(_PRICE_QUANTIZE)


def canonical_price_hash(normalized_key: str, retail_price) -> str:
    """Identity hash for a (station, price) pair — NOT a literal raw-source-row
    hash. Computed from the already-canonicalized station identity plus the
    quantized price, so insignificant source formatting differences
    (whitespace, trailing zeros, a differing display name) never create
    artificial duplicate FuelPrice rows. Two source rows that canonicalize to
    the same (normalized_key, price) are the same priced observation.
    """
    canonical_price = str(canonicalize_price(retail_price))
    parts = f"{normalized_key}|{canonical_price}"
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()
