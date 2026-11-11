"""Parse invoice files (JSON, both CSV shapes, XML) into the standard Invoice structure.

Values are parsed and typed, never corrected. A value that can't be read becomes None plus an
UNPARSEABLE flag; every other check lives in checks.py.
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
    "shipping": "shipping",
    "total": "total", "currency": "currency", "payment_terms": "payment_terms",
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


def _keep(unexpected: dict, path: str, value) -> None:
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
    if m := re.fullmatch(r"tax\s*\(\s*(\d+(?:\.\d+)?)\s*%\s*\)\s*:?", label.strip(), re.I):
        if "tax_rate" in d or "tax_amount" in d:  # a second tax row: keep the first rate and amount paired
            _keep(unexpected, f"{where}.{label.strip()}", value)
        else:
            d["tax_rate"], d["tax_amount"] = Decimal(m[1]) / 100, value
        return
    key = _FIELD_KEYS.get(_snake(label))
    if key not in _TOP_FIELDS or key in d:  # never overwrite: a second "total" is for review, not a winner
        _keep(unexpected, f"{where}.{label.strip()}", value)
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
        _keep(unexpected, f"row[{n}].extra_cells", extra)


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
        key = _FIELD_KEYS.get(_snake(label))
        lines = d["line_items"]
        if key == "item":
            lines.append({"item": value})
        elif key in _JSON_LINE and lines and not lines[-1].get(key, "").strip():
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
                _keep(unexpected, f"row[{n}]", row)
            continue
        i, li = len(d["line_items"]), {}
        _extra_cells(row, len(header), n, unexpected)
        for h, cell in zip(header, row):
            key = _FIELD_KEYS.get(h)
            if key in _JSON_LINE:
                if not li.get(key, "").strip():
                    li[key] = cell
                elif cell.strip():  # repeated column (two "Qty"s): first wins, the other is for review
                    _keep(unexpected, f"line_items[{i}].{h}", cell)
            elif key in _TOP_FIELDS and not first.get(h, "").strip():  # a blank first row claims nothing
                first[h] = cell
            elif cell.strip() and (key not in _TOP_FIELDS or cell.strip() != first[h].strip()):
                # unknown column, or rows disagree on vendor etc.
                _keep(unexpected, f"line_items[{i}].{h}", cell)
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


# Elements that only group invoice-level fields (1014: <header>, <totals>); their children are read
# as top-level fields.
_XML_GROUPS = {"header", "totals"}
_XML_LINE_TAGS = {"item", "line_item"}
_XML_ROOT = "invoice"
_CURRENCY_ATTR = re.compile(r"@currency(#\d+)?$")  # a currency="EUR" attribute anywhere, as kept by _keep


def _xml_tag(el) -> str:
    """Return an element's tag, snake_cased and without any namespace ("{urn:x}Unit_Price" -> "unit_price")."""
    return _snake(el.tag.rsplit("}", 1)[-1])


def _xml_raw(el) -> str:
    """Serialize one element for review, without the text that follows it (that belongs to its parent)."""
    tail, el.tail = el.tail, None
    try:
        return ET.tostring(el, encoding="unicode").strip()
    finally:
        el.tail = tail


def _xml_attrs(el, path: str, unexpected: dict) -> None:
    """Keep an element's attributes for review (as "path@name"), since no field reads them.

    Args:
        el: The XML element.
        path: The element's path.
        unexpected: Collected unmapped fields; updated in place.

    Returns:
        None.
    """
    for name, value in el.attrib.items():
        _keep(unexpected, f"{path}@{name}", value)


def _xml_children(el, path: str, unexpected: dict):
    """Yield a container element's children, keeping any loose text around them for review.

    In a container (an element whose children are fields), text between the child elements has no
    field to land in: "PAY TO ACCT 999" sitting next to <total> must not vanish.

    Args:
        el: The container element.
        path: Its path; loose text is kept as "path#text".
        unexpected: Collected unmapped fields; updated in place.

    Yields:
        Each child element.
    """
    if (el.text or "").strip():
        _keep(unexpected, f"{path}#text", el.text.strip())
    for child in el:
        yield child
        if (child.tail or "").strip():
            _keep(unexpected, f"{path}#text", child.tail.strip())


def _xml_value(el, path: str, unexpected: dict) -> str | None:
    """Read a leaf element's text. An element with children has no single value, so it is kept for review.

    Args:
        el: The XML element.
        path: The element's path, used for unexpected entries.
        unexpected: Collected unmapped fields; updated in place.

    Returns:
        The element's text, or None if there is nothing to set: the element is empty (so it claims no
        field, and a later real value still lands) or has children (sent to unexpected).
    """
    _xml_attrs(el, path, unexpected)
    if len(el):
        _keep(unexpected, path, _xml_raw(el))
        return None
    return el.text if (el.text or "").strip() else None


def _xml_line(el, i: int, unexpected: dict) -> dict:
    """Read one <item> under <line_items> into a JSON-shaped line dict.

    Args:
        el: The line element.
        i: The line's position, used in paths ("line_items[1].discount").
        unexpected: Collected unmapped fields; updated in place.

    Returns:
        A JSON-shaped line dict ("name" is read as the item).
    """
    path = f"line_items[{i}]"
    _xml_attrs(el, path, unexpected)
    li = {}
    for child in _xml_children(el, path, unexpected):
        tag = _xml_tag(child)
        value = _xml_value(child, f"{path}.{tag}", unexpected)
        if value is None:
            continue
        key = "item" if tag == "name" else _FIELD_KEYS.get(tag)
        if key in _JSON_LINE and key not in li:
            li[key] = value
        else:  # unknown, or a repeat of a field already read: first wins
            _keep(unexpected, f"{path}.{tag}", value)
    return li


def _no_dtd(*_):
    raise ValueError("XML with a DTD (<!DOCTYPE>) is not accepted: invoices don't need one, and entities can be abused")


def from_xml(path: str | Path) -> Invoice:
    """Load an XML invoice file (the 1014 shape, or any close variant).

    The root must be <invoice>. Fields can sit directly under it or inside <header>/<totals>; line
    items are <item> (or <line_item>) elements under one <line_items>, with <name> read as the item.
    <vendor> may be plain text or hold <name> and <address>. Field names map exactly as for CSV
    (qty, line_total, tax, ...). Nothing is silently dropped: unknown elements, attributes, repeats
    (including a second <line_items>), nested values, loose text, comments and processing
    instructions go to unexpected_fields. With no <currency> element, a currency="..." attribute is
    used as the invoice's currency.

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
    # encoding, unlike searching the bytes for "<!DOCTYPE"), and collects every comment and
    # processing instruction, which the element tree below doesn't keep.
    guard = expat.ParserCreate()
    guard.StartDoctypeDeclHandler = _no_dtd
    guard.CommentHandler = lambda text: _keep(unexpected, "#comment", text.strip())
    guard.ProcessingInstructionHandler = lambda target, data: _keep(unexpected, "#pi", f"{target} {data}".strip())
    guard.Parse(raw, True)
    root = ET.fromstring(raw)
    if _xml_tag(root) != _XML_ROOT:
        raise ValueError(f"root element is <{_xml_tag(root)}>, not <{_XML_ROOT}>: not an invoice")

    d = {"line_items": []}
    lines_read = False
    _xml_attrs(root, "", unexpected)

    def read_top(el, prefix: str):
        nonlocal lines_read
        tag = _xml_tag(el)
        path = f"{prefix}{tag}"
        if tag in _XML_GROUPS and not prefix:
            _xml_attrs(el, path, unexpected)
            for child in _xml_children(el, path, unexpected):
                read_top(child, f"{path}.")
        elif tag == "line_items" and not prefix:
            if lines_read:  # a second <line_items> block: first wins, like every other repeat
                _keep(unexpected, path, _xml_raw(el))
                return
            lines_read = True
            _xml_attrs(el, path, unexpected)
            for child in _xml_children(el, path, unexpected):
                if _xml_tag(child) in _XML_LINE_TAGS:
                    d["line_items"].append(_xml_line(child, len(d["line_items"]), unexpected))
                else:
                    _keep(unexpected, f"{path}.{_xml_tag(child)}", _xml_raw(child))
        elif tag == "vendor" and len(el):  # <vendor><name/><address/></vendor>
            _xml_attrs(el, path, unexpected)
            vendor = {}
            for child in _xml_children(el, path, unexpected):
                ctag = _xml_tag(child)
                value = _xml_value(child, f"{path}.{ctag}", unexpected)
                if value is None:
                    continue
                if ctag in _JSON_VENDOR and ctag not in vendor:
                    vendor[ctag] = value
                else:
                    _keep(unexpected, f"{path}.{ctag}", value)
            if not vendor:
                return
            if "vendor" in d:
                _keep(unexpected, path, vendor)
            else:
                d["vendor"] = vendor
        else:
            value = _xml_value(el, path, unexpected)
            if value is None:
                return
            key = _FIELD_KEYS.get(tag)
            if key in _TOP_FIELDS and key not in d:  # never overwrite: a second <total> is for review
                d[key] = {"name": value} if key == "vendor" else value
            else:
                _keep(unexpected, path, value)

    for child in _xml_children(root, "", unexpected):
        read_top(child, "")

    # No <currency> element but a currency="EUR" attribute (say on <total>): that is the currency.
    if "currency" not in d:
        attr = next((k for k in unexpected if _CURRENCY_ATTR.search(k)), None)
        if attr is not None:
            d["currency"] = unexpected.pop(attr)
    return _build(d, path.name, unexpected)


LOADERS = {".json": from_json, ".csv": from_csv, ".xml": from_xml}
