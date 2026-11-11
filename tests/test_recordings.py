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

# Two OCR-damaged values, both repaired; "Widget A" and "Gadget X" matched to inventory names; number "INV 1012".
OCR_1012 = ["OCR_REPAIRED", "OCR_REPAIRED", "ITEM_NAME_REPAIRED", "ITEM_NAME_REPAIRED", "UNEXPECTED_FORMAT", "NEAR_THRESHOLD"]
# file -> (vendor, total, flag codes in order)
EXPECTED = {
    "invoice_1001.txt": ("Widgets Inc.", "5000.00", []),
    "invoice_1002.txt": ("Gadgets Co.", "15000.00",  # number is a bare "1002"
                         # Needed a second attempt (see the test below); the result is clean of model errors.
                         ["UNEXPECTED_FORMAT", "OVER_APPROVAL_THRESHOLD", "STOCK_EXCEEDED"]),
    "invoice_1003.txt": ("Fraudster LLC", "100000.00",  # due "yesterday"
                         # terms "Immediate"; pressure wording in the notes and in the terms; "Wire transfer preferred"
                         ["UNPARSEABLE", "UNEXPECTED_FORMAT", "OVER_APPROVAL_THRESHOLD", "PRESSURE_LANGUAGE",
                          "PRESSURE_LANGUAGE", "WIRE_REQUEST", "STOCK_EXCEEDED"]),
    "invoice_1008.txt": ("NoProd Industries", "9900.00", ["UNKNOWN_PRODUCT", "UNKNOWN_PRODUCT", "NEAR_THRESHOLD", "UNEXPECTED_FIELDS"]),  # $100 under $10K
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
    """Each sample's real transcription gives the right vendor and total and exactly the expected flags. None names a currency, so each also carries the CURRENCY_ASSUMED note."""
    vendor, total, codes = EXPECTED[name]
    inv = load(INVOICES / name, Replay(REPLAYS), INVENTORY)
    assert (inv.vendor_name, inv.total) == (vendor, Decimal(total))
    assert [f["code"] for f in inv.flags if f["severity"] != "INFO"] == codes
    notes = ["EXTRACTION_RETRIED", "CURRENCY_ASSUMED"] if name == "invoice_1002.txt" else ["CURRENCY_ASSUMED"]
    assert [f["code"] for f in inv.flags if f["severity"] == "INFO"] == notes


def test_ocr_errors_are_copied_by_the_model_and_repaired_by_code():
    """The model left 1012's OCR damage as written ("2O26", "$3,500.O0"). Code reads both, each confirmed by another value on the invoice, and keeps the original text on the flag."""
    from datetime import date
    for name in ("invoice_1012.txt", "invoice_1012.pdf"):
        inv = load(INVOICES / name, Replay(REPLAYS), INVENTORY)
        assert (inv.date, inv.line_items[1].amount) == (date(2026, 1, 26), Decimal("3500.00"))
        assert [(f["severity"], f["field"], f["value"], f["proof"]) for f in inv.flags if f["code"] == "OCR_REPAIRED"] == [
            ("WARN", "line_items[1].amount", "$3,500.O0", "quantity 7 x unit price 500 = 3500.00"),
            ("WARN", "date", "26-Jan-2O26", "Net 30: 2026-01-26 to 2026-02-25 is 30 days")]
        assert not any(f["code"].endswith("_MISMATCH") or f["code"] == "UNPARSEABLE" for f in inv.flags)


def test_grounding_caught_a_real_model_error():
    """Grok's first transcription of 1002, before the prompt named "Amt" as a total: it put the invoice total on the line item. Every value is in the document, but not on that row."""
    first_reply = {"invoice_number": "1002", "vendor_name": "Gadgets Co.", "date": "Jan 30 2026", "due_date": "2026-01-30",
                   "total": None, "payment_terms": "Net 30", "other_fields": [],
                   "line_items": [{"item": "GadgetX", "quantity": "20", "unit_price": "$750", "amount": "$15,000.00", "note": None}]}
    [f] = ungrounded(first_reply, read_text(INVOICES / "invoice_1002.txt"))
    assert (f["code"], f["field"], f["reason"]) == ("NOT_IN_SOURCE", "line_items[0]", "NOT_ON_ONE_LINE")


def test_the_recorded_1002_was_corrected_on_its_second_attempt():
    """Real model output: Grok first copied 1002's invoice total onto the line item; told so, it returned the line without an amount, fully grounded."""
    inv = load(INVOICES / "invoice_1002.txt", Replay(REPLAYS), INVENTORY)
    assert [(a["outcome"], [(f["field"], f["reason"]) for f in a["ungrounded"]]) for a in inv.extraction_attempts] == [
        ("RETRY", [("line_items[0]", "NOT_ON_ONE_LINE")]), ("GROUNDED", [])]
    assert inv.total == Decimal("15000.00") and inv.line_items[0].amount is None
    assert not any(f["code"] == "NOT_IN_SOURCE" for f in inv.flags)


def test_only_1002_needed_a_retry():
    """Across the ten recorded samples, nine were grounded on the first attempt."""
    attempts = {name: len(load(INVOICES / name, Replay(REPLAYS), INVENTORY).extraction_attempts) for name in EXPECTED}
    assert {n for n, count in attempts.items() if count > 1} == {"invoice_1002.txt"} and attempts["invoice_1002.txt"] == 2


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
    both_1012 = {(n, "invoice_number", "INV 1012") for n in ("invoice_1012.txt", "invoice_1012.pdf")}
    assert found == both_1012 | {("invoice_1002.txt", "invoice_number", "1002"),
                                 ("invoice_1003.txt", "payment_terms", "Immediate")}


def test_1012_is_fully_read_and_carries_no_review_or_block_flag():
    """After repair, 1012 is a normal invoice: inventory item names, stock checked and within limits, arithmetic reconciled. What remains are warnings recording what was changed."""
    for name in ("invoice_1012.txt", "invoice_1012.pdf"):
        inv = load(INVOICES / name, Replay(REPLAYS), INVENTORY)
        assert [(li.item, li.quantity) for li in inv.line_items] == [("WidgetA", 12), ("WidgetB", 7), ("GadgetX", 4)]
        assert [(f["value"], f["repaired"]) for f in inv.flags if f["code"] == "ITEM_NAME_REPAIRED"] == [
            ("Widget A", "WidgetA"), ("Gadget X", "GadgetX")]
        assert {f["severity"] for f in inv.flags} <= {"WARN", "INFO"}
