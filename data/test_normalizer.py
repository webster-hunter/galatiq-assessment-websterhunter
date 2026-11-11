"""Run: python data/test_normalizer.py (or pytest). Uses the sample invoices as fixtures."""
from datetime import date
from decimal import Decimal
from pathlib import Path

from normalizer import load, normalize

INVOICES = Path(__file__).parent / "invoices"


def test_money_and_dates_are_typed():
    """Money loads as exact Decimal (never float) and dates load as date objects."""
    inv = load(INVOICES / "invoice_1013.json")
    assert inv.total == Decimal("22562.80") and isinstance(inv.total, Decimal)
    assert inv.line_items[3].note == "Volume discount"
    assert inv.date == date(2026, 1, 24)


def test_blanks_become_none_and_bad_values_are_kept():
    """Blank or null fields normalize to None, and bad values are kept as stated rather than corrected."""
    bad = load(INVOICES / "invoice_1009.json")
    assert bad.vendor_name is None and bad.vendor_address is None and bad.due_date is None
    assert bad.payment_terms is None
    assert bad.line_items[0].quantity == -5 and bad.total == Decimal("-250.00")


def test_unexpected_fields_are_collected(tmp_path):
    """JSON fields the loader doesn't map are collected by path (top level, vendor, line items), not dropped."""
    f = tmp_path / "inv.json"
    f.write_text('{"invoice_number": "X", "po_number": "PO-9", "vendor": {"name": "V", "tax_id": "12"},'
                 ' "line_items": [{"item": "A", "quantity": 1, "unit_price": 2, "discount": 0.5}]}')
    assert load(f).unexpected_fields == {
        "po_number": "PO-9", "vendor.tax_id": "12", "line_items[0].discount": Decimal("0.5")}
    assert load(INVOICES / "invoice_1013.json").unexpected_fields == {}  # known fields don't leak in


README_INVENTORY = {"WidgetA": 15, "WidgetB": 10, "GadgetX": 5, "FakeItem": 0}


def codes(inv):
    return [(f["code"], f.get("field") or f.get("item")) for f in inv.flags]


def test_duplicate_items_flagged_with_their_lines():
    """An item on more than one line gets one DUPLICATE_ITEM flag listing every line it appears on."""
    inv = load(INVOICES / "invoice_1013.json", README_INVENTORY)
    assert {"code": "DUPLICATE_ITEM", "item": "WidgetA", "lines": [0, 3, 6]} in inv.flags
    dupes = [c for c in codes(inv) if c[0] == "DUPLICATE_ITEM"]
    assert dupes == [("DUPLICATE_ITEM", "WidgetA"), ("DUPLICATE_ITEM", "WidgetB"),
                     ("DUPLICATE_ITEM", "GadgetX")]


def test_stock_checked_against_summed_quantity():
    """Stock is checked against each item's quantity summed across lines, and invalid quantities aren't counted."""
    # 1013: every line is within stock on its own; the sums are not.
    inv = load(INVOICES / "invoice_1013.json", README_INVENTORY)
    stock = [{k: f[k] for k in ("item", "requested", "available")} for f in inv.flags
             if f["code"] == "STOCK_EXCEEDED"]
    assert stock == [{"item": "WidgetA", "requested": 22, "available": 15},
                     {"item": "WidgetB", "requested": 18, "available": 10},
                     {"item": "GadgetX", "requested": 9, "available": 5}]

    assert load(INVOICES / "invoice_1004.json", README_INVENTORY).flags == []  # within stock: no flag
    # 1009's -5 WidgetA isn't netted against stock: no STOCK_EXCEEDED, no stock credit either.
    assert not [f for f in load(INVOICES / "invoice_1009.json", README_INVENTORY).flags
                if f["code"] == "STOCK_EXCEEDED"]


def test_invalid_values_flagged():
    """Non-positive quantities and unknown products get INVALID_VALUE; the product check is skipped without an inventory."""
    inv = load(INVOICES / "invoice_1009.json", README_INVENTORY)
    assert codes(inv)[0] == ("INVALID_VALUE", "line_items[0].quantity")
    assert inv.flags[0]["value"] == -5

    widget_c = load(INVOICES / "invoice_1016.json", README_INVENTORY)
    assert codes(widget_c) == [("INVALID_VALUE", "line_items[2].item")]
    assert load(INVOICES / "invoice_1016.json").flags == []  # no inventory given -> product check skipped


def test_negative_price_and_missing_item(tmp_path):
    """A zero quantity, a negative price, a missing price and a missing item name each get their own INVALID_VALUE flag."""
    f = tmp_path / "inv.json"
    f.write_text('{"line_items": [{"item": "WidgetA", "quantity": 0, "unit_price": -1}, {"quantity": 1}]}')
    assert codes(load(f)) == [("INVALID_VALUE", "line_items[0].quantity"),
                              ("INVALID_VALUE", "line_items[0].unit_price"),
                              ("INVALID_VALUE", "line_items[1].unit_price"),
                              ("INVALID_VALUE", "line_items[1].item")]


def unparseable(inv):
    return [(f["field"], f["value"]) for f in inv.flags if f.get("reason") == "UNPARSEABLE"]


def test_unreadable_values_become_none_and_flag_without_losing_the_invoice(tmp_path):
    """A value that can't be read becomes None plus an UNPARSEABLE flag with the raw value; the rest of the invoice still loads and is still checked."""
    f = tmp_path / "inv.json"
    f.write_text('{"invoice_number": "INV-1", "due_date": "yesterday", "total": "about 500",'
                 ' "line_items": [{"item": "WidgetA", "quantity": "lots", "unit_price": 250},'
                 '                {"item": "WidgetB", "quantity": 2, "unit_price": "NaN"},'
                 '                {"item": "WidgetA", "quantity": 1, "unit_price": 250}]}')
    inv = load(f, README_INVENTORY)
    assert inv.invoice_number == "INV-1" and inv.due_date is None and inv.total is None
    assert unparseable(inv) == [("line_items[0].quantity", "lots"), ("line_items[1].unit_price", "NaN"),
                                ("due_date", "yesterday"), ("total", "about 500")]
    # No second MISSING flag for the same fields, and the other checks still ran on what was readable.
    assert not [x for x in inv.flags if x.get("reason") == "MISSING"]
    assert {"code": "DUPLICATE_ITEM", "item": "WidgetA", "lines": [0, 2]} in inv.flags


def test_formatting_variants_are_normalized_not_flagged(tmp_path):
    """Formatting differences are normalized rather than flagged: $ and thousands commas in money, a vendor given as a plain name, and a numeric invoice number."""
    f = tmp_path / "inv.json"
    f.write_text('{"invoice_number": 1004, "revision": 0, "vendor": "Acme",'
                 ' "line_items": [{"item": "WidgetA", "quantity": 1, "unit_price": "$1,250.00"}],'
                 ' "subtotal": "1,250.00", "tax_amount": "0", "total": "-$0.00"}')
    inv = load(f)
    assert (inv.invoice_number, inv.revision, inv.vendor_name) == ("1004", "0", "Acme")
    assert inv.line_items[0].unit_price == Decimal("1250.00") and inv.subtotal == Decimal("1250.00")
    assert unparseable(inv) == []

    # Misplaced commas are not a thousands separator: flag, don't guess.
    f.write_text('{"line_items": [], "total": "1,25,0"}')
    assert unparseable(load(f)) == [("total", "1,25,0")]


def test_wrong_structure_is_flagged_not_fatal(tmp_path):
    """Structurally wrong JSON (vendor as a list, a line item that isn't an object, line_items not a list) is flagged per field instead of failing the file."""
    f = tmp_path / "inv.json"
    f.write_text('{"vendor": ["Acme"], "line_items": [{"item": "WidgetA", "quantity": 1, "unit_price": 1}, "junk"]}')
    inv = load(f, README_INVENTORY)
    assert unparseable(inv) == [("vendor", ["Acme"]), ("line_items[1]", "junk")]
    assert len(inv.line_items) == 2 and inv.line_items[0].item == "WidgetA"  # index 1 kept as a placeholder
    assert [x for x in inv.flags if x.get("reason") != "UNPARSEABLE"] == []  # placeholder adds no MISSING noise

    f.write_text('{"line_items": {"item": "WidgetA"}}')
    assert unparseable(load(f)) == [("line_items", {"item": "WidgetA"})]


def test_nan_literal_is_flagged_and_output_stays_strict_json(tmp_path):
    """A non-standard NaN/Infinity literal in JSON input is flagged UNPARSEABLE, and the tool's output is still valid strict JSON."""
    import json
    from normalizer import _json_default

    f = tmp_path / "inv.json"
    f.write_text('{"total": NaN, "subtotal": -Infinity, "line_items": []}')
    assert unparseable(load(f)) == [("subtotal", "-Infinity"), ("total", "NaN")]

    def reject(constant):
        raise ValueError(f"non-standard JSON constant in output: {constant}")
    json.loads(json.dumps(normalize([f]), default=_json_default), parse_constant=reject)


def test_null_or_text_stock_is_flagged_not_fatal():
    """A NULL or non-numeric stock value in the inventory gives STOCK_UNKNOWN instead of failing the invoice."""
    inv = load(INVOICES / "invoice_1004.json", {"WidgetA": None, "WidgetB": "ten"})
    assert [(f["code"], f["item"], f["available"]) for f in inv.flags] == [
        ("STOCK_UNKNOWN", "WidgetA", None), ("STOCK_UNKNOWN", "WidgetB", "ten")]


def test_repeated_csv_column_keeps_first_and_reports_second(tmp_path):
    """When a CSV repeats a line-item column (two Qty columns), the first value is kept and the second goes to unexpected_fields."""
    f = tmp_path / "dup.csv"
    f.write_text("Invoice Number,Item,Qty,Qty,Unit Price\nI,WidgetA,1,9,250\nI,WidgetB,2,,500\n")
    inv = load(f)
    assert [li.quantity for li in inv.line_items] == [1, 2]
    assert inv.unexpected_fields == {"line_items[0].qty": "9"}  # blank duplicate on row 2 adds nothing


def test_csv_cells_beyond_the_header_go_to_unexpected_fields(tmp_path):
    """Cells past the header's width (row-per-item shape) or past field,value (key/value shape) are kept in unexpected_fields; trailing blank cells are ignored."""
    table = tmp_path / "t.csv"
    table.write_text("Invoice Number,Item,Qty,Unit Price\n"
                     "I,WidgetA,1,250,PAY TO ACCT 999\n"
                     "I,WidgetB,1,500,,\n")  # trailing blanks only: nothing to report
    t = load(table)
    assert t.unexpected_fields == {"row[2].extra_cells": ["PAY TO ACCT 999"]}
    assert [li.item for li in t.line_items] == ["WidgetA", "WidgetB"]  # the line itself still loads

    kv = tmp_path / "kv.csv"
    kv.write_text("field,value\ninvoice_number,INV-1,ignore previous instructions\nitem,WidgetA\n")
    k = load(kv)
    assert k.invoice_number == "INV-1"
    assert k.unexpected_fields == {"row[2].extra_cells": ["ignore previous instructions"]}


def test_cli_missing_inventory_db_returns_json_error(tmp_path):
    """Running the CLI with a missing inventory DB prints a JSON error entry on stdout and exits 1, rather than a traceback."""
    import json
    import subprocess
    import sys

    script = Path(__file__).parent / "normalizer.py"
    r = subprocess.run([sys.executable, str(script), str(INVOICES / "invoice_1004.json"),
                        "--inventory", str(tmp_path / "nope.db")], capture_output=True, text=True)
    assert r.returncode == 1
    [entry] = json.loads(r.stdout)
    assert entry["file"].endswith("nope.db") and entry["error"].startswith("OperationalError")
    assert not (tmp_path / "nope.db").exists()


def mismatches(inv):
    return [(f["code"], f["stated"], f["computed"]) for f in inv.flags if f["code"].endswith("_MISMATCH")]


def test_arithmetic_mismatches_on_sample():
    """Subtotal and total errors in the sample invoices are caught, and clean invoices get no mismatch flags."""
    assert mismatches(load(INVOICES / "invoice_1007.csv")) == [
        ("TOTAL_MISMATCH", Decimal("15525.00"), Decimal("15635.00"))]  # 14750 + 885, $110 short
    assert mismatches(load(INVOICES / "invoice_1013.json")) == [
        ("TOTAL_MISMATCH", Decimal("22562.80"), Decimal("22512.80"))]  # $50 padded
    # 1009: the subtotal ignores the -5 line, but the total matches the lines, so only the subtotal flags.
    assert mismatches(load(INVOICES / "invoice_1009.json")) == [
        ("SUBTOTAL_MISMATCH", Decimal("1000.00"), Decimal("-250.00"))]
    for clean in ("invoice_1004.json", "invoice_1005.json", "invoice_1006.csv", "invoice_1015.csv"):
        assert mismatches(load(INVOICES / clean)) == [], clean


def test_tax_mismatch_rounds_to_cents_and_skips_missing_inputs(tmp_path):
    """Computed tax is rounded to the cent before comparing, and checks with missing inputs are skipped."""
    f = tmp_path / "inv.json"
    # 33.33 * 0.0725 = 2.4164... -> 2.42; stated 2.41 is a cent off.
    f.write_text('{"line_items": [{"item": "A", "quantity": 1, "unit_price": 33.33}],'
                 ' "subtotal": 33.33, "tax_rate": 0.0725, "tax_amount": 2.41, "total": 35.74}')
    assert mismatches(load(f)) == [("TAX_AMOUNT_MISMATCH", Decimal("2.41"), Decimal("2.42"))]

    f.write_text('{"line_items": [{"item": "A", "quantity": 1}], "total": 5}')  # no price, subtotal or tax
    assert mismatches(load(f)) == []


def test_total_checked_against_lines_not_stated_subtotal(tmp_path):
    """The total is recomputed from the line items plus tax, so a wrong subtotal doesn't produce a false total flag, and a wrong total is caught even with a correct-looking subtotal."""
    f = tmp_path / "inv.json"
    # Lines say 100; stated subtotal 90 is wrong; total 110 = 100 + 10 tax is right.
    f.write_text('{"line_items": [{"item": "A", "quantity": 1, "unit_price": 100}],'
                 ' "subtotal": 90, "tax_amount": 10, "total": 110}')
    assert mismatches(load(f)) == [("SUBTOTAL_MISMATCH", Decimal("90"), Decimal("100.00"))]

    # Lines can't be summed (missing price): fall back to the stated subtotal for the total check.
    f.write_text('{"line_items": [{"item": "A", "quantity": 1}], "subtotal": 100, "tax_amount": 10, "total": 120}')
    assert mismatches(load(f)) == [("TOTAL_MISMATCH", Decimal("120"), Decimal("110.00"))]


def test_second_tax_row_does_not_overwrite_first(tmp_path):
    """A repeated "Tax (x%)" footer row goes to unexpected_fields and leaves the first rate and amount paired."""
    f = tmp_path / "t.csv"
    f.write_text("Invoice Number,Item,Qty,Unit Price,Line Total\nI,A,1,100,100\n"
                 ",,,Tax (6%):,6\n,,,Tax (7%):,7\n,,,Total:,106\n")
    inv = load(f)
    assert (inv.tax_rate, inv.tax_amount) == (Decimal("0.06"), Decimal("6"))
    assert inv.unexpected_fields == {"row[4].Tax (7%):": "7"}
    assert mismatches(inv) == []


def test_flag_codes_and_reasons_are_screaming_snake_case():
    """Every flag code and reason produced on the sample invoices is in SCREAMING_SNAKE_CASE."""
    import re

    flags = [f for r in normalize([INVOICES], README_INVENTORY) for f in r["invoice"]["flags"]]
    assert flags  # the check below means nothing on an empty list
    for f in flags:
        for key in ("code", "reason"):
            if key in f:
                assert re.fullmatch(r"[A-Z][A-Z0-9]*(_[A-Z0-9]+)*", f[key]), f


def test_inventory_db_is_read(tmp_path):
    """inventory_from_db reads the README's inventory table into an item -> stock mapping."""
    import sqlite3
    from normalizer import inventory_from_db

    db = tmp_path / "inventory.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE inventory (item TEXT PRIMARY KEY, stock INTEGER)")
        conn.execute("INSERT INTO inventory VALUES ('WidgetA', 15), ('FakeItem', 0)")
    conn.close()
    assert inventory_from_db(db) == {"WidgetA": 15, "FakeItem": 0}


def test_inventory_path_with_special_characters_is_read_not_created(tmp_path):
    """A DB path containing "#" opens the real file read-only instead of silently creating an empty database."""
    import sqlite3
    from contextlib import closing
    from normalizer import inventory_from_db

    (tmp_path / "a#b").mkdir()
    with closing(sqlite3.connect(tmp_path / "a#b" / "inventory.db")) as conn, conn:
        conn.execute("CREATE TABLE inventory (item TEXT PRIMARY KEY, stock INTEGER)")
        conn.execute("INSERT INTO inventory VALUES ('WidgetA', 15)")
    assert inventory_from_db(tmp_path / "a#b" / "inventory.db") == {"WidgetA": 15}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a#b"]  # no stray "a" database next to it

    try:
        inventory_from_db(tmp_path / "missing.db")
        raise AssertionError("a missing DB must fail, not be created")
    except sqlite3.OperationalError:
        assert not (tmp_path / "missing.db").exists()


def test_csv_key_value_shape_keeps_every_repeated_item():
    """The field,value CSV shape (1006) keeps every line item even though the item keys repeat."""
    inv = load(INVOICES / "invoice_1006.csv", README_INVENTORY)
    assert [(li.item, li.quantity, li.unit_price) for li in inv.line_items] == [
        ("WidgetA", 5, Decimal("250.00")), ("WidgetB", 3, Decimal("500.00"))]  # a dict() load keeps only WidgetB
    assert (inv.vendor_name, inv.payment_terms, inv.tax_amount, inv.total) == (
        "Acme Industrial Supplies", "Net 15", Decimal("0.00"), Decimal("2750.00"))
    assert inv.unexpected_fields == {} and inv.flags == []


def test_csv_table_shape_reads_footer_and_us_dates():
    """The row-per-item CSV shape (1007) reads its footer totals, the tax rate in the "Tax (6%)" label, and US dates."""
    inv = load(INVOICES / "invoice_1007.csv", README_INVENTORY)
    assert inv.date == date(2026, 1, 28) and inv.due_date == date(2026, 2, 28)
    assert len(inv.line_items) == 3 and inv.line_items[0].amount == Decimal("5000.00")
    assert (inv.subtotal, inv.tax_rate, inv.tax_amount, inv.total) == (
        Decimal("14750.00"), Decimal("0.06"), Decimal("885.00"), Decimal("15525.00"))  # stated, not recomputed
    assert [f["item"] for f in inv.flags if f["code"] == "STOCK_EXCEEDED"] == ["WidgetA", "WidgetB"]


def test_csv_oddities_go_to_unexpected_fields(tmp_path):
    """Unknown columns, disagreeing rows, unknown or repeated footer labels and stray CSV rows go to unexpected_fields instead of being dropped or overwritten."""
    table = tmp_path / "t.csv"
    table.write_text("Invoice Number,Vendor,Item,Qty,Unit Price,PO\n"
                     "INV-1,Acme,WidgetA,1,250.00,PO-7\n"
                     "INV-1,Other Co,WidgetB,1,500.00,\n"     # vendor disagrees with row 1
                     ",,,,Shipping:,40.00\n"                  # footer label we don't map
                     ",,,,Total:,790.00\n,,,,Total:,1.00\n")  # second total must not win
    t = load(table)
    assert t.vendor_name == "Acme" and t.total == Decimal("790.00")
    assert t.unexpected_fields == {"line_items[0].po": "PO-7", "line_items[1].vendor": "Other Co",
                                   "row[4].Shipping:": "40.00", "row[6].Total:": "1.00"}

    kv = tmp_path / "kv.csv"
    kv.write_text("field,value\nquantity,3\nitem,WidgetA\nquantity,1\nquantity,2\npo_number,PO-9\n")
    k = load(kv)
    assert [(li.item, li.quantity) for li in k.line_items] == [("WidgetA", 1)]
    assert k.unexpected_fields == {"row[2].quantity": "3", "row[5].quantity": "2", "row[6].po_number": "PO-9"}


def test_directory_skips_unsupported_and_bad_file_is_reported():
    """A folder loads only supported file types, and an unsupported file named directly becomes an error entry without stopping the batch."""
    results = normalize([INVOICES, INVOICES / "invoice_1014.xml"])
    ok = [r for r in results if "invoice" in r]
    assert len(ok) == 9  # every JSON and CSV file in the folder; .txt/.xml/.pdf skipped
    assert "error" in results[-1]  # explicitly named unsupported file -> error entry, batch continues


if __name__ == "__main__":
    import tempfile

    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            if "tmp_path" in fn.__code__.co_varnames[:fn.__code__.co_argcount]:
                with tempfile.TemporaryDirectory() as d:
                    fn(Path(d))
            else:
                fn()
    print("ok")
