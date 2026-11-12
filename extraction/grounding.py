"""Grounding: is every value the model transcribed actually in the document?

The model is told to copy values exactly as written, so each one must appear in the source text.
One that doesn't was invented or altered by the model: a "corrected" OCR error, a guessed date, a
number from nowhere. The arithmetic checks can't see that (a corrected number reconciles
perfectly), so this is the only check that does.

What it proves is narrow: the value was not made up. It does not prove the value is in the right
field (a bill-to name returned as the vendor is in the document too), and it can't see a line the
model left out (the arithmetic checks catch that).
"""
import re


def _squash(s: str) -> str:
    """Collapse every run of whitespace to one space: PDF spacing and line wraps aren't differences."""
    return " ".join(s.split())


def _found(value: str, text: str) -> bool:
    """Whether value appears in text as a whole token, not as part of a longer one.

    A plain substring test is too weak for short values: "5" is inside "$500.00" and "2026-01-15".
    So a value that starts with a digit can't follow a digit, ".", "," or "-" (which would make it
    the tail of a longer or a negative number), and one that ends with a digit can't run on into
    more digits or a decimal part. A letter in front is allowed: "x12" is how 1008 writes a
    quantity of 12. Values that start or end with a letter can't be glued to another word character.

    Args:
        value: The transcribed value, already squashed.
        text: The source text (or one line of it), already squashed.
    """
    before = r"(?<![\d.,-])" if value[0].isdigit() else (r"(?<!\w)" if value[0].isalpha() else "")
    after = r"(?!\d|[.,]\d)" if value[-1].isdigit() else (r"(?!\w)" if value[-1].isalpha() else "")
    return re.search(before + re.escape(value) + after, text) is not None


def _on_one_row(values: dict, rows: list[str]) -> bool:
    """Whether a line item's values belong to one row of the document.

    The numbers (quantity, unit price, amount) must all be on a single line: that is what stops
    a quantity or price being taken from another row. The item name must be on that same line,
    with one allowance. A long description can wrap:

        Industrial widget, stainless,
        model A        4    $250.00    $1,000.00

    So a name that is on no single line anywhere may run across the numbers' line and the line
    before or after it. A name that does fit on one line gets no such allowance, or "WidgetB"
    could be paired with the numbers on WidgetA's row next to it.
    """
    item = values.get("item")
    numbers = [v for k, v in values.items() if k != "item"]
    wraps = item is not None and not any(_found(item, row) for row in rows)
    for k, row in enumerate(rows):
        if not all(_found(v, row) for v in numbers):
            continue
        if item is None or _found(item, row):
            return True
        if wraps and any(_found(item, pair) for pair in (" ".join(rows[max(k - 1, 0):k + 1]), " ".join(rows[k:k + 2]))):
            return True
    return False


_ROW_KEYS = ("item", "quantity", "unit_price", "amount")  # a line item's values share one row of the document
_TOP_PATHS = {"vendor_name": "vendor.name", "vendor_address": "vendor.address"}  # as the Invoice names them


def as_written(text: str, suffix: str) -> str:
    """The text a transcription is checked against. For a CSV file, its cells set apart by spaces.

    In running text a comma before a digit means the digit is the tail of a longer number
    ("1,250"), so a value there is not counted as found. In a CSV file the comma is the
    separator: every value follows one. Read as cells, "2" in "WidgetA,2,250.00" is the value 2,
    and a quoted cell "1,250.00" stays one value.
    """
    import csv
    import io
    rows = [cells for cells in csv.reader(io.StringIO(text)) if cells]
    if suffix != ".csv":
        # A file of another type may be comma-separated all the same (.dat often is). It is read
        # as cells if it looks like a table: several rows, each with the same number of cells.
        widths = {len(cells) for cells in rows}
        if suffix in (".txt", ".pdf", ".json", ".xml") or len(rows) < 2 or len(widths) != 1 or widths.pop() < 3:
            return text
    return "\n".join("  ".join(cells) for cells in rows)


def _json_objects(text: str) -> list[str]:
    """Every JSON object in the text that holds no array, at any depth, as written.

    Found by matching braces in the raw text (skipping strings), not by parsing: the check
    compares what the model returned with the file's own characters, and a parser would rewrite
    250.00 as 250.0.
    """
    found, open_objects, in_string, escaped = [], [], False, False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            open_objects.append([i, False])
        elif ch == "[":
            for obj in open_objects:  # every object this array sits inside holds an array
                obj[1] = True
        elif ch == "}" and open_objects:
            start, holds_array = open_objects.pop()
            if not holds_array:
                found.append(_squash(text[start:i + 1]))
    return found


def records(text: str, suffix: str) -> list[str]:
    """The units of a structured file that play the part of a row: where one line item's values sit together.

    In a text invoice a line item is one printed line. In JSON it is one object, in XML one
    element, and either may be spread over several lines of the file and may hold smaller
    objects of its own (a price with an amount and a currency). So a record is any JSON object
    that holds no array, and any XML element under which no tag repeats: the largest units that
    are still one thing and not a list of things. A CSV row is already a line.

    Args:
        text: The file's text.
        suffix: The file's extension, lowercase with the dot.

    Returns:
        The records, each squashed to one line. Empty for a format with none, or a file that doesn't parse.
    """
    if suffix == ".json":
        return _json_objects(text)
    if suffix == ".xml":
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return []

        def is_one_thing(element) -> bool:
            return not any(len({c.tag for c in e}) < len(e) for e in element.iter())

        return [_squash(" ".join(v for e in element.iter() for v in (*e.attrib.values(), e.text or "")))
                for element in root.iter() if len(element) and is_one_thing(element)]
    return []


def ungrounded(transcription: dict, text: str, extra_rows: list[str] = ()) -> list[dict]:
    """Flag every transcribed value that isn't in the source text.

    Invoice-level values, per-line notes and other_fields values are looked for anywhere in the
    document. A line item's item, quantity, unit price and amount must also belong to one row of
    the document (see _on_one_row), so a quantity borrowed from another row, or a short number
    that only matches by accident somewhere else, is caught.

    Args:
        transcription: The model's reply, in the extraction SCHEMA's shape, before any typing.
        text: The document text the model was given.
        extra_rows: Further units that count as one row, for a structured file (see records).

    Returns:
        NOT_IN_SOURCE flags (without severity), each with the field path and the value. A line
        whose values are each in the document but on no single line is flagged once, as
        "line_items[i]", with reason NOT_ON_ONE_LINE.
    """
    whole = _squash(text)
    rows = [_squash(line) for line in text.splitlines() if line.strip()] + list(extra_rows)
    flags = []

    def check(field, value):
        if isinstance(value, str) and (v := _squash(value)) and not _found(v, whole):
            flags.append({"code": "NOT_IN_SOURCE", "field": field, "value": value})
            return False
        return True

    for key, value in transcription.items():
        if key not in ("line_items", "other_fields"):
            check(_TOP_PATHS.get(key, key), value)
    for i, line in enumerate(transcription.get("line_items") or []):
        if not isinstance(line, dict):
            continue  # the builder flags it UNPARSEABLE
        check(f"line_items[{i}].note", line.get("note"))
        values = {k: _squash(line[k]) for k in _ROW_KEYS if isinstance(line.get(k), str) and line[k].strip()}
        each_found = [check(f"line_items[{i}].{k}", line[k]) for k in values]  # a list, so every value is checked
        if values and all(each_found) and not _on_one_row(values, rows):
            flags.append({"code": "NOT_IN_SOURCE", "field": f"line_items[{i}]", "value": values,
                          "reason": "NOT_ON_ONE_LINE"})
    for i, other in enumerate(transcription.get("other_fields") or []):
        if isinstance(other, dict):
            check(f"other_fields[{i}].value", other.get("value"))
    return flags
