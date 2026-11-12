"""Exchange rates: a mock rate service, standing in for a real one.

    service.py   Rate, the RateService interface, and MockRates (rates from a local SQLite table)

The brief rules out internet access, so, like the inventory database and mock_payment, the rate
service is simulated locally. Everything that needs a rate asks for it through one method,
usd_rate(currency, on). A real service is one more class with that method. This package imports
nothing else from the project.
"""
from .service import MockRates, Rate, RateService, seed

__all__ = ["MockRates", "Rate", "RateService", "seed"]
