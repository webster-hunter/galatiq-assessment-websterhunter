"""A mock exchange-rate service backed by a local SQLite table.

The table holds, per currency, how many US dollars one unit is worth on a given date:

    currency  as_of        usd_per_unit
    EUR       2026-01-26   1.0861        (1 EUR = 1.0861 USD)

A lookup returns the most recent rate on or before the date asked for. Rates are published for
past dates, so a rate dated after the invoice is never used for it.
"""
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Protocol

# Made-up rates for the weeks the sample invoices fall in. NOT real market quotes: they exist so
# the prototype has something to look up, exactly as the inventory seed is invented stock.
SEED = [
    ("EUR", "2026-01-12", "1.0842"), ("EUR", "2026-01-19", "1.0850"), ("EUR", "2026-01-26", "1.0861"),
    ("GBP", "2026-01-12", "1.2690"), ("GBP", "2026-01-19", "1.2700"), ("GBP", "2026-01-26", "1.2725"),
    ("CAD", "2026-01-12", "0.7410"), ("CAD", "2026-01-19", "0.7400"), ("CAD", "2026-01-26", "0.7385"),
    ("JPY", "2026-01-12", "0.0066"), ("JPY", "2026-01-19", "0.0066"), ("JPY", "2026-01-26", "0.0067"),
]


@dataclass(frozen=True)
class Rate:
    """One exchange rate: 1 unit of `currency` is `usd_per_unit` dollars, as published on `as_of`."""
    currency: str
    usd_per_unit: Decimal
    as_of: date
    source: str = "mock"

    def to_usd(self, amount: Decimal) -> Decimal:
        """The amount in dollars at this rate, rounded to the cent."""
        return (amount * self.usd_per_unit).quantize(Decimal("0.01"))

    def age_days(self, on: date | None) -> int | None:
        """How many days before `on` this rate was published; None if there is no date to measure from."""
        return (on - self.as_of).days if on else None


class RateService(Protocol):
    """What the rest of the system needs from a rate service: one lookup."""

    def usd_rate(self, currency: str, on: date | None = None) -> Rate | None: ...


def seed(db_path: str | Path = "rates.db") -> None:
    """Create or reset the rates table with the made-up SEED rates. Safe to re-run."""
    with closing(sqlite3.connect(str(db_path))) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS rates (currency TEXT NOT NULL, as_of TEXT NOT NULL, "
                     "usd_per_unit TEXT NOT NULL, PRIMARY KEY (currency, as_of))")
        conn.executemany("INSERT OR REPLACE INTO rates VALUES (?, ?, ?)", SEED)


class MockRates:
    """Rates read from a local SQLite table (see seed()). The file is opened read-only.

    Args:
        db_path: The rates database.

    Raises:
        sqlite3.OperationalError: The file is missing (it is never created here) or has no rates table.
    """

    def __init__(self, db_path: str | Path = "rates.db"):
        self._uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(self._uri, uri=True)) as conn:
            conn.execute("SELECT 1 FROM rates LIMIT 1")  # fail now, not at the first lookup

    def usd_rate(self, currency: str, on: date | None = None) -> Rate | None:
        """The most recent rate for `currency` on or before `on` (the latest of all if `on` is None).

        Returns:
            The Rate, or None if the currency isn't in the table or has no rate that early.
            Whether a rate is too old to use is the caller's decision (normalizer.checks.usable_rate).
        """
        sql = "SELECT as_of, usd_per_unit FROM rates WHERE currency=?" + (" AND as_of<=?" if on else "") + \
              " ORDER BY as_of DESC LIMIT 1"
        with closing(sqlite3.connect(self._uri, uri=True)) as conn:
            row = conn.execute(sql, (currency.upper(), on.isoformat()) if on else (currency.upper(),)).fetchone()
        return Rate(currency.upper(), Decimal(row[1]), date.fromisoformat(row[0])) if row else None
