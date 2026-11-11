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

OCR_1012 = ["UNPARSEABLE", "UNPARSEABLE", "UNKNOWN_PRODUCT", "UNKNOWN_PRODUCT", "UNEXPECTED_FIELDS"]
# file -> (vendor, total, flag codes in order)
EXPECTED = {
    "invoice_1001.txt": ("Widgets Inc.", "5000.00", []),
    "invoice_1002.txt": ("Gadgets Co.", "15000.00", ["UNPARSEABLE", "STOCK_EXCEEDED"]),  # date "Jan 30 2026"
    "invoice_1003.txt": ("Fraudster LLC", "100000.00", ["UNPARSEABLE", "STOCK_EXCEEDED"]),  # due "yesterday"
    "invoice_1008.txt": ("NoProd Industries", "9900.00", ["UNKNOWN_PRODUCT", "UNKNOWN_PRODUCT", "UNEXPECTED_FIELDS"]),
    # The bill-to (Acme Corp Manufacturing Division) is not the vendor; the rush line is WidgetA with a note.
    "invoice_1010.txt": ("Consolidated Materials Group", "7185.00", ["UNEXPECTED_FIELDS", "DUPLICATE_ITEM"]),
    "invoice_1011.txt": ("Summit Manufacturing Co.", "3000.00", []),
    "invoice_1011.pdf": ("Summit Manufacturing Co.", "3000.00", []),
    "invoice_1012.txt": ("QuickShip Distributers", "9975.00", OCR_1012),  # FROM, not the TO party
    "invoice_1012.pdf": ("QuickShip Distributers", "9975.00", OCR_1012),
    "invoice_1013.pdf": ("Atlas Industrial Supply", "22562.80",
                         ["DUPLICATE_ITEM"] * 3 + ["STOCK_EXCEEDED"] * 3 + ["TOTAL_MISMATCH"]),
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
