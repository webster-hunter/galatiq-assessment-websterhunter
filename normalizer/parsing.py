"""Parse invoice files (JSON, both CSV shapes) into the standard Invoice structure.

Values are parsed and typed, never corrected. A value that can't be read becomes None plus an
UNPARSEABLE flag; every other check lives in checks.py.
"""
import csv
import json
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .models import Invoice, LineItem


def _text(v) -> str | None:
    """Normalize a text field.

    Args:
        v: A raw value from the source file.

    Returns:
        The stripped string; a number as text (invoice_number 1004 -> "1004"); None for null or blank.

    Raises:
        TypeError: v is another type (list, dict, bool). The caller records it as UNPARSEABLE.
    """
    if isinstance(v, (int, Decimal)) and not isinstance(v, bool):
        return str(v)
    if v is not None and not isinstance(v, str):
        raise TypeError(f"expected text, got {type(v).__name__}")
    return (v or "").strip() or None


def _date(v) -> date | None:
    """Parse a date written as ISO (2026-01-24) or US MM/DD/YYYY.

    Args:
        v: A raw value from the source file.

    Returns:
        The date, or None for null or blank.

    Raises:
        ValueError: Any other format, such as "yesterday". The caller records it as UNPARSEABLE.
        TypeError: v isn't text (see _text).
    """
    v = _text(v)
    if not v:
        return None
    try:
        return date.fromisoformat(v)
    except ValueError:
        # TODO: US MM/DD/YYYY is the only other format in the sample (1007), so 01/02/2026 reads
        # as Jan 2. A DD/MM vendor would be misread; add a per-vendor format when one turns up.
        # Anything else raises, and _build records it as UNPARSEABLE rather than guessing a date.
        return datetime.strptime(v, "%m/%d/%Y").date()


# "$1,250.00", "-$250", "1250": an optional sign and $, then digits with correctly placed thousands commas.
_MONEY = re.compile(r"(-?)\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?")


def _money(v) -> Decimal | None:
    """Parse a money or quantity value into an exact Decimal.

    Accepts numbers and strings like "1250", "1,250.00", "$1,250.00" and "-$250".

    Args:
        v: A raw value from the source file.

    Returns:
        The Decimal, or None for null or blank.

    Raises:
        InvalidOperation: Unreadable text, including misplaced commas ("1,25,0").
        ValueError: NaN or Infinity.
        TypeError: A bool.
        The caller records any of these as UNPARSEABLE.
    """
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
    """Collect the keys in a JSON invoice that the loader doesn't map.

    Args:
        d: The parsed JSON invoice.

    Returns:
        Path -> raw value, e.g. {"po_number": ..., "vendor.tax_id": ..., "line_items[2].discount": ...}.
    """
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
    """Load a JSON invoice file.

    Args:
        path: The .json file.

    Returns:
        The Invoice, with UNPARSEABLE flags for unreadable values but no check flags yet
        (load() adds those).

    Raises:
        OSError: The file can't be read.
        json.JSONDecodeError: The file isn't valid JSON.
        AttributeError: The top level isn't a JSON object.
    """
    path = Path(path)
    # parse_float=Decimal keeps money exact: 1472.80 never passes through a float.
    # parse_constant=str: the non-standard NaN/Infinity literals stay text, get flagged UNPARSEABLE,
    # and never reach the output as a bare NaN that strict JSON readers reject.
    d = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal, parse_constant=str)
    return _build(d, path.name, _unexpected(d))


def _build(d: dict, source_file: str, unexpected: dict) -> Invoice:
    """Build an Invoice from a JSON-shaped dict. Every loader converts to this shape first.

    A value that can't be read becomes None plus an UNPARSEABLE flag carrying the raw value,
    so one bad field never costs the rest of the invoice. A vendor given as a plain string is
    taken as its name.

    Args:
        d: Invoice data in the JSON file's shape (vendor as {"name", "address"}, line_items as a list).
        source_file: File name to record on the invoice.
        unexpected: Unmapped fields already collected by the loader; stored as-is.

    Returns:
        The Invoice. Its flags hold only the UNPARSEABLE flags found while building.
    """
    problems = []

    def bad(field, raw):
        problems.append({"code": "UNPARSEABLE", "field": field, "value": raw})

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
    """Convert a CSV header or label to snake_case: "Unit Price" -> "unit_price", "Total:" -> "total".

    Args:
        s: The header or label text.

    Returns:
        Lowercase words joined by underscores, with punctuation removed.
    """
    return re.sub(r"\W+", "_", s.strip().lower()).strip("_")


def _put(d: dict, label: str, value: str, unexpected: dict, where: str) -> None:
    """Set one invoice-level field from a CSV label and value.

    "Tax (6%):" sets both the tax rate and the tax amount. A field is never overwritten: a
    repeated label, a second tax row, or an unknown label goes to unexpected instead.

    Args:
        d: The JSON-shaped dict being built; updated in place.
        label: The CSV header or footer label, e.g. "Subtotal:".
        value: The raw cell value.
        unexpected: Collected unmapped fields; updated in place.
        where: Path prefix for unexpected entries, e.g. "row[5]" or "header".

    Returns:
        None.
    """
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
    """Keep CSV cells past the expected width for review, since they have no column to land in.

    A stray "PAY TO ACCT 999" in an unlabelled column is exactly what must not vanish.
    Trailing blank cells (common in spreadsheet exports) are ignored.

    Args:
        row: The CSV row.
        width: Number of cells the row should have (header length, or 2 for field,value files).
        n: Row number as a spreadsheet shows it, used in the path.
        unexpected: Collected unmapped fields; gets "row[n].extra_cells" if any extra cell has content.

    Returns:
        None.
    """
    extra = row[width:]
    if any(c.strip() for c in extra):
        unexpected[f"row[{n}].extra_cells"] = extra


def _csv_kv(rows: list[list[str]], unexpected: dict) -> dict:
    """Read a field,value CSV (the 1006 shape) into a JSON-shaped dict.

    Each `item` row starts a new line item, and the quantity, unit_price, amount and note rows
    after it attach to that item, so repeated keys keep every line.

    Args:
        rows: Data rows, without the field,value header.
        unexpected: Collected unmapped fields; updated in place.

    Returns:
        A JSON-shaped dict for _build.
    """
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
    """Read a row-per-item CSV (the 1007 shape) into a JSON-shaped dict.

    Invoice fields repeat on every row and are taken from the first; a later row that disagrees
    goes to unexpected. A row with a blank Item cell is a footer: its first filled cell is the
    label and its last is the value ("Subtotal:", "14750.00").

    Args:
        header: The header row, already snake_cased.
        rows: Data rows, without the header.
        unexpected: Collected unmapped fields; updated in place.

    Returns:
        A JSON-shaped dict for _build.
    """
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
    """Load a CSV invoice file in either shape, detected from the header.

    Args:
        path: The .csv file, UTF-8 (with or without a BOM).

    Returns:
        The Invoice, with UNPARSEABLE flags for unreadable values but no check flags yet
        (load() adds those).

    Raises:
        OSError: The file can't be read.
        UnicodeDecodeError: The file isn't UTF-8.
        ValueError: The file is empty.
    """
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
