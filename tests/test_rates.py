"""The mock rate service: a local table of made-up rates, looked up by currency and date."""
import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from rates import MAX_RATE_AGE_DAYS, MockRates, Rate, seed


@pytest.fixture
def rates(tmp_path):
    seed(tmp_path / "rates.db")
    return MockRates(tmp_path / "rates.db")


def test_lookup_returns_the_latest_rate_on_or_before_the_date(rates):
    """The rate for a date is the most recent one published by then: never a later one, and none if there is nothing that early."""
    assert rates.usd_rate("EUR", date(2026, 1, 26)) == Rate("EUR", Decimal("1.0861"), date(2026, 1, 26))
    assert rates.usd_rate("EUR", date(2026, 1, 25)).as_of == date(2026, 1, 19)     # the 26th's rate isn't out yet
    assert rates.usd_rate("EUR", date(2026, 1, 11)) is None                        # before the first rate
    assert rates.usd_rate("EUR").as_of == date(2026, 1, 26)                        # no date: the latest
    assert rates.usd_rate("eur", date(2026, 1, 26)).currency == "EUR"              # case doesn't matter


def test_an_unknown_currency_has_no_rate(rates):
    """A currency the table doesn't carry returns None, never a guess."""
    assert rates.usd_rate("CHF", date(2026, 1, 26)) is None and rates.usd_rate("XXX") is None


def test_a_rate_converts_to_the_cent_and_knows_its_age():
    """4,125.00 EUR at 1.0861 is $4,480.16; a rate's age is counted from the date it is wanted for."""
    rate = Rate("EUR", Decimal("1.0861"), date(2026, 1, 26))
    assert rate.to_usd(Decimal("4125.00")) == Decimal("4480.16")
    assert rate.age_days(date(2026, 2, 5)) == 10 > MAX_RATE_AGE_DAYS and rate.age_days(None) is None


def test_seeding_is_repeatable_and_the_service_never_creates_a_file(tmp_path):
    """seed() can run twice; MockRates opens read-only, so a wrong path is an error, not a new empty database."""
    path = tmp_path / "rates.db"
    seed(path)
    seed(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*), COUNT(DISTINCT currency) FROM rates").fetchone() == (12, 4)
    missing = tmp_path / "typo.db"
    with pytest.raises(sqlite3.OperationalError):
        MockRates(missing)
    assert not missing.exists()
