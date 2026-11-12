"""Paying, and the key that stops it happening twice."""
import sys


def mock_payment(vendor, amount, currency="USD"):
    """The brief's mock payment API, with two changes.

    It reports on stderr: the brief's version prints to stdout, which here carries the JSON
    results. And it takes an optional currency, so an invoice billed in euros can be paid in
    euros. Called with two arguments, as in the brief, it pays dollars and prints the same line.
    """
    print(f"Paid {amount} to {vendor}" if currency == "USD" else f"Paid {amount} {currency} to {vendor}", file=sys.stderr)
    return {"status": "success"}


def payment_key(vendor_key: str, number_key: str, revision: int) -> str:
    """The idempotency key for paying one invoice: the same invoice always gives the same key.

    A real payment API takes a key like this and refuses a second request that carries it. The
    ledger does the same here: it holds at most one payment per key, so running a file again, or
    a pipeline step being repeated after a crash, cannot pay twice.
    """
    return f"{vendor_key}/{number_key}/r{revision}"
