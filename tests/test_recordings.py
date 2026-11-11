"""The recorded Grok replies in data/replays, replayed with no API key: what real model output produces.

These are the model's actual transcriptions of the ten TXT and PDF samples, so they test the
prompt, the grounding check and the checks together. If the prompt, the schema or a sample file
changes, the recordings no longer match and these fail with "no recording": record again with a
key (python main.py --invoice_path=data/invoices) and review what changed.
"""
from decimal import Decimal
from pathlib import Path

import pytest

from extraction import Replay, load
from extraction.grounding import ungrounded
from extraction import read_text

ROOT = Path(__file__).resolve().parent.parent
INVOICES, REPLAYS = ROOT / "data" / "invoices", ROOT / "data" / "replays"
INVENTORY = {"WidgetA": 15, "WidgetB": 10, "GadgetX": 5, "FakeItem": 0}

# Two OCR-damaged values; "Widget A" and "Gadget X" each unknown and oddly formed; number "INV 1012".
OCR_1012 = ["UNPARSEABLE", "UNPARSEABLE", "UNKNOWN_PRODUCT", "UNEXPECTED_FORMAT", "UNKNOWN_PRODUCT", "UNEXPECTED_FORMAT",
            "UNEXPECTED_FORMAT"]
# file -> (vendor, total, flag codes in order)
EXPECTED = {
    "invoice_1001.txt": ("Widgets Inc.", "5000.00", []),
    "invoice_1002.txt": ("Gadgets Co.", "15000.00",  # number is a bare "1002"
                         # NOT_IN_SOURCE is a real model error in this recording: see the test below.
                         ["NOT_IN_SOURCE", "UNEXPECTED_FORMAT", "OVER_APPROVAL_THRESHOLD", "STOCK_EXCEEDED"]),
    "invoice_1003.txt": ("Fraudster LLC", "100000.00",  # due "yesterday"
                         ["UNPARSEABLE", "UNEXPECTED_FORMAT", "OVER_APPROVAL_THRESHOLD", "STOCK_EXCEEDED"]),  # terms "Immediate"
    "invoice_1008.txt": ("NoProd Industries", "9900.00", ["UNKNOWN_PRODUCT", "UNKNOWN_PRODUCT", "UNEXPECTED_FIELDS"]),
    # The bill-to (Acme Corp Manufacturing Division) is not the vendor; the rush line is WidgetA with a note.
    "invoice_1010.txt": ("Consolidated Materials Group", "7185.00", ["UNEXPECTED_FIELDS", "DUPLICATE_ITEM"]),
    "invoice_1011.txt": ("Summit Manufacturing Co.", "3000.00", []),
    "invoice_1011.pdf": ("Summit Manufacturing Co.", "3000.00", []),
    # Vendor is the FROM party alone: not the TO party, and without the "(formerly FastShip Ltd.)" remark.
    "invoice_1012.txt": ("QuickShip Distributers", "9975.00", OCR_1012 + ["UNEXPECTED_FIELDS"]),
    "invoice_1012.pdf": ("QuickShip Distributers", "9975.00", OCR_1012 + ["UNEXPECTED_FIELDS"]),
    "invoice_1013.pdf": ("Atlas Industrial Supply", "22562.80",
                         ["OVER_APPROVAL_THRESHOLD"] + ["DUPLICATE_ITEM"] * 3 + ["STOCK_EXCEEDED"] * 3
                         + ["TOTAL_MISMATCH"]),
}


@pytest.mark.parametrize("name", EXPECTED)
def test_recorded_extraction_of_each_sample(name):
    """Each sample's real transcription gives the right vendor and total and exactly the expected flags, with nothing ungrounded."""
    vendor, total, codes = EXPECTED[name]
    inv = load(INVOICES / name, Replay(REPLAYS), INVENTORY)
    assert (inv.vendor_name, inv.total) == (vendor, Decimal(total))
    assert [f["code"] for f in inv.flags] == codes


def test_ocr_errors_are_copied_not_corrected():
    """The model left 1012's OCR damage as written ("2O26", "$3,500.O0"), so code flags it instead of the model guessing."""
    inv = load(INVOICES / "invoice_1012.txt", Replay(REPLAYS), INVENTORY)
    assert {(f["field"], f["value"]) for f in inv.flags if f["code"] == "UNPARSEABLE"} == {
        ("date", "26-Jan-2O26"), ("line_items[1].amount", "$3,500.O0")}


def test_grounding_caught_a_real_model_error():
    """Grok's first transcription of 1002, before the prompt named "Amt" as a total: it put the invoice total on the line item. Every value is in the document, but not on that row."""
    first_reply = {"invoice_number": "1002", "vendor_name": "Gadgets Co.", "date": "Jan 30 2026", "due_date": "2026-01-30",
                   "total": None, "payment_terms": "Net 30", "other_fields": [],
                   "line_items": [{"item": "GadgetX", "quantity": "20", "unit_price": "$750", "amount": "$15,000.00", "note": None}]}
    [f] = ungrounded(first_reply, read_text(INVOICES / "invoice_1002.txt"))
    assert (f["code"], f["field"], f["reason"]) == ("NOT_IN_SOURCE", "line_items[0]", "NOT_ON_ONE_LINE")


def test_the_recorded_1002_has_a_model_error_and_grounding_holds_it():
    """In the committed recording Grok copied 1002's invoice total onto its line item as well, against the prompt. The total is right, and grounding flags the line (REVIEW): the amount isn't on that row."""
    inv = load(INVOICES / "invoice_1002.txt", Replay(REPLAYS), INVENTORY)
    assert inv.total == Decimal("15000.00") and inv.line_items[0].amount == Decimal("15000.00")
    [f] = [f for f in inv.flags if f["code"] == "NOT_IN_SOURCE"]
    assert (f["severity"], f["field"], f["reason"]) == ("REVIEW", "line_items[0]", "NOT_ON_ONE_LINE")


def test_former_vendor_name_is_kept_apart_from_the_vendor():
    """1012's "(formerly FastShip Ltd.)" is not part of the vendor name; it is kept for review in unexpected_fields."""
    inv = load(INVOICES / "invoice_1012.txt", Replay(REPLAYS), INVENTORY)
    assert inv.vendor_name == "QuickShip Distributers" and inv.currency is None
    assert inv.unexpected_fields["other.formerly"] == "FastShip Ltd."


def test_1002_date_without_a_comma_is_read():
    """1002's "Jan 30 2026" is a date, not an unreadable value; the invoice is held for stock and its amount, not for our parser."""
    from datetime import date
    inv = load(INVOICES / "invoice_1002.txt", Replay(REPLAYS), INVENTORY)
    assert inv.date == date(2026, 1, 30) and not any(f["code"] == "UNPARSEABLE" for f in inv.flags)


def test_unexpected_formats_on_the_samples():
    """Across all ten recorded samples, exactly these values are not in their field's usual form."""
    found = {(name, f["field"], f["value"]) for name in EXPECTED
             for f in load(INVOICES / name, Replay(REPLAYS), INVENTORY).flags if f["code"] == "UNEXPECTED_FORMAT"}
    both_1012 = {(n, field, value) for n in ("invoice_1012.txt", "invoice_1012.pdf") for field, value in (
        ("invoice_number", "INV 1012"), ("line_items[0].item", "Widget A"), ("line_items[2].item", "Gadget X"))}
    assert found == both_1012 | {("invoice_1002.txt", "invoice_number", "1002"),
                                 ("invoice_1003.txt", "payment_terms", "Immediate")}
