"""Format-agnostic checks on a loaded Invoice, plus reading the inventory they check against.

Each check adds a flag with a stable code and its evidence. Severity is graded separately
(severity.py), so the checks only say what is wrong, not how much it matters.
"""
import sqlite3
from collections import defaultdict
from contextlib import closing
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from .models import Invoice


def flag(inv: Invoice, inventory: dict[str, int] | None = None) -> list[dict]:
    """Run the format-agnostic checks on a loaded invoice.

    Covers invalid line values, missing vendor and due date, negative total, unexpected fields,
    duplicate items, stock summed per item, and the arithmetic checks. Fields already flagged UNPARSEABLE in
    inv.flags are not flagged again as missing.

    Args:
        inv: A loaded Invoice. Its existing flags are read, not modified.
        inventory: Item name -> stock on hand. None skips the unknown-product and stock checks.

    Returns:
        New flag dicts (without severity; load() adds it), in check order.
    """
    flags = []
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

    # Invoice-level values. A field already flagged UNPARSEABLE (or whose parent was) isn't re-flagged.
    def invoice_invalid(field, value, code):
        if field not in unparseable and field.split(".")[0] not in unparseable:
            flags.append({"code": code, "field": field, "value": value})

    if inv.vendor_name is None:
        invoice_invalid("vendor.name", None, "VENDOR_MISSING")
    if inv.due_date is None:
        invoice_invalid("due_date", None, "DUE_DATE_MISSING")
    if inv.total is not None and inv.total < 0:
        invoice_invalid("total", inv.total, "TOTAL_NEGATIVE")
    if not inv.line_items:  # a bill for nothing must not look clean
        invoice_invalid("line_items", None, "LINE_ITEMS_MISSING")

    # One flag listing every path, so an agent knows to read unexpected_fields without scanning it first.
    if inv.unexpected_fields:
        flags.append({"code": "UNEXPECTED_FIELDS", "fields": list(inv.unexpected_fields)})

    for item, lines in lines_by_item.items():
        if item is not None and len(lines) > 1:
            flags.append({"code": "DUPLICATE_ITEM", "item": item, "lines": lines})

    # Summed across lines: three WidgetA lines of 15 + 5 + 2 need 22 on hand, not 15.
    if inventory is not None:
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


def _arithmetic_flags(inv: Invoice) -> list[dict]:
    """Recompute subtotal, tax and total, and flag any stated value that disagrees.

    The total is checked against the line items themselves (plus tax), not the stated
    subtotal: 1009's subtotal is wrong but its total matches its lines, so only the subtotal
    is flagged. Tax uses the stated subtotal, since that is what the vendor applied the rate to.
    With no tax amount stated, the total check uses the rate if there is one, else zero tax, and
    records which in tax_basis. Recomputed values are rounded to the cent (half up). A check
    whose inputs are missing is skipped.

    Args:
        inv: A loaded Invoice.

    Returns:
        SUBTOTAL_MISMATCH, TAX_AMOUNT_MISMATCH and TOTAL_MISMATCH flags, each with the stated
        and computed values. Empty if everything reconciles.
    """
    flags = []

    def check(field, stated, computed, **evidence):
        computed = computed.quantize(CENT, ROUND_HALF_UP)
        if stated is not None and stated != computed:
            flags.append({"code": f"{field.upper()}_MISMATCH", "field": field,
                          "stated": stated, "computed": computed, **evidence})

    lines = inv.line_items
    line_sum = None
    if lines and all(li.quantity is not None and li.unit_price is not None for li in lines):
        # Lines count as stated, negatives included: 1009's -5 WidgetA is why its subtotal is wrong.
        line_sum = sum((li.quantity * li.unit_price for li in lines), Decimal(0))
        check("subtotal", inv.subtotal, line_sum)
    if inv.subtotal is not None and inv.tax_rate is not None:
        check("tax_amount", inv.tax_amount, inv.subtotal * inv.tax_rate)
    base = line_sum if line_sum is not None else inv.subtotal  # stated subtotal only when lines can't be summed
    if base is not None:
        # Tax for the total check: as stated; else from the rate; else none was charged. 1002, 1003
        # and 1008 have no tax line and their totals equal their lines. tax_basis says which was used,
        # so a vendor who charged tax but left the line off shows up as that, not as a mystery.
        if inv.tax_amount is not None:
            tax, basis = inv.tax_amount, "STATED"
        elif inv.tax_rate is not None:
            tax, basis = (inv.subtotal if inv.subtotal is not None else base) * inv.tax_rate, "FROM_RATE"
        else:
            tax, basis = Decimal(0), "NONE_STATED"
        # TODO: lines + tax only; a shipping line (txt invoice 1010) will need adding here.
        check("total", inv.total, base + tax, tax_basis=basis)
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
