"""Parse invoice files (JSON, both CSV shapes, XML) into the standard Invoice structure.

Values are parsed and typed, never corrected. A value that can't be read becomes None plus an
UNPARSEABLE flag, and CSV rows that disagree on an invoice-level field get CONFLICTING_ROWS;
every other check lives in checks.py.
"""
import csv
import json
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from xml.parsers import expat

from .models import Invoice, LineItem



class UnfamiliarLayout(ValueError):
    """The file is well-formed, but its outermost structure isn't an invoice's as the loaders know it.

    A ValueError, so everything that treats a loader's ValueError as "this file can't be loaded"
    still does. Raised separately so that a caller with a model can try reading the file as text.
    """

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


# Every date format seen in vendor invoices, tried in order. An explicit list (not date.fromisoformat,
# whose accepted formats change between Python versions) means a file parses the same on every
# interpreter. A new vendor format is one more line here.
_DATE_FORMATS = (
    "%Y-%m-%d",    # 2026-01-24 (JSON, CSV, XML, most text)
    "%m/%d/%Y",    # 01/28/2026 (1007). TODO: US order, so 01/02/2026 is Jan 2; a DD/MM vendor needs its own rule
    "%B %d, %Y",   # January 27, 2026 (1010)
    "%b %d, %Y",   # Jan 27, 2026
    "%B %d %Y",    # January 27 2026
    "%b %d %Y",    # Jan 30 2026 (1002)
    "%d-%b-%Y",    # 25-Feb-2026 (1012)
)


def _date(v) -> date | None:
    """Parse a date in any of _DATE_FORMATS.

    Args:
        v: A raw value from the source file.

    Returns:
        The date, or None for null or blank.

    Raises:
        ValueError: No format matches, such as "yesterday". The caller records it as UNPARSEABLE
            rather than guessing a date.
        TypeError: v isn't text (see _text).
    """
    v = _text(v)
    if not v:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {v!r}")


# "$1,250.00", "-$250", "1250": an optional sign and $, then digits with correctly placed thousands commas.
# ASCII digits only: \d alone would also match digits from other scripts.
_MONEY = re.compile(r"(-?)\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?", re.ASCII)


def _money(v) -> Decimal | None:
    """Parse a money or quantity value into an exact Decimal.

    Accepts numbers and strings like "1250", "1,250.00", "$1,250.00" and "-$250".

    Args:
        v: A raw value from the source file.

    Returns:
        The Decimal, or None for null or blank.

    Raises:
        ValueError: Text that isn't a plain number: misplaced commas ("1,25,0"), digit-group
            underscores ("1_000", which Decimal alone would accept), exponents, NaN or Infinity.
        TypeError: A bool.
        The caller records any of these as UNPARSEABLE.
    """
    if isinstance(v, bool):  # JSON true is an int in Python; it is not a number here
        raise TypeError("expected a number, got bool")
    if isinstance(v, str):
        v = _text(v)  # blank CSV cell -> None, not Decimal("")
        if v is None:
            return None
        m = _MONEY.fullmatch(v)
        if not m:  # Decimal() is too generous for vendor text: it reads "1_0" as 10 and "1e3" as 1000
            raise ValueError(f"not a plain number: {v!r}")
        v = m[1] + m[2].replace(",", "") + (m[3] or "")
    if v is None:
        return None
    d = Decimal(str(v))
    if not d.is_finite():  # "NaN"/"Infinity" parse as Decimals but break every comparison downstream
        raise ValueError(f"not a finite number: {v}")
    return d


_JSON_TOP = {"invoice_number", "vendor", "date", "due_date", "line_items", "subtotal", "tax_rate",
             "tax_amount", "shipping", "total", "currency", "payment_terms", "revision", "notes"}
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
        if isinstance(li, dict):  # non-dict lines are flagged UNPARSEABLE by build_invoice
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
        ValueError: The top level isn't a JSON object.
    """
    path = Path(path)
    repeats = []

    def first_wins(pairs):
        # json.loads alone keeps the LAST value of a repeated key and drops the others silently.
        # Keep the first, as CSV and XML do, and hold the rest for review.
        obj = {}
        for key, value in pairs:
            if key in obj:
                repeats.append((key, value))
            else:
                obj[key] = value
        return obj

    # parse_float=Decimal keeps money exact: 1472.80 never passes through a float.
    # parse_constant=str: the non-standard NaN/Infinity literals stay text, get flagged UNPARSEABLE,
    # and never reach the output as a bare NaN that strict JSON readers reject.
    d = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal, parse_constant=str,
                   object_pairs_hook=first_wins)
    if not isinstance(d, dict):
        raise UnfamiliarLayout(f"top level is a JSON {type(d).__name__}, not an object: not an invoice")
    unexpected = _unexpected(d)
    for key, value in repeats:
        keep_unexpected(unexpected, f"repeated_key.{key}", value)
    return build_invoice(d, path.name, unexpected)


def build_invoice(d: dict, source_file: str, unexpected: dict) -> Invoice:
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
        ("tax_rate", _money), ("tax_amount", _money), ("shipping", _money), ("total", _money), ("currency", _text),
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


# Source field name (snake_cased) -> JSON-shape key. Both CSV shapes and XML go through this.
_FIELD_KEYS = {
    "invoice_number": "invoice_number", "vendor": "vendor", "date": "date", "due_date": "due_date",
    "item": "item", "qty": "quantity", "quantity": "quantity", "unit_price": "unit_price",
    "line_total": "amount", "amount": "amount", "note": "note",
    "subtotal": "subtotal", "tax": "tax_amount", "tax_amount": "tax_amount", "tax_rate": "tax_rate",
    "sales_tax": "tax_amount",  # 1010
    "shipping": "shipping", "shipping_handling": "shipping", "shipping_and_handling": "shipping",
    "total": "total", "total_amount": "total", "grand_total": "total",  # 1003, 1013 PDF
    "currency": "currency", "payment_terms": "payment_terms",
    "revision": "revision", "notes": "notes",
}
_TOP_FIELDS = _JSON_TOP - {"line_items"}


def _snake(s: str) -> str:
    """Convert a CSV header or label to snake_case: "Unit Price" -> "unit_price", "Total:" -> "total".

    Args:
        s: The header or label text.

    Returns:
        Lowercase words joined by underscores, with punctuation removed.
    """
    return re.sub(r"\W+", "_", s.strip().lower()).strip("_")


def keep_unexpected(unexpected: dict, path: str, value) -> None:
    """Add one entry to unexpected without overwriting: a repeat of the same path becomes "path#2", "path#3".

    Args:
        unexpected: Collected unmapped fields; updated in place.
        path: Where the value was found, e.g. "totals.handling".
        value: The raw value.

    Returns:
        None.
    """
    key, n = path, 2
    while key in unexpected:
        key, n = f"{path}#{n}", n + 1
    unexpected[key] = value


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
    if not value.strip():  # blank claims no field, so a later row with a real value still lands
        return
    # "Tax (6%):". A malformed rate like "6.5.1" doesn't match and goes to unexpected as an unknown label.
    if m := re.fullmatch(r"(?:sales\s+)?tax\s*\(\s*(\d+(?:\.\d+)?)\s*%\s*\)\s*:?", label.strip(), re.I):
        if "tax_rate" in d or "tax_amount" in d:  # a second tax row: keep the first rate and amount paired
            keep_unexpected(unexpected, f"{where}.{label.strip()}", value)
        else:
            d["tax_rate"], d["tax_amount"] = Decimal(m[1]) / 100, value
        return
    key = _FIELD_KEYS.get(_snake(label))
    if key not in _TOP_FIELDS or key in d:  # never overwrite: a second "total" is for review, not a winner
        keep_unexpected(unexpected, f"{where}.{label.strip()}", value)
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
        keep_unexpected(unexpected, f"row[{n}].extra_cells", extra)


def _csv_kv(rows: list[list[str]], unexpected: dict) -> dict:
    """Read a field,value CSV (the 1006 shape) into a JSON-shaped dict.

    Each `item` row starts a new line item, and the quantity, unit_price, amount and note rows
    after it attach to that item, so repeated keys keep every line.

    Args:
        rows: Data rows, without the field,value header.
        unexpected: Collected unmapped fields; updated in place.

    Returns:
        A JSON-shaped dict for build_invoice.
    """
    d = {"line_items": []}
    line_open = False  # True from an `item` row until the next row that isn't one of that line's fields
    for n, row in enumerate(rows, start=2):  # numbered as a spreadsheet shows them
        if not any(c.strip() for c in row):
            continue
        label, value = row[0], row[1] if len(row) > 1 else ""
        _extra_cells(row, 2, n, unexpected)
        key = _FIELD_KEYS.get(_snake(label))
        lines = d["line_items"]
        if key == "item":
            lines.append({"item": value})
            line_open = True
        elif key in _JSON_LINE and line_open and not lines[-1].get(key, "").strip():
            lines[-1][key] = value
        else:
            # An invoice-level row closes the line: a `note` or `amount` after `total` is not that
            # line's, so it goes to unexpected rather than being buried in the last line item.
            line_open = False
            _put(d, label, value, unexpected, f"row[{n}]")
    return d


def _csv_table(header: list[str], rows: list[list[str]], unexpected: dict, conflicts: list) -> dict:
    """Read a row-per-item CSV (the 1007 shape) into a JSON-shaped dict.

    Invoice fields repeat on every row and are taken from the first. A later row that disagrees
    is recorded in conflicts: rows for INV-A and INV-B in one file must not load as one invoice.
    A row with a blank Item cell holding just a label and a value is a footer ("Subtotal:",
    "14750.00"). Any other blank-Item row with a quantity or price is still a line item, and is
    flagged for its missing item.

    Args:
        header: The header row, already snake_cased.
        rows: Data rows, without the header.
        unexpected: Collected unmapped fields; updated in place.
        conflicts: Collected CONFLICTING_ROWS flags; updated in place.

    Returns:
        A JSON-shaped dict for build_invoice.
    """
    d, first = {"line_items": []}, {}
    keys = [_FIELD_KEYS.get(h) for h in header]
    if "amount" not in keys:
        # Item, Qty, Unit Price, Total: with no other amount column, a "Total" column is each
        # line's total. The invoice total comes from a footer row.
        keys = ["amount" if h == "total" else k for h, k in zip(header, keys)]
    cols = {k: i for i, k in reversed(list(enumerate(keys))) if k}  # first column for each key

    def cell_of(row, key):
        i = cols.get(key)
        return row[i].strip() if i is not None and i < len(row) else ""

    for n, row in enumerate(rows, start=2):
        cells = [c for c in row if c.strip()]
        if not cells:
            continue
        if "item" in cols and not cell_of(row, "item"):
            # Blank Item cell. A label and a value (",,,,,,Subtotal:,14750.00") is a footer. The label
            # sits in whatever column the vendor put it, so it is told apart by not being a number.
            if len(cells) == 2 and not _MONEY.fullmatch(cells[0].strip()):
                _put(d, cells[0], cells[1], unexpected, f"row[{n}]")
                continue
            # Anything else with a quantity or price is a line item with no product: load it, so
            # ITEM_MISSING blocks it, rather than dropping the line from the invoice.
            if not (cell_of(row, "quantity") or cell_of(row, "unit_price")):
                keep_unexpected(unexpected, f"row[{n}]", row)
                continue
        i, li = len(d["line_items"]), {}
        _extra_cells(row, len(header), n, unexpected)
        for h, key, cell in zip(header, keys, row):
            if key in _JSON_LINE:
                if not li.get(key, "").strip():
                    li[key] = cell
                elif cell.strip():  # repeated column (two "Qty"s): first wins, the other is for review
                    keep_unexpected(unexpected, f"line_items[{i}].{h}", cell)
            elif key in _TOP_FIELDS and not first.get(h, "").strip():  # a blank first row claims nothing
                first[h] = cell
            elif key in _TOP_FIELDS:
                if cell.strip() and cell.strip() != first[h].strip():
                    conflicts.append({"code": "CONFLICTING_ROWS", "field": key, "row": n,
                                      "value": cell.strip(), "first_value": first[h].strip()})
            elif cell.strip():  # unknown column
                keep_unexpected(unexpected, f"line_items[{i}].{h}", cell)
        d["line_items"].append(li)
    for h, cell in first.items():
        _put(d, h, cell, unexpected, "header")
    return d


def from_csv(path: str | Path) -> Invoice:
    """Load a CSV invoice file in either shape, detected from the header.

    Args:
        path: The .csv file, UTF-8 (with or without a BOM).

    Returns:
        The Invoice, with UNPARSEABLE flags for unreadable values and CONFLICTING_ROWS flags for
        rows that disagree on an invoice-level field, but no check flags yet (load() adds those).

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
    header, unexpected, conflicts = [_snake(h) for h in rows[0]], {}, []
    if header[:2] == ["field", "value"]:
        d = _csv_kv(rows[1:], unexpected)
    else:
        d = _csv_table(header, rows[1:], unexpected, conflicts)
    inv = build_invoice(d, path.name, unexpected)
    inv.flags += conflicts
    return inv


# The XML shape in the sample (1014): an <invoice> root holding plain fields, optionally grouped
# under <header> and <totals>, and one <line_items> holding <item> elements. Anything outside that
# shape is kept in unexpected_fields for review, never interpreted.
_XML_GROUPS = {"header", "totals"}


def _xml_raw(el) -> str:
    """Serialize one element for review, without the text that follows it (that belongs to its parent)."""
    tail, el.tail = el.tail, None
    try:
        return ET.tostring(el, encoding="unicode").strip()
    finally:
        el.tail = tail


def _xml_children(el, path: str, unexpected: dict) -> list:
    """Return a grouping element's children, keeping whatever else it holds for review.

    A grouping element (the root, <header>, <totals>, <line_items>, an <item>) should hold only
    child elements. Its attributes and any loose text between the children have no field to land
    in: "PAY TO ACCT 999" sitting next to <total> must not vanish.

    Args:
        el: The grouping element.
        path: Its path ("" for the root); extras are kept as "path@attributes" and "path#text".
        unexpected: Collected unmapped fields; updated in place.

    Returns:
        The child elements, in document order.
    """
    if el.attrib:
        keep_unexpected(unexpected, f"{path}@attributes", dict(el.attrib))
    for text in [el.text] + [child.tail for child in el]:
        if (text or "").strip():
            keep_unexpected(unexpected, f"{path}#text", text.strip())
    return list(el)


def _xml_field(el, where: str, target: dict, allowed: set, unexpected: dict) -> None:
    """Set one field from a plain element: text only, no attributes, no children.

    Anything else is kept whole in unexpected and sets nothing. So <total currency="EUR">500</total>
    leaves the total missing (a BLOCK) rather than being read as 500 dollars. An empty element
    claims no field, and a field is never overwritten: a repeat goes to unexpected.

    Args:
        el: The element.
        where: Path of its parent ("" for the root, "totals", "line_items[0]").
        target: The JSON-shaped dict being built (the invoice, or one line); updated in place.
        allowed: The keys target accepts (_TOP_FIELDS or _JSON_LINE).
        unexpected: Collected unmapped fields; updated in place.

    Returns:
        None.
    """
    tag = _snake(el.tag)
    path = f"{where}.{tag}" if where else tag
    if len(el) or el.attrib:
        keep_unexpected(unexpected, path, _xml_raw(el))
        return
    if not (el.text or "").strip():
        return
    key = "item" if tag == "name" and allowed is _JSON_LINE else _FIELD_KEYS.get(tag)
    if key in allowed and key not in target:
        target[key] = {"name": el.text} if key == "vendor" else el.text
    else:
        keep_unexpected(unexpected, path, el.text)


def _no_dtd(*_):
    raise ValueError("XML with a DTD (<!DOCTYPE>) is not accepted: invoices don't need one, and entities can be abused")


def from_xml(path: str | Path) -> Invoice:
    """Load an XML invoice file in the 1014 shape.

    The root must be <invoice>. Fields are plain elements directly under it or inside <header> or
    <totals>; line items are <item> elements under one <line_items>, with <name> read as the item.
    Field names map exactly as for CSV (qty, line_total, tax, ...). Everything else is kept in
    unexpected_fields and raises UNEXPECTED_FIELDS: unknown or repeated elements (including a
    second <line_items>), elements with attributes or children, loose text, comments and
    processing instructions.

    Args:
        path: The .xml file. The encoding is taken from the XML declaration.

    Returns:
        The Invoice, with UNPARSEABLE flags for unreadable values but no check flags yet
        (load() adds those).

    Raises:
        OSError: The file can't be read.
        ValueError: The file declares a DTD (vendor XML is untrusted; refusing DTDs rules out
            entity-expansion attacks), or the root element isn't <invoice>.
        xml.parsers.expat.ExpatError: The file isn't well-formed XML (caught by the pre-pass).
    """
    path = Path(path)
    raw = path.read_bytes()
    unexpected = {}
    # Pre-pass with expat: stops at a DOCTYPE before its contents are processed (works in any
    # encoding, unlike searching the bytes for "<!DOCTYPE"), and collects comments and processing
    # instructions, which the element tree below doesn't keep.
    guard = expat.ParserCreate()
    guard.StartDoctypeDeclHandler = _no_dtd
    guard.CommentHandler = lambda text: keep_unexpected(unexpected, "#comment", text.strip())
    guard.ProcessingInstructionHandler = lambda target, data: keep_unexpected(unexpected, "#pi", f"{target} {data}".strip())
    guard.Parse(raw, True)
    root = ET.fromstring(raw)
    if _snake(root.tag) != "invoice":
        raise UnfamiliarLayout(f"root element is <{root.tag}>, not <invoice>: not an invoice")

    d = {"line_items": []}
    lines_read = False
    for child in _xml_children(root, "", unexpected):
        tag = _snake(child.tag)
        if tag in _XML_GROUPS:
            for el in _xml_children(child, tag, unexpected):
                _xml_field(el, tag, d, _TOP_FIELDS, unexpected)
        elif tag == "line_items" and not lines_read:
            lines_read = True
            for item in _xml_children(child, tag, unexpected):
                if _snake(item.tag) != "item":
                    keep_unexpected(unexpected, f"{tag}.{_snake(item.tag)}", _xml_raw(item))
                    continue
                where, line = f"{tag}[{len(d['line_items'])}]", {}
                for el in _xml_children(item, where, unexpected):
                    _xml_field(el, where, line, _JSON_LINE, unexpected)
                d["line_items"].append(line)
        else:  # a plain field; or a second <line_items>, which _xml_field keeps whole (first wins)
            _xml_field(child, "", d, _TOP_FIELDS, unexpected)
    return build_invoice(d, path.name, unexpected)


LOADERS = {".json": from_json, ".csv": from_csv, ".xml": from_xml}


def layout_recognised(inv: Invoice) -> bool:
    """Whether a loader found an invoice in the file it read, not just a stray field.

    A structured file in a layout the loaders don't know (a supplier's own field names) parses
    without error and yields next to nothing: at most one thing that happens to share a name
    with ours, and the rest of what it holds in unexpected_fields. That is different from an
    invoice in our layout with a field missing, which has the others. So: recognised means at
    least two of an invoice number, a vendor, a total, and a line with anything on it.
    """
    line = any(v is not None for li in inv.line_items for v in (li.item, li.quantity, li.unit_price, li.amount))
    return sum([inv.invoice_number is not None, inv.vendor_name is not None, inv.total is not None, line]) >= 2
