"""Paying, and the key that stops it happening twice."""
import sys


def mock_payment(vendor, amount):
    """The brief's mock payment API, unchanged except that it reports on stderr.

    The brief's version prints to stdout; here stdout carries the JSON results, so the line goes
    to stderr instead.
    """
    print(f"Paid {amount} to {vendor}", file=sys.stderr)
    return {"status": "success"}


def payment_key(vendor_key: str, number_key: str, revision: int) -> str:
    """The idempotency key for paying one invoice: the same invoice always gives the same key.

    A real payment API takes a key like this and refuses a second request that carries it. The
    ledger does the same here: it holds at most one payment per key, so running a file again, or
    a pipeline step being repeated after a crash, cannot pay twice.
    """
    return f"{vendor_key}/{number_key}/r{revision}"
