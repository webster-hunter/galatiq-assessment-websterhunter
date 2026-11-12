"""Repair values damaged by OCR, but only when the rest of the invoice proves the reading.

OCR confuses a few look-alike characters: the letter O for zero, a lowercase l or capital I for
one. So "$3,500.O0" and "26-Jan-2O26" arrive UNPARSEABLE. This step proposes the obvious reading
and then asks an independent question the invoice can answer:

    line amount      does it equal quantity x unit price?        7 x 500 = 3,500.00
    invoice date     does it sit the stated terms before the due date?   Net 30: 26 Jan -> 25 Feb

Proven: the value is applied, the original text is kept on the flag, and UNPARSEABLE (REVIEW)
becomes OCR_REPAIRED (WARN). Not proven: nothing is applied. The field stays empty and
UNPARSEABLE, with the reading attached as `suggested` for whoever reviews it. Code never changes
a value on a guess.

Item names get the same treatment against the inventory: "Widget A" becomes WidgetA only when
exactly one inventory name is the same apart from spacing, hyphens and letter case. That exact
match is the proof. Nothing fuzzy: a typo or a new product stays unknown, for a person.

No model is involved. The model's job was to copy the damage faithfully; this is where it is read.
"""
import re
from datetime import date
from decimal import Decimal
from itertools import product

from .checks import CENT, _rounding
from .models import Invoice
from .parsing import _date, _money

# Characters OCR commonly writes in place of a digit. Deliberately short: each pair added makes
# more readings possible, and so fewer of them provable.
LOOKALIKES = {"O": "0", "o": "0", "l": "1", "I": "1"}
MAX_LOOKALIKES = 8  # 2**8 readings to try at most; a value with more is too damaged to guess at

_MONEY_FIELDS = {"subtotal", "tax_amount", "shipping", "total", "quantity", "unit_price", "amount"}
_DATE_FIELDS = {"date", "due_date"}
_LINE_FIELD = re.compile(r"line_items\[(\d+)\]\.(quantity|unit_price|amount)")
_NET_DAYS = re.compile(r"Net (\d+)")
TERMS_TOLERANCE_DAYS = 3  # "Net 30" is 30 or 31 days on the samples (a calendar month)


def _readings(raw: str, parse) -> list:
    """Every distinct value raw can be read as by swapping look-alike characters for digits.

    Each look-alike is tried both ways, since not every O is a zero: in "26-Oct-2O26" only the
    second one is. A reading counts only if the whole value then parses.
    """
    spots = [i for i, ch in enumerate(raw) if ch in LOOKALIKES]
    if not spots or len(spots) > MAX_LOOKALIKES:
        return []
    found = []
    for choice in product((False, True), repeat=len(spots)):
        chars = list(raw)
        for i, swap in zip(spots, choice):
            if swap:
                chars[i] = LOOKALIKES[chars[i]]
        try:
            value = parse("".join(chars))
        except (ValueError, TypeError, ArithmeticError):
            continue
        if value is not None and value not in found:
            found.append(value)
    return found


def _proof(inv: Invoice, field: str, value, unreadable: set[str]) -> str | None:
    """A sentence stating what confirms `value` for `field`, or None if nothing on the invoice does.

    The confirming values must themselves have been read: a field that is unreadable is never
    used as evidence.
    """
    if m := _LINE_FIELD.fullmatch(field):
        li, name = inv.line_items[int(m[1])], m[2]
        q, p, a = li.quantity, li.unit_price, li.amount
        if name == "amount" and q is not None and p is not None and abs(q * p - value) <= CENT:
            return f"quantity {q} x unit price {p} = {value}"
        if name == "quantity" and p and a is not None and abs(value * p - a) <= CENT:
            return f"{value} x unit price {p} = amount {a}"
        if name == "unit_price" and q and a is not None and abs(q * value - a) <= CENT:
            return f"quantity {q} x {value} = amount {a}"
        return None

    lines = inv.line_items
    line_sum = None
    if lines and all(li.quantity is not None and li.unit_price is not None for li in lines):
        line_sum = sum((li.quantity * li.unit_price for li in lines), Decimal(0))
    if field == "subtotal" and line_sum is not None and abs(line_sum - value) <= _rounding(len(lines)):
        return f"the line items sum to {line_sum}"
    if field == "total" and line_sum is not None and not unreadable & {"tax_amount", "tax_rate", "shipping"}:
        tax = inv.tax_amount if inv.tax_amount is not None else (
            (inv.subtotal if inv.subtotal is not None else line_sum) * inv.tax_rate if inv.tax_rate is not None else Decimal(0))
        expected = line_sum + tax + (inv.shipping or Decimal(0))
        if abs(expected - value) <= _rounding(len(lines)) + CENT:
            return f"line items {line_sum} + tax {tax} + shipping {inv.shipping or 0} = {expected.quantize(CENT)}"
    if field in _DATE_FIELDS and (m := _NET_DAYS.fullmatch(inv.payment_terms or "")):
        other = inv.due_date if field == "date" else inv.date
        if other is not None:
            days = (other - value).days if field == "date" else (value - other).days
            if abs(days - int(m[1])) <= TERMS_TOLERANCE_DAYS:
                start, end = (value, other) if field == "date" else (other, value)
                return f"{inv.payment_terms}: {start} to {end} is {days} days"
    return None


def _set(inv: Invoice, field: str, value) -> None:
    if m := _LINE_FIELD.fullmatch(field):
        setattr(inv.line_items[int(m[1])], m[2], value)
    else:
        setattr(inv, field, value)


def _name_key(name: str) -> str:
    """A product name with spacing, hyphens, underscores and letter case removed: "Widget A" and "widget-a" -> "widgeta"."""
    return re.sub(r"[\s_-]+", "", name).casefold()


def repair_item_names(inv: Invoice, inventory: dict) -> Invoice:
    """Rename a line item to its inventory name when the two differ only in spacing, hyphens or case.

    Runs before the checks, so the product and stock checks see the inventory's name: "Widget A"
    is found, and its quantity is summed with any other WidgetA line. Only an exact match on the
    stripped name counts, and only if it points to one inventory name. A near miss ("WidgtA") or
    a product the inventory doesn't have (WidgetC) is left as written, to be flagged
    UNKNOWN_PRODUCT, which rejects the invoice.

    Args:
        inv: An Invoice straight from a loader. Changed in place: a matched item is renamed and
            gets an ITEM_NAME_REPAIRED flag holding the original name.
        inventory: Item name -> stock on hand.

    Returns:
        The same Invoice.
    """
    by_key = {}
    for name in inventory:
        if isinstance(name, str):
            by_key.setdefault(_name_key(name), []).append(name)
    for i, li in enumerate(inv.line_items):
        if li.item is None or li.item in inventory:
            continue
        matches = by_key.get(_name_key(li.item), [])
        if len(matches) == 1:  # two inventory names that collapse to the same key can't be told apart
            inv.flags.append({"code": "ITEM_NAME_REPAIRED", "field": f"line_items[{i}].item", "value": li.item,
                              "repaired": matches[0], "proof": "the only inventory name that matches without spacing, hyphens and case"})
            li.item = matches[0]
    return inv


def repair(inv: Invoice) -> Invoice:
    """Apply every OCR repair the invoice itself proves; attach a suggestion where it can't.

    Runs before the checks, so a repaired value is checked like any other. Repeats while it makes
    progress, since one repair can supply the evidence for another.

    Args:
        inv: An Invoice straight from a loader. Changed in place: proven values are set, and each
            UNPARSEABLE flag either becomes OCR_REPAIRED or gains `suggested`.

    Returns:
        The same Invoice.
    """
    progress = True
    while progress:
        progress = False
        unreadable = {f["field"] for f in inv.flags if f["code"] == "UNPARSEABLE"}
        for f in inv.flags:
            field, raw = f.get("field", ""), f.get("value")
            name = field.rsplit(".", 1)[-1]
            if f["code"] != "UNPARSEABLE" or not isinstance(raw, str):
                continue
            if name in _DATE_FIELDS and field == name:
                parse = _date
            elif field in _MONEY_FIELDS or _LINE_FIELD.fullmatch(field):
                parse = _money
            else:
                continue
            readings = _readings(raw, parse)
            proven = [(v, p) for v in readings if (p := _proof(inv, field, v, unreadable - {field}))]
            if len(proven) == 1:  # two provable readings would be a coincidence too far to choose between
                value, proof = proven[0]
                _set(inv, field, value)
                f.update(code="OCR_REPAIRED", repaired=value, proof=proof)  # value keeps the original text
                f.pop("suggested", None)
                progress = True
            elif len(readings) == 1:
                f["suggested"] = readings[0]
    return inv


__all__ = ["LOOKALIKES", "repair", "repair_item_names"]
