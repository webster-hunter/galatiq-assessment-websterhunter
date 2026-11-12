"""Format-agnostic checks on a loaded Invoice, plus reading the inventory they check against.

Each check adds a flag with a stable code and its evidence. Severity is graded separately
(severity.py), so the checks only say what is wrong, not how much it matters.
"""
import re
import sqlite3
from datetime import date
from collections import defaultdict
from contextlib import closing
from decimal import ROUND_HALF_UP, ROUND_UP, Decimal
from pathlib import Path

from .models import Invoice


# The form each text field takes across the sample invoices. A value in another form is still
# read as written, with an UNEXPECTED_FORMAT warning: it is worth a look, and matching (duplicate
# invoice numbers, product names against inventory) needs one form to compare. Only fields the
# samples write one way are listed; vendor names and addresses have no form to check. One pattern
# per field for now; per-vendor patterns belong with a vendor list.
FORMATS = {
    "invoice_number": re.compile(r"INV-\d+"),          # INV-1001; not "1002" or "INV 1012"
    "revision": re.compile(r"R\d+"),                   # R1
    "currency": re.compile(r"[A-Z]{3}"),               # a currency code, USD or EUR; not "$"
    "payment_terms": re.compile(r"Net \d+"),           # Net 30; not "Immediate"
    "line_items[].item": re.compile(r"[A-Z][A-Za-z0-9]*"),  # one word, WidgetA; not "Widget A"
}

# "Invoices over $10K require additional scrutiny" (the brief). At or above it, a person reviews
# the invoice however clean it is. Exactly $10,000 counts: the cautious reading of "over".
APPROVAL_THRESHOLD = Decimal("10000")
# A total within this fraction below the threshold is noted: $9,900 and $9,975 on the samples.
# Billing just under an approval limit is a known way to avoid review. On its own it is only a
# warning (plenty of honest invoices are $9,900); the approval policy weighs it with other flags.
NEAR_THRESHOLD_BAND = Decimal("0.02")

# Wording that pushes for fast payment, and requests to pay by wire or to a changed account: the
# two marks of payment fraud that show in an invoice's own text. Keyword checks, in code, so that
# rejecting an invoice never depends on a model noticing something. Each is a warning on its own.
# The lists are narrow on purpose. Ordinary terms are not pressure: "late fees of 1.5% apply" and
# "penalty" in a terms clause appear on honest invoices, so only "avoid penalties", the phrasing
# that pushes, is matched. Paying by bank transfer is also ordinary, which is why the policy never
# rejects on wording alone.
PRESSURE_WORDS = re.compile(r"\b(urgent(ly)?|immediate(ly)?|asap|right away|final notice|avoid (any |further )?penalt(y|ies)|legal action)\b", re.I)
WIRE_WORDS = re.compile(r"\b(wire(d| transfers?)?|bank transfers?|(new|updated|changed) (bank )?(account|details)|routing number)\b", re.I)


# A rate published more than this many days before the invoice date is too old to estimate with.
MAX_RATE_AGE_DAYS = 7


def usable_rate(rates, currency: str, on: date | None, today: date | None = None):
    """Ask the rate service for a rate that can be relied on. The one place this rule lives.

    The rate wanted is the one in force on the invoice date. An invoice with no date is priced as
    of today, so that an old rate is still caught as old. A rate more than MAX_RATE_AGE_DAYS
    before that date is too stale to estimate with.

    A rate service is something outside this program, and can fail. A failure here must cost one
    invoice its estimate, not stop the batch, so any error from the lookup is reported as a reason.

    Args:
        rates: The rate service (anything with usd_rate(currency, on)), or None.
        currency: The invoice's currency.
        on: The invoice date, or None if it has none.
        today: The date to price an undated invoice at; defaults to the system date.

    Returns:
        (rate, None) if there is a usable rate, else (rate or None, reason): NO_RATE_SERVICE,
        NO_RATE, STALE_RATE, or RATE_SERVICE_ERROR with the error text after a colon.
    """
    if rates is None:
        return None, "NO_RATE_SERVICE"
    wanted = on or today or date.today()
    try:
        rate = rates.usd_rate(currency, wanted)
    except Exception as e:  # whatever a rate service throws: a lookup that failed, not a bad invoice
        return None, f"RATE_SERVICE_ERROR: {type(e).__name__}: {e}"
    if rate is None:
        return None, "NO_RATE"
    if rate.age_days(wanted) > MAX_RATE_AGE_DAYS:
        return rate, "STALE_RATE"
    return rate, None


def flag(inv: Invoice, inventory: dict[str, int] | None = None, rates=None, today: date | None = None) -> list[dict]:
    """Run the format-agnostic checks on a loaded invoice.

    Covers invalid line values, missing invoice number, vendor, due date and total, an invoice
    number, revision, currency, payment terms or item name in an unexpected format, a due date before the invoice date, negative total and
    shipping, a non-USD or assumed currency, a total at or just under the approval threshold,
    pressure and wire-transfer wording,
    unexpected fields, duplicate items, stock summed per item, and the arithmetic checks. Fields
    already flagged UNPARSEABLE in inv.flags are not flagged again as missing.

    Args:
        inv: A loaded Invoice. Its existing flags are read, not modified.
        inventory: Item name -> stock on hand. None skips the unknown-product and stock checks.
        rates: A rate service: anything with usd_rate(currency, on) returning a rate (see the
            rates package). None means an invoice in another currency has no dollar estimate.
        today: The date an undated foreign-currency invoice is priced at; defaults to the system date.

    Returns:
        New flag dicts (without severity; load() adds it), in check order.
    """
    flags = []

    def unexpected_format(field, value, pattern):
        if value is not None and not pattern.fullmatch(value):
            flags.append({"code": "UNEXPECTED_FORMAT", "field": field, "value": value, "expected": pattern.pattern})

    # Already flagged UNPARSEABLE at load: don't flag the resulting None again as missing.
    unparseable = {f["field"] for f in inv.flags if f["code"] == "UNPARSEABLE"}
    lines_by_item = defaultdict(list)
    requested = defaultdict(Decimal)
    for i, li in enumerate(inv.line_items):
        if f"line_items[{i}]" in unparseable:  # whole line unreadable: one flag is enough
            continue
        lines_by_item[li.item].append(i)
        if li.quantity is not None and li.quantity > 0:  # invalid quantities are flagged, not counted
            requested[li.item] += li.quantity

        def invalid(attr, code):
            path = f"line_items[{i}].{attr}"
            if path not in unparseable:
                flags.append({"code": code, "field": path, "value": getattr(li, attr)})

        if li.quantity is None:
            invalid("quantity", "QTY_MISSING")
        elif li.quantity <= 0:  # zero is as unpayable as negative
            invalid("quantity", "QTY_NON_POSITIVE")
        elif li.quantity != li.quantity.to_integral_value():
            # Stock is counted in whole units (INTEGER column), so 1.5 WidgetA needs a person to confirm.
            invalid("quantity", "QTY_NOT_WHOLE")
        if li.unit_price is None:
            invalid("unit_price", "PRICE_MISSING")
        elif li.unit_price < 0:
            invalid("unit_price", "PRICE_NEGATIVE")
        if li.item is None:
            invalid("item", "ITEM_MISSING")
        # TODO: exact name match; "WidgetA (rush order)"-style variants need SKU matching.
        elif inventory is not None and li.item not in inventory:
            invalid("item", "UNKNOWN_PRODUCT")
        # Alongside UNKNOWN_PRODUCT, this says why "Widget A" wasn't found: its form, not a new product.
        unexpected_format(f"line_items[{i}].item", li.item, FORMATS["line_items[].item"])

    # Invoice-level values. A field already flagged UNPARSEABLE (or whose parent was) isn't re-flagged.
    def invoice_invalid(field, value, code):
        if field not in unparseable and field.split(".")[0] not in unparseable:
            flags.append({"code": code, "field": field, "value": value})

    if inv.invoice_number is None:  # duplicate detection keys on it
        invoice_invalid("invoice_number", None, "INVOICE_NUMBER_MISSING")
    for name in ("invoice_number", "revision", "currency", "payment_terms"):
        unexpected_format(name, getattr(inv, name), FORMATS[name])
    if inv.vendor_name is None:
        invoice_invalid("vendor.name", None, "VENDOR_MISSING")
    if inv.due_date is None:
        invoice_invalid("due_date", None, "DUE_DATE_MISSING")
    if inv.total is None:  # nothing to pay against, and nothing to reconcile
        invoice_invalid("total", None, "TOTAL_MISSING")
    elif inv.total < 0:
        invoice_invalid("total", inv.total, "TOTAL_NEGATIVE")
    if inv.date is not None and inv.due_date is not None and inv.due_date < inv.date:
        flags.append({"code": "DUE_DATE_BEFORE_INVOICE_DATE", "field": "due_date",
                      "value": inv.due_date, "invoice_date": inv.date})
    if not inv.line_items:  # a bill for nothing must not look clean
        invoice_invalid("line_items", None, "LINE_ITEMS_MISSING")
    # A negative tax or shipping amount would quietly offset an inflated line sum.
    if inv.shipping is not None and inv.shipping < 0:
        invoice_invalid("shipping", inv.shipping, "SHIPPING_NEGATIVE")
    if inv.tax_amount is not None and inv.tax_amount < 0:
        invoice_invalid("tax_amount", inv.tax_amount, "TAX_NEGATIVE")
    if inv.tax_rate is not None and inv.tax_rate < 0:
        invoice_invalid("tax_rate", inv.tax_rate, "TAX_NEGATIVE")

    # Thresholds, prices and stock values are all in dollars. An invoice that names no currency
    # (most do not: every CSV, TXT and PDF sample) is read as USD, and so is a bare "$", which
    # several currencies share. That is an assumption, so it is recorded (INFO: it blocks nothing,
    # but it is on the invoice for audit) instead of being made silently. The cases that would
    # contradict it are held by other checks: a named non-USD currency here, and "€1,250" or
    # "£1,250" as UNPARSEABLE.
    usd_total = inv.total  # the total in dollars, for the threshold checks; None if it can't be known
    if inv.currency is None or inv.currency.upper() in ("US$", "$"):
        flags.append({"code": "CURRENCY_ASSUMED", "field": "currency", "value": inv.currency, "assumed": "USD"})
    elif inv.currency.upper() != "USD":
        # Billed in another currency. The vendor is owed that amount in that currency, and is
        # paid so. The rate service supplies a dollar ESTIMATE, used to judge the invoice against
        # the dollar thresholds and to show a reviewer what it costs. Without a usable rate the
        # invoice can't be weighed at all.
        rate, problem = usable_rate(rates, inv.currency, inv.date, today)
        if problem:
            usd_total = None
            flags.append({"code": "CURRENCY_UNSUPPORTED", "field": "currency", "value": inv.currency, "reason": problem,
                          **({"rate_date": rate.as_of} if rate else {})})
        else:
            usd_total = rate.to_usd(inv.total) if inv.total is not None else None
            flags.append({"code": "CURRENCY_CONVERTED", "field": "currency", "value": inv.currency,
                          "usd_per_unit": rate.usd_per_unit, "rate_date": rate.as_of, "rate_source": rate.source,
                          "usd_estimate": usd_total})

    # Judged on the dollar figure: the invoice's own total, or its estimate for another currency.
    if usd_total is not None and usd_total >= APPROVAL_THRESHOLD:
        flags.append({"code": "OVER_APPROVAL_THRESHOLD", "field": "total", "value": usd_total,
                      "threshold": APPROVAL_THRESHOLD})
    elif usd_total is not None and usd_total >= APPROVAL_THRESHOLD * (1 - NEAR_THRESHOLD_BAND):
        flags.append({"code": "NEAR_THRESHOLD", "field": "total", "value": usd_total,
                      "threshold": APPROVAL_THRESHOLD, "under_by": APPROVAL_THRESHOLD - usd_total})

    # The vendor's own free text: notes, terms, and anything kept in unexpected_fields.
    texts = [("notes", inv.notes), ("payment_terms", inv.payment_terms)] + [
        (f"unexpected_fields.{k}", v) for k, v in inv.unexpected_fields.items()]
    for code, words in (("PRESSURE_LANGUAGE", PRESSURE_WORDS), ("WIRE_REQUEST", WIRE_WORDS)):
        for field, text in texts:
            if isinstance(text, str) and (found := [m[0] for m in words.finditer(text)]):
                flags.append({"code": code, "field": field, "matched": found})

    # One flag listing every path, so an agent knows to read unexpected_fields without scanning it first.
    if inv.unexpected_fields:
        flags.append({"code": "UNEXPECTED_FIELDS", "fields": list(inv.unexpected_fields)})

    for item, lines in lines_by_item.items():
        if item is not None and len(lines) > 1:
            flags.append({"code": "DUPLICATE_ITEM", "item": item, "lines": lines})

    # Summed across lines: three WidgetA lines of 15 + 5 + 2 need 22 on hand, not 15.
    if inventory is None:
        # Say so: an invoice whose products and stock were never checked must not look like one
        # that passed. 1016 (WidgetC) has no other flag.
        flags.append({"code": "INVENTORY_NOT_CHECKED", "skipped": ["UNKNOWN_PRODUCT", "STOCK_EXCEEDED"]})
    else:
        for item, qty in requested.items():
            if item not in inventory:
                continue  # already flagged UNKNOWN_PRODUCT
            stock = inventory[item]
            if isinstance(stock, bool) or not isinstance(stock, (int, float, Decimal)):
                # NULL or text stock in the DB: can't compare, so say so instead of crashing the file.
                flags.append({"code": "STOCK_UNKNOWN", "item": item, "requested": qty,
                              "available": stock, "lines": lines_by_item[item]})
            elif qty > stock:
                flags.append({"code": "STOCK_EXCEEDED", "item": item, "requested": qty,
                              "available": stock, "lines": lines_by_item[item]})

    flags += _arithmetic_flags(inv)
    return flags


CENT = Decimal("0.01")


def _rounding(lines: int) -> Decimal:
    """Return how far a sum of line amounts can differ from the exact sum through rounding alone.

    A vendor who rounds each line to the cent before adding is off by up to half a cent per line.
    That is rounding, not a mismatch; anything beyond it is flagged.

    Args:
        lines: Number of line items summed.

    Returns:
        Half a cent per line, rounded up to a whole cent (never less than one cent).
    """
    return max(CENT, (Decimal("0.005") * lines).quantize(CENT, ROUND_UP))


def _arithmetic_flags(inv: Invoice) -> list[dict]:
    """Recompute subtotal, tax and total, and flag any stated value that disagrees.

    The total is checked against the line items themselves (plus tax), not the stated
    subtotal: 1009's subtotal is wrong but its total matches its lines, so only the subtotal
    is flagged. Tax uses the stated subtotal, since that is what the vendor applied the rate to.
    With no tax amount stated, the total check uses the rate if there is one, else zero tax, and
    records which in tax_basis. Shipping, if stated, is added after tax and is not taxed.
    Recomputed values are rounded to the cent (half up), and a difference that rounding alone
    can explain is not flagged: one cent on a line amount or the tax, half a cent per line on a
    sum of lines (see _rounding). A check whose inputs are missing is skipped.

    Args:
        inv: A loaded Invoice.

    Returns:
        LINE_AMOUNT_MISMATCH, SUBTOTAL_MISMATCH, TAX_AMOUNT_MISMATCH and TOTAL_MISMATCH flags, each with the stated
        and computed values (TOTAL_MISMATCH also carries tax_basis and shipping). Empty if
        everything reconciles.
    """
    flags = []

    def check(field, stated, computed, allowed=CENT, **evidence):
        computed = computed.quantize(CENT, ROUND_HALF_UP)
        if stated is not None and abs(stated - computed) > allowed:
            flags.append({"code": f"{field.upper()}_MISMATCH", "field": field,
                          "stated": stated, "computed": computed, **evidence})

    lines = inv.line_items
    # A line's stated amount against quantity x price: the same padding as a total, one line down.
    for i, li in enumerate(lines):
        if li.amount is not None and li.quantity is not None and li.unit_price is not None:
            computed = (li.quantity * li.unit_price).quantize(CENT, ROUND_HALF_UP)
            if abs(li.amount - computed) > CENT:
                flags.append({"code": "LINE_AMOUNT_MISMATCH", "field": f"line_items[{i}].amount",
                              "stated": li.amount, "computed": computed})
    line_sum = None
    if lines and all(li.quantity is not None and li.unit_price is not None for li in lines):
        # Lines count as stated, negatives included: 1009's -5 WidgetA is why its subtotal is wrong.
        line_sum = sum((li.quantity * li.unit_price for li in lines), Decimal(0))
        check("subtotal", inv.subtotal, line_sum, allowed=_rounding(len(lines)))
    if inv.subtotal is not None and inv.tax_rate is not None:
        check("tax_amount", inv.tax_amount, inv.subtotal * inv.tax_rate)
    base = line_sum if line_sum is not None else inv.subtotal  # stated subtotal only when lines can't be summed
    # An unreadable tax or shipping value is unknown, not zero: guessing it would raise a false
    # TOTAL_MISMATCH on top of the UNPARSEABLE flag that already explains the problem.
    unreadable = {f["field"] for f in inv.flags if f["code"] == "UNPARSEABLE"}
    if base is not None and not unreadable & {"tax_amount", "tax_rate", "shipping"}:
        # Tax for the total check: as stated; else from the rate; else none was charged. 1002, 1003
        # and 1008 have no tax line and their totals equal their lines. tax_basis says which was used,
        # so a vendor who charged tax but left the line off shows up as that, not as a mystery.
        if inv.tax_amount is not None:
            tax, basis = inv.tax_amount, "STATED"
        elif inv.tax_rate is not None:
            tax, basis = (inv.subtotal if inv.subtotal is not None else base) * inv.tax_rate, "FROM_RATE"
        else:
            tax, basis = Decimal(0), "NONE_STATED"
        # Shipping is added after tax, untaxed: on 1010 the 5% tax is on the 6,700 subtotal alone.
        shipping = inv.shipping or Decimal(0)
        # Rounding allowance: the lines' (when the base is their sum) plus a cent for the tax.
        allowed = (_rounding(len(lines)) if line_sum is not None else Decimal(0)) + CENT
        check("total", inv.total, base + tax + shipping, allowed=allowed, tax_basis=basis, shipping=inv.shipping)
    return flags


def inventory_from_db(db_path: str | Path) -> dict[str, int]:
    """Read stock levels from the README's inventory table, read-only.

    Args:
        db_path: Path to the SQLite database.

    Returns:
        Item name -> stock. Stock is whatever the DB holds, so it may be None for a NULL row.

    Raises:
        sqlite3.OperationalError: The file is missing (it is never created) or has no inventory table.
    """
    # as_uri() percent-encodes the path: a raw "#" or "?" would cut the URI short, drop mode=ro,
    # and silently create an empty database. closing(): sqlite3's own `with` never closes.
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"  # read-only; fails if missing
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        return dict(conn.execute("SELECT item, stock FROM inventory"))
