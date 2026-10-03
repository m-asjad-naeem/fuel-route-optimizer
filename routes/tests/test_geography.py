import pytest

from routes.services.geography import is_within_usa

TORONTO = (43.6532, -79.3832)
MONTERREY = (25.6866, -100.3161)
OKLAHOMA_CITY = (35.4730, -97.5171)
HONOLULU = (21.3099, -157.8581)
ANCHORAGE = (61.2181, -149.9003)
MID_ATLANTIC_OCEAN = (35.0, -40.0)
MID_PACIFIC_NEAR_US_LATITUDE = (35.0, -140.0)


@pytest.mark.parametrize(
    "lat, lon",
    [
        TORONTO,
        MONTERREY,
        MID_ATLANTIC_OCEAN,
        MID_PACIFIC_NEAR_US_LATITUDE,
    ],
)
def test_non_us_points_are_rejected(lat, lon):
    # Both Toronto and Monterrey fall inside the OLD rectangular bounding
    # box (lat 24-50, lon -125..-66) that this polygon check replaces —
    # confirming the old check was a real gap, not just a style preference.
    assert is_within_usa(lat, lon) is False


@pytest.mark.parametrize("lat, lon", [OKLAHOMA_CITY, HONOLULU, ANCHORAGE])
def test_us_points_are_accepted(lat, lon):
    assert is_within_usa(lat, lon) is True


def test_point_well_outside_bounds_is_rejected_cheaply():
    assert is_within_usa(0.0, 0.0) is False
