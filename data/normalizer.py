"""Load invoices from any source format into one standard Invoice structure.

Values are parsed and typed, never corrected. A negative quantity is kept as stated and
flagged; validation decides what a flag means.
"""
import csv
import json
import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path


@dataclass
class LineItem:
    item: str | None
    quantity: Decimal | None
    unit_price: Decimal | None
    amount: Decimal | None = None  # stated line total, if the invoice gives one
    note: str | None = None


@dataclass
class Invoice:
    invoice_number: str | None
    vendor_name: str | None
    vendor_address: str | None
    date: date | None
    due_date: date | None
    line_items: list[LineItem]
    subtotal: Decimal | None
    tax_rate: Decimal | None
    tax_amount: Decimal | None
    total: Decimal | None
    currency: str | None
    payment_terms: str | None
    revision: str | None = None
    notes: str | None = None
    source_file: str | None = None
    # Source fields the loader doesn't map, keyed by path ("vendor.tax_id", "line_items[2].discount").
    # Kept verbatim for the agent to review: nothing in the file is silently dropped.
    unexpected_fields: dict = field(default_factory=dict)
    # Issues for validation to judge, e.g. {"code": "DUPLICATE_ITEM", "item": "WidgetA", "lines": [0, 3]}.
    flags: list[dict] = field(default_factory=list)


def flag(inv: Invoice, inventory: dict[str, int] | None = None) -> list[dict]:
    """Format-agnostic checks on a loaded Invoice.

    inventory maps item -> stock on hand. None skips the product and stock checks.
    """
    flags = []
    # Already flagged UNPARSEABLE at load: don't flag the resulting None again as MISSING.
    unparseable = {f["field"] for f in inv.flags if f.get("reason") == "UNPARSEABLE"}
    lines_by_item = defaultdict(list)
    requested = defaultdict(Decimal)
    for i, li in enumerate(inv.line_items):
        if f"line_items[{i}]" in unparseable:  # whole line unreadable: one flag is enough
            continue
        lines_by_item[li.item].append(i)
        if li.quantity is not None and li.quantity > 0:  # invalid quantities are flagged, not counted
            requested[li.item] += li.quantity

        def invalid(attr, reason):
            path = f"line_items[{i}].{attr}"
            if path not in unparseable:
                flags.append({"code": "INVALID_VALUE", "field": path, "value": getattr(li, attr), "reason": reason})

        if li.quantity is None:
            invalid("quantity", "MISSING")
        elif li.quantity <= 0:  # zero is as unpayable as negative
            invalid("quantity", "NOT_POSITIVE")
        if li.unit_price is None:
            invalid("unit_price", "MISSING")
        elif li.unit_price < 0:
            invalid("unit_price", "NEGATIVE")
        if li.item is None:
            invalid("item", "MISSING")
        # ponytail: exact name match; "WidgetA (rush order)"-style variants need SKU matching later.
        elif inventory is not None and li.item not in inventory:
            invalid("item", "UNKNOWN_PRODUCT")

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
    """Recompute subtotal, tax and total; flag any stated value that disagrees.

    The total is checked against the line items themselves (plus stated tax), not the stated
    subtotal: 1009's subtotal is wrong but its total matches its lines, so only the subtotal
    is flagged. Tax uses the stated subtotal, since that is what the vendor applied the rate to.
    A check whose inputs are missing is skipped.
    """
    flags = []

    def check(field, stated, computed):
        computed = computed.quantize(CENT, ROUND_HALF_UP)
        if stated is not None and stated != computed:
            flags.append({"code": f"{field.upper()}_MISMATCH", "field": field,
                          "stated": stated, "computed": computed})

    lines = inv.line_items
    line_sum = None
    if lines and all(li.quantity is not None and li.unit_price is not None for li in lines):
        # Lines count as stated, negatives included: 1009's -5 WidgetA is why its subtotal is wrong.
        line_sum = sum((li.quantity * li.unit_price for li in lines), Decimal(0))
        check("subtotal", inv.subtotal, line_sum)
    if inv.subtotal is not None and inv.tax_rate is not None:
        check("tax_amount", inv.tax_amount, inv.subtotal * inv.tax_rate)
    base = line_sum if line_sum is not None else inv.subtotal  # stated subtotal only when lines can't be summed
    if base is not None and inv.tax_amount is not None:
        # ponytail: lines + tax only; a shipping line (txt invoice 1010) will need adding here.
        check("total", inv.total, base + inv.tax_amount)
    return flags


def inventory_from_db(db_path: str | Path) -> dict[str, int]:
    """item -> stock, from the README's inventory table."""
    # as_uri() percent-encodes the path: a raw "#" or "?" would cut the URI short, drop mode=ro,
    # and silently create an empty database. closing(): sqlite3's own `with` never closes.
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"  # read-only; fails if missing
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        return dict(conn.execute("SELECT item, stock FROM inventory"))


def _text(v) -> str | None:
    """Blank strings mean 'not provided', same as null. A number (invoice_number: 1004) becomes text."""
    if isinstance(v, (int, Decimal)) and not isinstance(v, bool):
        return str(v)
    if v is not None and not isinstance(v, str):
        raise TypeError(f"expected text, got {type(v).__name__}")
    return (v or "").strip() or None


def _date(v) -> date | None:
    v = _text(v)
    if not v:
        return None
    try:
        return date.fromisoformat(v)
    except ValueError:
        # ponytail: US MM/DD/YYYY is the only other format in the sample (1007), so 01/02/2026 reads
        # as Jan 2. A DD/MM vendor would be misread; add a per-vendor format when one turns up.
        # Anything else raises, and _build records it as UNPARSEABLE rather than guessing a date.
        return datetime.strptime(v, "%m/%d/%Y").date()


# "$1,250.00", "-$250", "1250": an optional sign and $, then digits with correctly placed thousands commas.
_MONEY = re.compile(r"(-?)\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?")


def _money(v) -> Decimal | None:
    if isinstance(v, bool):  # JSON true is an int in Python; it is not a number here
        raise TypeError("expected a number, got bool")
    if isinstance(v, str):
        v = _text(v)  # blank CSV cell -> None, not Decimal("")
        if v and (m := _MONEY.fullmatch(v)):
            v = m[1] + m[2].replace(",", "") + (m[3] or "")
    if v is None:
        return None
    d = Decimal(str(v))  # anything still unreadable raises InvalidOperation
    if not d.is_finite():  # "NaN"/"Infinity" parse as Decimals but break every comparison downstream
        raise ValueError(f"not a finite number: {v}")
    return d


_JSON_TOP = {"invoice_number", "vendor", "date", "due_date", "line_items", "subtotal", "tax_rate",
             "tax_amount", "total", "currency", "payment_terms", "revision", "notes"}
_JSON_VENDOR = {"name", "address"}
_JSON_LINE = {"item", "quantity", "unit_price", "amount", "note"}


def _unexpected(d: dict) -> dict:
    """Every key in the JSON record that from_json doesn't map, by path."""
    out = {k: v for k, v in d.items() if k not in _JSON_TOP}
    vendor = d.get("vendor")
    if isinstance(vendor, dict):
        out |= {f"vendor.{k}": v for k, v in vendor.items() if k not in _JSON_VENDOR}
    lines = d.get("line_items")
    for i, li in enumerate(lines if isinstance(lines, list) else []):
        if isinstance(li, dict):  # non-dict lines are flagged UNPARSEABLE by _build
            out |= {f"line_items[{i}].{k}": v for k, v in li.items() if k not in _JSON_LINE}
    return out


def from_json(path: str | Path) -> Invoice:
    path = Path(path)
    # parse_float=Decimal keeps money exact: 1472.80 never passes through a float.
    # parse_constant=str: the non-standard NaN/Infinity literals stay text, get flagged UNPARSEABLE,
    # and never reach the output as a bare NaN that strict JSON readers reject.
    d = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal, parse_constant=str)
    return _build(d, path.name, _unexpected(d))


def _build(d: dict, source_file: str, unexpected: dict) -> Invoice:
    """JSON-shaped dict -> Invoice. Every loader converts to this shape first.

    A value that can't be read becomes None plus an UNPARSEABLE flag carrying the raw value,
    so one bad field never costs the rest of the invoice.
    """
    problems = []

    def bad(field, raw):
        problems.append({"code": "INVALID_VALUE", "field": field, "value": raw, "reason": "UNPARSEABLE"})

    def parse(fn, raw, field):
        try:
            return fn(raw)
        except (ValueError, TypeError, ArithmeticError):  # decimal.InvalidOperation is an ArithmeticError
            bad(field, raw)
            return None

    vendor = d.get("vendor")
    if isinstance(vendor, str):
        vendor = {"name": vendor}  # a bare string is the vendor's name
    elif vendor is not None and not isinstance(vendor, dict):
        bad("vendor", vendor)
        vendor = None
    vendor = vendor or {}

    raw_lines = d.get("line_items")
    if raw_lines is not None and not isinstance(raw_lines, list):
        bad("line_items", raw_lines)
        raw_lines = None
    line_items = []
    for i, li in enumerate(raw_lines or []):
        if not isinstance(li, dict):
            bad(f"line_items[{i}]", li)
            li = {}  # keep an empty line so later line_items[i] paths still match the source
        line_items.append(LineItem(
            item=parse(_text, li.get("item"), f"line_items[{i}].item"),
            quantity=parse(_money, li.get("quantity"), f"line_items[{i}].quantity"),
            unit_price=parse(_money, li.get("unit_price"), f"line_items[{i}].unit_price"),
            amount=parse(_money, li.get("amount"), f"line_items[{i}].amount"),
            note=parse(_text, li.get("note"), f"line_items[{i}].note"),
        ))

    top = {k: parse(fn, d.get(k), k) for k, fn in (
        ("invoice_number", _text), ("date", _date), ("due_date", _date), ("subtotal", _money),
        ("tax_rate", _money), ("tax_amount", _money), ("total", _money), ("currency", _text),
        ("payment_terms", _text), ("revision", _text), ("notes", _text))}
    return Invoice(
        **top,
        vendor_name=parse(_text, vendor.get("name"), "vendor.name"),
        vendor_address=parse(_text, vendor.get("address"), "vendor.address"),
        line_items=line_items,
        source_file=source_file,
        unexpected_fields=unexpected,
        flags=problems,
    )


# CSV header or key (snake_cased) -> JSON-shape key. Both CSV shapes go through this.
_CSV_KEYS = {
    "invoice_number": "invoice_number", "vendor": "vendor", "date": "date", "due_date": "due_date",
    "item": "item", "qty": "quantity", "quantity": "quantity", "unit_price": "unit_price",
    "line_total": "amount", "amount": "amount", "note": "note",
    "subtotal": "subtotal", "tax": "tax_amount", "tax_amount": "tax_amount", "tax_rate": "tax_rate",
    "total": "total", "currency": "currency", "payment_terms": "payment_terms",
    "revision": "revision", "notes": "notes",
}
_CSV_TOP = _JSON_TOP - {"line_items"}


def _snake(s: str) -> str:
    return re.sub(r"\W+", "_", s.strip().lower()).strip("_")


def _put(d: dict, label: str, value: str, unexpected: dict, where: str) -> None:
    """Set one invoice-level field from a CSV label. Unknown or repeated labels go to unexpected."""
    # "Tax (6%):". A malformed rate like "6.5.1" doesn't match and goes to unexpected as an unknown label.
    if m := re.fullmatch(r"tax\s*\(\s*(\d+(?:\.\d+)?)\s*%\s*\)\s*:?", label.strip(), re.I):
        if "tax_rate" in d or "tax_amount" in d:  # a second tax row: keep the first rate and amount paired
            unexpected[f"{where}.{label.strip()}"] = value
        else:
            d["tax_rate"], d["tax_amount"] = Decimal(m[1]) / 100, value
        return
    key = _CSV_KEYS.get(_snake(label))
    if key not in _CSV_TOP or key in d:  # never overwrite: a second "total" is for review, not a winner
        unexpected[f"{where}.{label.strip()}"] = value
    else:
        d[key] = {"name": value} if key == "vendor" else value


def _extra_cells(row: list[str], width: int, n: int, unexpected: dict) -> None:
    """Cells past the expected width have no column to land in; keep them for review.

    A stray "PAY TO ACCT 999" in an unlabelled column is exactly what must not vanish.
    Trailing blank cells (common in spreadsheet exports) are ignored.
    """
    extra = row[width:]
    if any(c.strip() for c in extra):
        unexpected[f"row[{n}].extra_cells"] = extra


def _csv_kv(rows: list[list[str]], unexpected: dict) -> dict:
    """1006 shape: field,value rows. Each `item` row starts a new line item."""
    d = {"line_items": []}
    for n, row in enumerate(rows, start=2):  # numbered as a spreadsheet shows them
        if not any(c.strip() for c in row):
            continue
        label, value = row[0], row[1] if len(row) > 1 else ""
        _extra_cells(row, 2, n, unexpected)
        key = _CSV_KEYS.get(_snake(label))
        lines = d["line_items"]
        if key == "item":
            lines.append({"item": value})
        elif key in _JSON_LINE and lines and key not in lines[-1]:
            lines[-1][key] = value
        else:
            _put(d, label, value, unexpected, f"row[{n}]")
    return d


def _csv_table(header: list[str], rows: list[list[str]], unexpected: dict) -> dict:
    """1007 shape: one row per line item, invoice fields repeated on each, then label/value footer rows."""
    d, first = {"line_items": []}, {}
    item_col = header.index("item") if "item" in header else None
    for n, row in enumerate(rows, start=2):
        cells = [c for c in row if c.strip()]
        if not cells:
            continue
        if item_col is not None and not (row[item_col:item_col + 1] or [""])[0].strip():
            # Footer, e.g. ",,,,,,Subtotal:,14750.00": label is the first filled cell, value the last.
            if len(cells) == 2:
                _put(d, cells[0], cells[1], unexpected, f"row[{n}]")
            else:
                unexpected[f"row[{n}]"] = row
            continue
        i, li = len(d["line_items"]), {}
        _extra_cells(row, len(header), n, unexpected)
        for h, cell in zip(header, row):
            key = _CSV_KEYS.get(h)
            if key in _JSON_LINE:
                if key not in li:
                    li[key] = cell
                elif cell.strip():  # repeated column (two "Qty"s): first wins, the other is for review
                    unexpected[f"line_items[{i}].{h}"] = cell
            elif key in _CSV_TOP and h not in first:
                first[h] = cell
            elif cell.strip() and (key not in _CSV_TOP or cell.strip() != first[h].strip()):
                unexpected[f"line_items[{i}].{h}"] = cell  # unknown column, or rows disagree on vendor etc.
        d["line_items"].append(li)
    for h, cell in first.items():
        _put(d, h, cell, unexpected, "header")
    return d


def from_csv(path: str | Path) -> Invoice:
    path = Path(path)
    with path.open(newline="", encoding="utf-8-sig") as f:  # -sig: Excel exports start with a BOM
        rows = list(csv.reader(f))
    if not rows:
        raise ValueError(f"Empty CSV: {path}")
    header, unexpected = [_snake(h) for h in rows[0]], {}
    if header[:2] == ["field", "value"]:
        d = _csv_kv(rows[1:], unexpected)
    else:
        d = _csv_table(header, rows[1:], unexpected)
    return _build(d, path.name, unexpected)


LOADERS = {".json": from_json, ".csv": from_csv}


def load(path: str | Path, inventory: dict[str, int] | None = None) -> Invoice:
    suffix = Path(path).suffix.lower()
    if suffix not in LOADERS:
        raise ValueError(f"No loader for {suffix} files yet: {path}")
    inv = LOADERS[suffix](path)
    inv.flags += flag(inv, inventory)  # here, not in each loader, so every format gets the same checks
    return inv


def expand(paths) -> list[Path]:
    """Files pass through as given; a directory contributes its supported files."""
    out = []
    for p in map(Path, paths):
        if p.is_dir():
            out += sorted(f for f in p.iterdir() if f.suffix.lower() in LOADERS)
        else:
            out.append(p)
    return out


def normalize(paths, inventory: dict[str, int] | None = None) -> list[dict]:
    """One result per file. A bad file yields an error entry, not a crashed batch."""
    results = []
    for p in expand(paths):
        try:
            results.append({"file": str(p), "invoice": asdict(load(p, inventory))})
        except Exception as e:
            results.append({"file": str(p), "error": f"{type(e).__name__}: {e}"})
    return results


def _json_default(v):
    # Decimal as a string keeps it exact for whoever reads the JSON.
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, date):
        return v.isoformat()
    raise TypeError(f"Not JSON serializable: {type(v).__name__}")


if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="Normalize invoice files into the standard Invoice format (JSON on stdout).")
    ap.add_argument("paths", nargs="+", help="invoice files and/or directories")
    ap.add_argument("--inventory", help="SQLite inventory DB; enables unknown-product and stock checks")
    args = ap.parse_args()
    try:
        inventory = inventory_from_db(args.inventory) if args.inventory else None
    except sqlite3.Error as e:
        # Same shape as a file error, so an agent always gets JSON on stdout.
        print(json.dumps([{"file": args.inventory, "error": f"{type(e).__name__}: {e}"}], indent=2))
        sys.exit(1)
    results = normalize(args.paths, inventory)
    print(json.dumps(results, indent=2, default=_json_default))
    sys.exit(1 if any("error" in r for r in results) else 0)
