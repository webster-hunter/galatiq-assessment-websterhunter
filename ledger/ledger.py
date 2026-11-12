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
import inspect
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
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
    status TEXT NOT NULL, created_at TEXT NOT NULL, currency TEXT NOT NULL DEFAULT 'USD'
);
-- Invoices waiting for a person. thread_id names the paused pipeline run that resumes with their answer.
CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL UNIQUE,
    vendor_key TEXT, number_key TEXT, revision INTEGER,
    source_file TEXT NOT NULL, vendor TEXT, total TEXT, reasons TEXT NOT NULL,
    status TEXT NOT NULL, action TEXT, reviewer TEXT, note TEXT,
    created_at TEXT NOT NULL, resolved_at TEXT
);
-- One row per run that reached an outcome: what happened to the file, who resolved it, and the
-- evidence (flags, decision, the agent's reason) as JSON. The invoices table says where an invoice
-- stands; this says how each file got there, including second copies that have no row of their own.
CREATE TABLE IF NOT EXISTS runs (
    thread_id TEXT PRIMARY KEY, source_file TEXT NOT NULL, vendor TEXT, total TEXT, currency TEXT,
    status TEXT NOT NULL, resolved_by TEXT NOT NULL, reviewer TEXT, summary TEXT NOT NULL, finished_at TEXT NOT NULL,
    -- What a list of runs shows and searches, kept out of the JSON so listing never has to parse it.
    source_path TEXT, reasons TEXT, note TEXT,
    search TEXT  -- everything a search looks through, in lower case (see search_text)
);
"""
# The kinds of outcome a list can be narrowed to, and the condition each puts on a finished run.
# "other" is whatever is none of the first three: a file that couldn't be read, a payment that
# didn't confirm.
RUN_OUTCOMES = {"paid": "status = 'PAID'", "rejected": "status = 'REJECTED'", "skipped": "status LIKE 'SKIPPED%'",
                "other": "status NOT IN ('PAID', 'REJECTED') AND status NOT LIKE 'SKIPPED%'", "waiting": "0"}
# The same for a reviewer's decision: an approval counts as paid only if its payment confirmed.
DECIDED_OUTCOMES = {"paid": "q.action = 'approve' AND p.status = 'PAID'", "rejected": "q.action = 'reject'",
                    # IS / IS NOT, not = / !=: an entry closed as lost or superseded has no action, and a
                    # comparison with NULL is neither true nor false, which would drop the row from every filter.
                    "other": "NOT (q.action IS 'reject' OR (q.action IS 'approve' AND p.status IS 'PAID'))",
                    "skipped": "0", "waiting": "0"}
# A queue entry with what the lists need from other tables: whether its decision was recorded but
# never carried out (stuck), the payment made for it, and whether its finished run was logged.
_QUEUE = """
SELECT q.*, COALESCE(q.status = 'RESOLVED' AND i.status = 'HELD', 0) AS stuck,
       p.amount AS paid_amount, p.currency AS paid_currency, p.status AS payment_status, r.thread_id IS NOT NULL AS logged
FROM review_queue q
LEFT JOIN invoices i ON i.vendor_key = q.vendor_key AND i.number_key = q.number_key AND i.revision = q.revision
LEFT JOIN payments p ON p.payment_key = q.vendor_key || '/' || q.number_key || '/r' || q.revision
LEFT JOIN runs r ON r.thread_id = q.thread_id
"""
_WAITING = "(q.status = 'WAITING' OR COALESCE(q.status = 'RESOLVED' AND i.status = 'HELD', 0))"


def search_text(*fields, total=None) -> str:
    """What a row is searched by: its fields in lower case, flag codes also in words, the amount also as shown.

    "stock exceeded" finds STOCK_EXCEEDED, and "15,000" or "$15,000.00" finds a total stored as
    15000. Written to the row's `search` column when the row is, so a search is one LIKE per word.
    """
    text = " ".join(str(f) for f in fields if f).lower()
    text += " " + text.replace("_", " ")
    try:
        amount = Decimal(str(total))
        text += f" {total} {amount:.2f} ${amount:,.2f}"     # "15,000.00" is inside "$15,000.00"
    except (InvalidOperation, ValueError):
        pass
    return text


def _matching(words: str, column: str = "search") -> tuple[str, list[str]]:
    """The SQL condition for "every word appears", and its parameters. LIKE's own wildcards in a word are taken literally."""
    terms = [w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") for w in words.lower().split()]
    return " AND ".join([f"{column} LIKE ? ESCAPE '\\'"] * len(terms)) or "1", [f"%{t}%" for t in terms]


RUN_COLUMNS = ("thread_id", "source_file", "source_path", "vendor", "total", "currency", "status", "resolved_by", "reviewer",
               "reasons", "note", "finished_at")


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


def _takes_currency(pay) -> bool:
    """Whether a payment function accepts a third argument, the currency (the brief's version takes two)."""
    try:
        params = list(inspect.signature(pay).parameters.values())
    except (TypeError, ValueError):
        return True  # can't tell (a builtin, say): let the call decide
    return len(params) >= 3 or any(p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD) for p in params)


def currency_of(inv: Invoice) -> str:
    """The currency an invoice is paid in: the one it names, or dollars if it names none (or only "$")."""
    named = (inv.currency or "").upper()
    return "USD" if named in ("", "$", "US$") else named


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
        # Added after the first version of the queue: the dollar amount a reviewer set. A ledger
        # file made before then gets the column here, since CREATE TABLE IF NOT EXISTS won't add it.
        if "amount" not in {row["name"] for row in self._db.execute("PRAGMA table_info(review_queue)")}:
            self._db.execute("ALTER TABLE review_queue ADD COLUMN amount TEXT")
        if "currency" not in {row["name"] for row in self._db.execute("PRAGMA table_info(payments)")}:
            self._db.execute("ALTER TABLE payments ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD'")
        run_columns = {row["name"] for row in self._db.execute("PRAGMA table_info(runs)")}
        for column in ("source_path", "reasons", "note", "search"):  # added to runs after its first version
            if column not in run_columns:
                self._db.execute(f"ALTER TABLE runs ADD COLUMN {column} TEXT")
        queue_columns = {row["name"] for row in self._db.execute("PRAGMA table_info(review_queue)")}
        if "search" not in queue_columns:
            self._db.execute("ALTER TABLE review_queue ADD COLUMN search TEXT")
        # Only a ledger that has just gained the column has rows without search text: every row
        # written since has it. So the tables are scanned once, at the upgrade, not at every opening.
        if "search" not in run_columns or "search" not in queue_columns:
            self._fill_search()
        self._pay, self._now = pay, now

    def close(self) -> None:
        self._db.close()

    def _fill_search(self) -> None:
        """Give rows written before the search column existed their search text."""
        with self._db:
            for r in self._db.execute("SELECT * FROM runs WHERE search IS NULL").fetchall():
                self._db.execute("UPDATE runs SET search=? WHERE thread_id=?", (search_text(
                    r["source_file"], r["vendor"], r["reasons"], r["note"], r["status"], r["resolved_by"], r["reviewer"],
                    total=r["total"]), r["thread_id"]))
            for q in self._db.execute("SELECT * FROM review_queue WHERE search IS NULL").fetchall():
                self._db.execute("UPDATE review_queue SET search=? WHERE id=?", (search_text(
                    q["source_file"], q["vendor"], q["reasons"], q["action"], q["reviewer"], q["note"], total=q["total"]), q["id"]))

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
            # What actually went out, from the payments table: a reviewer may have released an
            # earlier revision for only part of its total.
            keys = [payment_key(r["vendor_key"], r["number_key"], r["revision"]) for r in paid]
            sent = self._db.execute(f"SELECT amount FROM payments WHERE payment_key IN ({','.join('?' * len(keys))})", keys)
            return Seen(REVISION_OF_PAID, paid[-1], paid_total=sum((Decimal(r["amount"]) for r in sent), Decimal(0)),
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
        """Mark an earlier, open revision as replaced, so it is not paid or reviewed on its own.

        If it was waiting in the review queue, that entry is closed too: the reviewer should see
        the revision that replaced it, not both.
        """
        with self._db:
            self._db.execute("UPDATE review_queue SET status='SUPERSEDED', note=?, resolved_at=?, search = search || ' ' || ? "
                             "WHERE vendor_key=? AND number_key=? AND revision=? AND status='WAITING'",
                             (f"by {by_file}", self._now().isoformat(), search_text("superseded", by_file),
                              earlier["vendor_key"], earlier["number_key"], earlier["revision"]))
            self._db.execute("UPDATE invoices SET status='SUPERSEDED', detail=?, updated_at=? "
                             "WHERE vendor_key=? AND number_key=? AND revision=?",
                             (f"by {by_file}", self._now().isoformat(),
                              earlier["vendor_key"], earlier["number_key"], earlier["revision"]))

    def pay(self, inv: Invoice, amount: Decimal | None = None) -> dict:
        """Pay the invoice once. A second call for the same invoice pays nothing and says so.

        amount, if given, is paid in place of the invoice total: a reviewer releasing a revision
        of an invoice that was already paid pays only the difference.

        The payment is made in the invoice's own currency: an invoice for 4,125.00 EUR pays
        4,125.00 EUR. The vendor is owed what they billed; dollars at some rate would leave them
        short or over once their bank converted back.

        The payment row is written as PENDING before the payment function is called, and marked
        PAID after it returns. If the call raises (a timeout, a dropped connection), or the
        process dies in between, the PENDING row stays: the payment may have gone through, so it
        is never sent again blindly. It needs a person to check with the bank.

        A failure of the payment function is caught here and reported in the result, so one bad
        payment costs one invoice and the rest of a batch carries on.

        Returns:
            {"payment_key", "amount", "currency", "status", "repeated"}: status is PAID; PENDING for
            a payment that never confirmed (with "error" if it failed just now); or NOT_SENT, with
            "error", if it couldn't be attempted. repeated is True if a payment was already on record.

        Raises:
            ValueError: The invoice has no identity or no positive total. Approval should never
                send one here; this is the last guard before money moves.
        """
        key = identity(inv)
        amount = inv.total if amount is None else amount
        if key is None or amount is None or amount <= 0:
            raise ValueError(f"refusing to pay {inv.source_file}: no invoice number or no positive amount")
        pkey = payment_key(*key)
        currency = currency_of(inv)
        if currency != "USD" and not _takes_currency(self._pay):
            # Decided before anything is written: this payment can't be attempted at all, which is
            # different from one that was sent and never confirmed. Nothing is locked, so it can
            # be paid once the payment function supports the currency.
            return {"payment_key": pkey, "amount": amount, "currency": currency, "status": "NOT_SENT", "repeated": False,
                    "error": f"the payment function takes no currency, so {currency} can't be paid through it"}
        try:
            with self._db:  # the PRIMARY KEY on payment_key is the lock: only one insert can succeed
                self._db.execute("INSERT INTO payments (payment_key, vendor, amount, status, created_at, currency) "
                                 "VALUES (?,?,?,?,?,?)",
                                 (pkey, inv.vendor_name, _plain(amount), "PENDING", self._now().isoformat(), currency))
        except sqlite3.IntegrityError:
            row = self._db.execute("SELECT amount, currency, status FROM payments WHERE payment_key=?", (pkey,)).fetchone()
            return {"payment_key": pkey, "amount": Decimal(row["amount"]), "currency": row["currency"],
                    "status": row["status"], "repeated": True}
        try:
            # Dollars use the brief's two-argument call; another currency is named.
            self._pay(inv.vendor_name, amount) if currency == "USD" else self._pay(inv.vendor_name, amount, currency)
        except Exception as e:  # whatever the payment API throws: we can't know whether money moved
            return {"payment_key": pkey, "amount": amount, "currency": currency, "status": "PENDING", "repeated": False,
                    "error": f"{type(e).__name__}: {e}"}
        with self._db:
            self._db.execute("UPDATE payments SET status='PAID' WHERE payment_key=?", (pkey,))
        return {"payment_key": pkey, "amount": amount, "currency": currency, "status": "PAID", "repeated": False}

    def enqueue(self, thread_id: str, inv: Invoice, reasons: list[str]) -> int:
        """Put an invoice in the review queue and return its queue number.

        Safe to repeat: the same thread_id always maps to the same entry, so a pipeline step
        that runs twice does not queue the invoice twice.
        """
        key = identity(inv) or (None, None, None)
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO review_queue (thread_id, vendor_key, number_key, revision, source_file, vendor, "
                "total, reasons, status, created_at, search) VALUES (?,?,?,?,?,?,?,?,'WAITING',?,?)",
                (thread_id, *key, inv.source_file, inv.vendor_name, _plain(inv.total), ", ".join(reasons),
                 self._now().isoformat(), search_text(inv.source_file, inv.vendor_name, *reasons, total=inv.total)))
        return self._db.execute("SELECT id FROM review_queue WHERE thread_id=?", (thread_id,)).fetchone()["id"]

    def queue(self, status: str | None = "WAITING") -> list[dict]:
        """Review-queue entries, oldest first. status=None returns every entry, resolved ones included."""
        sql = "SELECT * FROM review_queue" + (" WHERE status=?" if status else "") + " ORDER BY id"
        return [dict(r) for r in self._db.execute(sql, (status,) if status else ())]

    def queue_entry(self, queue_id: int) -> dict | None:
        row = self._db.execute("SELECT * FROM review_queue WHERE id=?", (queue_id,)).fetchone()
        return dict(row) if row else None

    def decided_not_finished(self) -> list[dict]:
        """Queue entries with a decision recorded whose invoice is still held: the run never finished.

        A decision is written here first and carried out second. If the second step fails, this
        is how the gap stays visible: the entry still shows in the review list, and repeating
        the decision completes it.
        """
        return [dict(r) for r in self._db.execute(
            "SELECT q.* FROM review_queue q JOIN invoices i ON i.vendor_key=q.vendor_key AND i.number_key=q.number_key "
            "AND i.revision=q.revision WHERE q.status='RESOLVED' AND i.status='HELD' ORDER BY q.id")]

    def reopen(self, entry: dict, why: str) -> bool:
        """Give up on a queue entry whose saved run is gone, and put its invoice back to be processed again.

        The entry is closed as LOST and the invoice's status returns to RECEIVED, so the next run
        over its file treats it as unfinished: it is checked and decided afresh, and held again
        under a new queue number if the policy still holds it.

        Returns:
            True if an invoice was put back; False if the entry had no invoice row of its own
            (one with no invoice number, or a conflicting duplicate), which is processed again
            anyway since nothing was recorded for it.
        """
        with self._db:
            self._db.execute("UPDATE review_queue SET status='LOST', note=?, resolved_at=?, search = search || ' ' || ? WHERE id=?",
                             (why, self._now().isoformat(), search_text("lost", why), entry["id"]))
            done = self._db.execute("UPDATE invoices SET status='RECEIVED', detail=?, updated_at=? "
                                    "WHERE vendor_key=? AND number_key=? AND revision=? AND status='HELD'",
                                    (why, self._now().isoformat(), entry["vendor_key"], entry["number_key"], entry["revision"]))
        return done.rowcount > 0

    def resolve(self, thread_id: str, action: str, reviewer: str, note: str | None, amount: Decimal | None = None) -> bool:
        """Record a person's decision on a queued invoice: who, what, why, and any amount they set.

        Returns:
            True if this call recorded the decision. False if the entry was no longer waiting:
            someone else decided it first, and that decision stands.
        """
        with self._db:
            done = self._db.execute("UPDATE review_queue SET status='RESOLVED', action=?, reviewer=?, note=?, amount=?, resolved_at=?, "
                             "search = search || ' ' || ? WHERE thread_id=? AND status='WAITING'",
                             (action, reviewer, note, _plain(amount) or None, self._now().isoformat(),
                              search_text(action, reviewer, note), thread_id))
        return done.rowcount > 0

    def vendor_history(self, vendor_name: str | None) -> list[dict]:
        """What is on record for a vendor: each of its invoices with its number, revision, total and outcome.

        Matched on the vendor's name without case or punctuation, as identity() does.
        """
        rows = self._db.execute("SELECT number_key, revision, total, status, source_file FROM invoices "
                                "WHERE vendor_key=? ORDER BY number_key, revision", (_alnum(vendor_name),))
        return [dict(r) for r in rows]

    def log_run(self, thread_id: str, source_path: str, source_file: str, vendor: str | None, total, currency: str | None,
                status: str, resolved_by: str, reviewer: str | None, reasons: list[str], note: str | None, summary: str) -> None:
        """Record how one run ended. Safe to repeat: a run is one row, replaced if written again.

        Args:
            thread_id: The run's name. A held invoice released later finishes under the same one.
            source_path: The file's full path. Two files with the same name in different folders are different files.
            source_file: The file's name, for showing.
            status: The outcome: PAID, REJECTED, SKIPPED_DUPLICATE, ERROR and so on.
            reasons: The flag codes behind the outcome (or, for an approval, its warnings).
            note: One line of explanation where codes don't give it: what a skipped file is a copy
                of, why a file couldn't be read, a reviewer's note.
            resolved_by: "policy", "policy+agent" (the reviewer agent wrote the reason), "ledger"
                (a duplicate or superseded copy), "person" (released or rejected on review), or
                "none" for a file that couldn't be read.
            reviewer: The person's name, when resolved_by is "person".
            summary: The evidence as a JSON object, for showing the run later.
        """
        row = {"thread_id": thread_id, "source_file": source_file, "source_path": source_path, "vendor": vendor,
               "total": _plain(total) if total is not None else None, "currency": currency, "status": status,
               "resolved_by": resolved_by, "reviewer": reviewer, "reasons": ", ".join(reasons), "note": note,
               "summary": summary, "finished_at": self._now().isoformat(),
               "search": search_text(source_file, vendor, *reasons, note, status, resolved_by, reviewer, total=total)}
        with self._db:  # columns are named: the table has gained columns since it was first created
            self._db.execute(f"INSERT OR REPLACE INTO runs ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))

    def has_run(self, source_path: str, status: str) -> bool:
        """Whether a run of this exact file already ended this way."""
        return self._db.execute("SELECT 1 FROM runs WHERE source_path=? AND status=? LIMIT 1", (source_path, status)).fetchone() is not None

    def runs(self) -> list[dict]:
        """Every finished run, newest first, without its evidence (see run_record for one in full)."""
        return [dict(r) for r in self._db.execute(f"SELECT {', '.join(RUN_COLUMNS)} FROM runs ORDER BY finished_at DESC, rowid DESC")]

    def find_runs(self, words: str = "", outcome: str = "", limit: int = 200) -> tuple[list[dict], int]:
        """Runs that ended without a person, newest first, narrowed in the database.

        Args:
            words: A search: every word must appear in the run's search text. Empty: no narrowing.
            outcome: A key of RUN_OUTCOMES, to keep one kind. Empty: all kinds.
            limit: The most rows to return.

        Returns:
            (the matching rows, without their evidence; how many such runs there are before narrowing).
        """
        match, params = _matching(words)
        sql = (f"SELECT {', '.join(RUN_COLUMNS)} FROM runs WHERE resolved_by != 'person' AND {match} "
               f"AND ({RUN_OUTCOMES[outcome] if outcome else '1'}) ORDER BY finished_at DESC, rowid DESC LIMIT ?")
        rows = [dict(r) for r in self._db.execute(sql, (*params, limit))]
        return rows, self._db.execute("SELECT COUNT(*) FROM runs WHERE resolved_by != 'person'").fetchone()[0]

    def find_waiting(self, words: str = "") -> tuple[list[dict], int]:
        """Queue entries needing a person, oldest first: those waiting, and those decided but never carried out (stuck).

        Returns:
            (the entries matching the search, each with a `stuck` column; how many there are before narrowing). Never limited.
        """
        match, params = _matching(words, "q.search")
        rows = [dict(r) for r in self._db.execute(f"{_QUEUE} WHERE {_WAITING} AND {match} ORDER BY q.id", params)]
        return rows, self.count_waiting()

    def count_waiting(self) -> int:
        """How many queue entries need a person, without reading them."""
        return self._db.execute(f"SELECT COUNT(*) FROM ({_QUEUE} WHERE {_WAITING})").fetchone()[0]

    def find_decided(self, words: str = "", outcome: str = "", limit: int = 200) -> tuple[list[dict], int]:
        """Queue entries a reviewer has dealt with (or that were closed as lost or superseded), newest first.

        Each row carries the payment made for it (paid_amount, paid_currency, payment_status) and
        whether its finished run was logged.

        Returns:
            (the matching rows; how many such entries there are before narrowing).
        """
        match, params = _matching(words, "q.search")
        sql = (f"{_QUEUE} WHERE NOT {_WAITING} AND {match} AND ({DECIDED_OUTCOMES[outcome] if outcome else '1'}) "
               "ORDER BY q.id DESC LIMIT ?")
        rows = [dict(r) for r in self._db.execute(sql, (*params, limit))]
        return rows, self._db.execute(f"SELECT COUNT(*) FROM ({_QUEUE} WHERE NOT {_WAITING})").fetchone()[0]

    def run_record(self, thread_id: str) -> dict | None:
        row = self._db.execute("SELECT * FROM runs WHERE thread_id=?", (thread_id,)).fetchone()
        return dict(row) if row else None

    def invoices(self) -> list[dict]:
        """Every invoice on record, oldest identity first."""
        return [dict(r) for r in self._db.execute("SELECT * FROM invoices ORDER BY number_key, vendor_key, revision")]

    def payments(self) -> list[dict]:
        """Every payment on record, in the order made."""
        return [dict(r) for r in self._db.execute("SELECT * FROM payments ORDER BY created_at, payment_key")]

    def payment_summary(self) -> tuple[int, dict[str, Decimal]]:
        """How many payments are on record, and what has been paid per currency, without reading the rows.

        The same figures as len(payments()) and paid_totals(), computed by the database. Amounts
        are summed in hundredths as whole numbers, so the total is exact.
        """
        count = self._db.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        rows = self._db.execute("SELECT currency, SUM(CAST(ROUND(amount * 100) AS INTEGER)) AS hundredths FROM payments "
                                "WHERE status = 'PAID' GROUP BY currency ORDER BY MIN(rowid)")
        return count, {r["currency"]: Decimal(r["hundredths"]) / 100 for r in rows}

    def paid_totals(self) -> dict[str, Decimal]:
        """What has been paid, per currency. Amounts in different currencies are never added together."""
        totals: dict[str, Decimal] = {}
        for p in self.payments():
            if p["status"] == "PAID":
                totals[p["currency"]] = totals.get(p["currency"], Decimal(0)) + Decimal(p["amount"])
        return totals

    def total_paid(self) -> Decimal:
        """What has been paid in dollars. Payments in other currencies are in paid_totals()."""
        return self.paid_totals().get("USD", Decimal(0))
