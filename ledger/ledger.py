"""The ledger: every invoice processed and every payment made, in one SQLite file.

An invoice is identified by three things:

    vendor    its name, lower-cased, letters and digits only     "Widgets Inc." -> "widgetsinc"
    number    letters and digits only, without a leading INV     "INV 1012", "INV-1012" -> "1012"
    revision  the number in its revision label, 0 for none       "R1" -> 1

The vendor is part of the identity because two vendors can both issue an invoice 1001.

Whether two files are the same invoice is decided by content, not by file name or bytes: a
fingerprint of the number, revision, total and each line's item, quantity and unit price. So a
PDF and a TXT of one invoice match although the PDF leaves out the subtotal line. The vendor is
not in the fingerprint, so the same bill under a slightly different vendor name can be noticed.
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
UNFINISHED = "UNFINISHED"                # recorded by an earlier run that stopped before deciding it
SAME_BILL_OTHER_VENDOR = "SAME_BILL_OTHER_VENDOR"  # same number and content, under a different vendor name

# Invoice statuses. RECEIVED is the only one that isn't an outcome: it means "recorded, not decided".
RECEIVED = "RECEIVED"
MONEY_MAY_HAVE_MOVED = ("PAID", "PAYMENT_UNCONFIRMED")  # a revision of either must not be paid in full
OPEN = (RECEIVED, "HELD")                               # not paid, not closed: a newer revision replaces these

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
    """A fingerprint of what the invoice bills: which number and revision, how much, for which lines.

    The vendor is left out on purpose. Within one vendor it adds nothing, and across vendors its
    absence is what lets "QuickShip Distributers" and "QuickShip Distributers (formerly FastShip
    Ltd.)" be recognised as the same bill.
    """
    parts = [*map(str, (identity(inv) or ("", "", 0))[1:]), _plain(inv.total)]
    parts += [f"{_alnum(li.item)}|{_plain(li.quantity)}|{_plain(li.unit_price)}" for li in inv.line_items]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Seen:
    """The ledger's answer about one invoice.

    kind: what sort of arrival it is (the constants above).
    earlier: the record it was compared with, if any.
    paid_total: for REVISION_OF_PAID, everything already paid across earlier revisions.
    replaces: earlier revisions still open (held or unfinished), which this revision replaces.
    """
    kind: str
    earlier: dict | None = None
    paid_total: Decimal | None = None
    replaces: tuple = ()


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

    def _history(self, vendor_key: str, number_key: str) -> list[dict]:
        """Every revision on record for one vendor and number, lowest revision first."""
        return [dict(r) for r in self._db.execute(
            "SELECT * FROM invoices WHERE vendor_key=? AND number_key=? ORDER BY revision", (vendor_key, number_key))]

    def seen(self, inv: Invoice) -> Seen:
        """Compare an invoice with what is on record. Changes nothing.

        It looks at the invoice's whole history, not only the latest revision: what matters for
        a revision is whether money has already gone out for ANY earlier one.

        Returns:
            Seen(kind, earlier, ...), where kind is one of the constants at the top of this
            module and earlier is the record it was compared with (None for NEW and UNIDENTIFIED).
        """
        key = identity(inv)
        if key is None:
            return Seen(UNIDENTIFIED)
        vendor_key, number_key, revision = key
        history = self._history(vendor_key, number_key)
        fingerprint = content_hash(inv)

        same = next((r for r in history if r["revision"] == revision), None)
        if same:
            if same["content_hash"] != fingerprint:
                return Seen(CONFLICT, same)
            if same["status"] != RECEIVED:
                return Seen(SAME_FILE if same["source_file"] == inv.source_file else DUPLICATE, same)
            # RECEIVED: recorded but never decided, because an earlier run stopped partway.
            # Skipping it as "already processed" would leave it undecided for good, so it is
            # finished now. It still goes through the revision checks below, exactly as a new
            # arrival would: an unfinished revision of a paid invoice is still that.

        # A newer revision replaces this one only while it is still live. If every newer revision
        # was rejected, this one is the best there is and is processed normally.
        newer = [r for r in history if r["revision"] > revision and r["status"] != "REJECTED"]
        if newer:
            return Seen(SUPERSEDED, newer[-1])

        older = [r for r in history if r["revision"] < revision]
        paid = [r for r in older if r["status"] in MONEY_MAY_HAVE_MOVED]
        open_ = tuple(r for r in older if r["status"] in OPEN)  # held or unfinished: replaced by this revision
        if paid:
            return Seen(REVISION_OF_PAID, paid[-1], paid_total=sum((Decimal(r["total"]) for r in paid), Decimal(0)),
                        replaces=open_)
        if open_:
            return Seen(REVISION_OF_UNPAID, open_[-1], replaces=open_)

        # Nothing under this vendor. The same number, revision and content under another vendor
        # name is probably one invoice whose vendor was written or read differently.
        other = self._db.execute("SELECT * FROM invoices WHERE number_key=? AND revision=? AND content_hash=? "
                                 "AND vendor_key<>? AND status<>'REJECTED' LIMIT 1",
                                 (number_key, revision, fingerprint, vendor_key)).fetchone()
        if other:
            return Seen(SAME_BILL_OTHER_VENDOR, dict(other))
        return Seen(UNFINISHED, same) if same else Seen(NEW)

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
        """Mark an earlier, open revision as replaced, so it is not paid or reviewed on its own."""
        with self._db:
            self._db.execute("UPDATE invoices SET status='SUPERSEDED', detail=?, updated_at=? "
                             "WHERE vendor_key=? AND number_key=? AND revision=?",
                             (f"by {by_file}", self._now().isoformat(),
                              earlier["vendor_key"], earlier["number_key"], earlier["revision"]))

    def pay(self, inv: Invoice) -> dict:
        """Pay the invoice once. A second call for the same invoice pays nothing and says so.

        The payment row is written as PENDING before the payment function is called, and marked
        PAID after it returns. If the call raises (a timeout, a dropped connection), or the
        process dies in between, the PENDING row stays: the payment may have gone through, so it
        is never sent again blindly. It needs a person to check with the bank.

        A failure of the payment function is caught here and reported in the result, so one bad
        payment costs one invoice and the rest of a batch carries on.

        Returns:
            {"payment_key", "amount", "status", "repeated"}: status is PAID, or PENDING for a
            payment that never confirmed (with "error" if it failed just now); repeated is True
            if nothing was sent now.

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
        try:
            self._pay(inv.vendor_name, inv.total)
        except Exception as e:  # whatever the payment API throws: we can't know whether money moved
            return {"payment_key": pkey, "amount": inv.total, "status": "PENDING", "repeated": False,
                    "error": f"{type(e).__name__}: {e}"}
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
