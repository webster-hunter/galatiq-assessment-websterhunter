"""What has been seen and what has been paid, across files and across runs.

    ledger.py   Ledger: one SQLite file holding every invoice processed and every payment made
    payment.py  mock_payment (the brief's function) and the key that makes paying idempotent

The checks look at one invoice at a time. The ledger is the only part that compares invoices
with each other: the same invoice arriving twice, a revision of one already handled.
"""
from .ledger import Ledger, Seen
from .payment import mock_payment, payment_key

__all__ = ["Ledger", "Seen", "mock_payment", "payment_key"]
