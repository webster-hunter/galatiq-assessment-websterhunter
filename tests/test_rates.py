"""The mock rate service: a local table of made-up rates, looked up by currency and date."""
import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from normalizer.checks import MAX_RATE_AGE_DAYS, usable_rate
from rates import MockRates, Rate, seed


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


def test_an_undated_invoice_is_priced_as_of_today_so_an_old_rate_is_caught(rates):
    """With no invoice date the rate is judged against today, not taken on trust: fresh on the day it was published, stale months later."""
    rate, problem = usable_rate(rates, "EUR", None, today=date(2026, 1, 28))
    assert (rate.as_of, problem) == (date(2026, 1, 26), None)
    rate, problem = usable_rate(rates, "EUR", None, today=date(2026, 11, 12))
    assert (rate.as_of, problem) == (date(2026, 1, 26), "STALE_RATE")
    assert usable_rate(rates, "EUR", date(2026, 1, 26), today=date(2026, 11, 12)) == (rate, None)   # a dated invoice uses its own date


def test_usable_rate_names_each_reason(rates):
    """No service, no rate, a stale rate and a failing service each come back as a reason, never as an exception."""
    class Down:
        def usd_rate(self, currency, on=None):
            raise ConnectionError("unreachable")

    assert usable_rate(None, "EUR", date(2026, 1, 26)) == (None, "NO_RATE_SERVICE")
    assert usable_rate(rates, "CHF", date(2026, 1, 26)) == (None, "NO_RATE")
    assert usable_rate(rates, "EUR", date(2026, 2, 20))[1] == "STALE_RATE"
    assert usable_rate(Down(), "EUR", date(2026, 1, 26)) == (None, "RATE_SERVICE_ERROR: ConnectionError: unreachable")


def test_the_rates_package_stands_alone():
    """rates/ imports nothing else from this project, so a real rate service can replace it without touching the checks."""
    import ast
    from pathlib import Path
    ours = {"normalizer", "extraction", "approval", "ledger", "pipeline"}
    for path in (Path(__file__).resolve().parent.parent / "rates").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else []
            assert not any(n.split(".")[0] in ours for n in names), (path.name, names)
