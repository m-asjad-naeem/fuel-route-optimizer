from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command

from fuel.models import FuelPrice, FuelStation

FIXTURE = Path(__file__).parent / "fixtures" / "sample_fuel_prices.csv"


@pytest.mark.django_db
def test_import_rolls_back_entirely_on_unexpected_mid_run_error():
    """The whole import runs inside one transaction.atomic() block — an
    unexpected failure partway through (not a malformed-row case, which is
    already handled gracefully by _parse_row) must leave zero rows behind,
    not a partially-imported dataset."""
    original_get_or_create = FuelPrice.objects.get_or_create
    call_count = {"n": 0}

    def flaky_get_or_create(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 4:
            raise RuntimeError("simulated unexpected failure mid-import")
        return original_get_or_create(*args, **kwargs)

    with patch.object(FuelPrice.objects, "get_or_create", side_effect=flaky_get_or_create):
        with pytest.raises(RuntimeError):
            call_command("import_fuel_prices", str(FIXTURE))

    # Rows 1-3 were successfully processed before the simulated failure on
    # the 4th FuelPrice write — the whole run must roll back regardless.
    assert FuelStation.objects.count() == 0
    assert FuelPrice.objects.count() == 0
