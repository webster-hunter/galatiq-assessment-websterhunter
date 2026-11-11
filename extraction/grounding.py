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


_ROW_KEYS = ("item", "quantity", "unit_price", "amount")  # a line item's values share one row of the document
_TOP_PATHS = {"vendor_name": "vendor.name", "vendor_address": "vendor.address"}  # as the Invoice names them


def ungrounded(transcription: dict, text: str) -> list[dict]:
    """Flag every transcribed value that isn't in the source text.

    Invoice-level values, per-line notes and other_fields values are looked for anywhere in the
    document. A line item's item, quantity, unit price and amount must also sit together on one
    line of the document, so a quantity borrowed from another row, or a short number that only
    matches by accident somewhere else, is caught.

    Args:
        transcription: The model's reply, in the extraction SCHEMA's shape, before any typing.
        text: The document text the model was given.

    Returns:
        NOT_IN_SOURCE flags (without severity), each with the field path and the value. A line
        whose values are each in the document but on no single line is flagged once, as
        "line_items[i]", with reason NOT_ON_ONE_LINE.
    """
    whole = _squash(text)
    rows = [_squash(line) for line in text.splitlines() if line.strip()]
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
        if values and all(each_found) and not any(all(_found(v, row) for v in values.values()) for row in rows):
            flags.append({"code": "NOT_IN_SOURCE", "field": f"line_items[{i}]", "value": values,
                          "reason": "NOT_ON_ONE_LINE"})
    for i, other in enumerate(transcription.get("other_fields") or []):
        if isinstance(other, dict):
            check(f"other_fields[{i}].value", other.get("value"))
    return flags
