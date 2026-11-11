"""The ledger: every invoice processed and every payment made, in one SQLite file.

An invoice is identified by three things:

    vendor    its name, lower-cased, letters and digits only     "Widgets Inc." -> "widgetsinc"
    number    letters and digits only, without a leading INV     "INV 1012", "INV-1012" -> "1012"
    revision  the number in its revision label, 0 for none       "R1" -> 1

The vendor is part of the identity because two vendors can both issue an invoice 1001.

Whether two files are the same invoice is decided by content, not by file name or bytes: a hash
of the vendor, number, revision, total and each line's item, quantity and unit price. So a PDF
and a TXT of one invoice match although the PDF leaves out the subtotal line.
"""
import hashlib
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from normalizer.models import Invoice

from .payment import mock_payment, payment_key

# What the ledger knows about an invoice it has been asked about. See Ledger.seen().
NEW = "NEW"                              # never seen
SAME_FILE = "SAME_FILE"                  # this very file was processed before
DUPLICATE = "DUPLICATE"                  # another file held the same invoice, same content
CONFLICT = "CONFLICT"                    # another file used the same number and revision with different content
SUPERSEDED = "SUPERSEDED"                # a newer revision has already been processed
REVISION_OF_PAID = "REVISION_OF_PAID"    # this revises an invoice that was already paid
REVISION_OF_UNPAID = "REVISION_OF_UNPAID"  # this revises one that wasn't paid; that one is now superseded
UNIDENTIFIED = "UNIDENTIFIED"            # no invoice number, so nothing to look up

_SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    vendor_key TEXT NOT NULL, number_key TEXT NOT NULL, revision INTEGER NOT NULL,
    content_hash TEXT NOT NULL, source_file TEXT NOT NULL, vendor TEXT, total TEXT,
    status TEXT NOT NULL, detail TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY (vendor_key, number_key, revision)
);
CREATE TABLE IF NOT EXISTS payments (
    payment_key TEXT PRIMARY KEY, vendor TEXT NOT NULL, amount TEXT NOT NULL,
    status TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def _alnum(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").casefold())


def _plain(d: Decimal | None) -> str:
    return "" if d is None else format(d.normalize(), "f")  # 5000, 5000.0 and 5000.00 are one amount


def identity(inv: Invoice) -> tuple[str, str, int] | None:
    """The (vendor, number, revision) an invoice is known by, or None if it has no invoice number."""
    number = re.sub(r"^inv", "", _alnum(inv.invoice_number))
    if not number:
        return None
    digits = re.sub(r"\D", "", inv.revision or "")
    return _alnum(inv.vendor_name), number, int(digits) if digits else 0


def content_hash(inv: Invoice) -> str:
    """A fingerprint of what the invoice bills: who, which number, how much, for which lines."""
    parts = [*map(str, identity(inv) or ("", "", 0)), _plain(inv.total)]
    parts += [f"{_alnum(li.item)}|{_plain(li.quantity)}|{_plain(li.unit_price)}" for li in inv.line_items]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Seen:
    """The ledger's answer about one invoice: what kind of arrival it is, and the earlier record it relates to, if any."""
    kind: str
    earlier: dict | None = None


class Ledger:
    """One SQLite file of invoices and payments.

    Args:
        path: The database file; created if missing. ":memory:" keeps it for this object's life only.
        pay: The payment function, called as pay(vendor, amount). Defaults to the brief's mock_payment.
        now: Returns the current time; tests pass a fixed clock.
    """

    def __init__(self, path: str | Path = "ledger.db", pay=mock_payment, now=lambda: datetime.now(timezone.utc)):
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._pay, self._now = pay, now

    def close(self) -> None:
        self._db.close()

    def _row(self, key, revision=None):
        if revision is None:  # the highest revision on record for this vendor and number
            sql, args = "SELECT * FROM invoices WHERE vendor_key=? AND number_key=? ORDER BY revision DESC LIMIT 1", key[:2]
        else:
            sql, args = "SELECT * FROM invoices WHERE vendor_key=? AND number_key=? AND revision=?", (*key[:2], revision)
        row = self._db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def seen(self, inv: Invoice) -> Seen:
        """Compare an invoice with what is on record. Changes nothing.

        Returns:
            Seen(kind, earlier), where kind is one of the constants at the top of this module and
            earlier is the record it was compared with (None for NEW and UNIDENTIFIED).
        """
        key = identity(inv)
        if key is None:
            return Seen(UNIDENTIFIED)
        same = self._row(key, key[2])
        if same:
            if same["content_hash"] != content_hash(inv):
                return Seen(CONFLICT, same)
            return Seen(SAME_FILE if same["source_file"] == inv.source_file else DUPLICATE, same)
        latest = self._row(key)
        if latest is None:
            return Seen(NEW)
        if latest["revision"] > key[2]:
            return Seen(SUPERSEDED, latest)
        return Seen(REVISION_OF_PAID if latest["status"] == "PAID" else REVISION_OF_UNPAID, latest)

    def record(self, inv: Invoice, status: str, detail: str | None = None) -> None:
        """Add the invoice, or update the status of the one already recorded under its identity."""
        key = identity(inv)
        with self._db:
            self._db.execute(
                "INSERT INTO invoices VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT (vendor_key, number_key, revision) "
                "DO UPDATE SET status=excluded.status, detail=excluded.detail, updated_at=excluded.updated_at",
                (*key, content_hash(inv), inv.source_file, inv.vendor_name, _plain(inv.total), status, detail,
                 self._now().isoformat()))

    def supersede(self, earlier: dict, by_file: str) -> None:
        """Mark an earlier, unpaid revision as replaced, so it is not paid or reviewed on its own."""
        with self._db:
            self._db.execute("UPDATE invoices SET status='SUPERSEDED', detail=?, updated_at=? "
                             "WHERE vendor_key=? AND number_key=? AND revision=?",
                             (f"by {by_file}", self._now().isoformat(),
                              earlier["vendor_key"], earlier["number_key"], earlier["revision"]))

    def pay(self, inv: Invoice) -> dict:
        """Pay the invoice once. A second call for the same invoice pays nothing and says so.

        The payment row is written as PENDING before the payment function is called, and marked
        PAID after it returns. So if the process dies in between, the PENDING row is still there:
        the next attempt does not call the payment function again, because the first call may
        have gone through. That payment needs a person to check; it is never retried blindly.

        Returns:
            {"payment_key", "amount", "status", "repeated"}: status is PAID, or PENDING for a
            payment whose first attempt never confirmed; repeated is True if nothing was sent now.

        Raises:
            ValueError: The invoice has no identity or no positive total. Approval should never
                send one here; this is the last guard before money moves.
        """
        key = identity(inv)
        if key is None or inv.total is None or inv.total <= 0:
            raise ValueError(f"refusing to pay {inv.source_file}: no invoice number or no positive total")
        pkey = payment_key(*key)
        try:
            with self._db:  # the PRIMARY KEY on payment_key is the lock: only one insert can succeed
                self._db.execute("INSERT INTO payments VALUES (?,?,?,?,?)",
                                 (pkey, inv.vendor_name, _plain(inv.total), "PENDING", self._now().isoformat()))
        except sqlite3.IntegrityError:
            row = self._db.execute("SELECT amount, status FROM payments WHERE payment_key=?", (pkey,)).fetchone()
            return {"payment_key": pkey, "amount": Decimal(row["amount"]), "status": row["status"], "repeated": True}
        self._pay(inv.vendor_name, inv.total)
        with self._db:
            self._db.execute("UPDATE payments SET status='PAID' WHERE payment_key=?", (pkey,))
        return {"payment_key": pkey, "amount": inv.total, "status": "PAID", "repeated": False}

    def invoices(self) -> list[dict]:
        """Every invoice on record, oldest identity first."""
        return [dict(r) for r in self._db.execute("SELECT * FROM invoices ORDER BY number_key, vendor_key, revision")]

    def payments(self) -> list[dict]:
        """Every payment on record, in the order made."""
        return [dict(r) for r in self._db.execute("SELECT * FROM payments ORDER BY created_at, payment_key")]

    def total_paid(self) -> Decimal:
        return sum((Decimal(p["amount"]) for p in self.payments() if p["status"] == "PAID"), Decimal(0))
