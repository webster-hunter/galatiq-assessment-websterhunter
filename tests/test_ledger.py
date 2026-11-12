"""The ledger on its own: identity, the content fingerprint, and paying exactly once."""
from decimal import Decimal
from pathlib import Path

import pytest

import normalizer
from ledger import Ledger, payment_key
from ledger.ledger import content_hash, identity
from normalizer.models import Invoice, LineItem

INVOICES = Path(__file__).resolve().parent.parent / "data" / "invoices"


def invoice(number="INV-1001", vendor="Widgets Inc.", total="5000.00", revision=None, source="a.json", lines=(("WidgetA", 10, 250),)):
    return Invoice(invoice_number=number, vendor_name=vendor, vendor_address=None, date=None, due_date=None,
                   line_items=[LineItem(i, Decimal(q), Decimal(p)) for i, q, p in lines], subtotal=None, tax_rate=None,
                   tax_amount=None, shipping=None, total=Decimal(total) if total is not None else None, currency=None,
                   payment_terms=None, revision=revision, source_file=source)


def test_identity_ignores_how_the_number_and_vendor_are_written():
    """"INV-1012", "INV 1012" and "1012" are one number, a vendor is matched without case or punctuation, and "R1" is revision 1."""
    assert identity(invoice("INV-1012")) == identity(invoice("INV 1012")) == identity(invoice("1012")) == ("widgetsinc", "1012", 0)
    assert identity(invoice(vendor="WIDGETS, INC")) == identity(invoice(vendor="Widgets Inc."))
    assert identity(invoice(revision="R1"))[2] == 1
    assert identity(invoice(vendor="Other Co")) != identity(invoice())  # two vendors can both issue a 1001
    assert identity(invoice(number=None)) is None


def test_fingerprint_follows_what_is_billed_not_how_it_is_written():
    """The same lines and total match whatever the number formatting ("5000" or "5000.00"); a changed quantity or total doesn't."""
    assert content_hash(invoice(total="5000")) == content_hash(invoice(total="5000.00"))
    assert content_hash(invoice()) != content_hash(invoice(total="5001.00"))
    assert content_hash(invoice()) != content_hash(invoice(lines=(("WidgetA", 11, 250),)))


def test_sample_copies_in_other_formats_have_the_same_fingerprint():
    """1013 as JSON and 1004 against its revision: the same file content matches itself, and a revision is a different invoice."""
    a = normalizer.load(INVOICES / "invoice_1004.json")
    b = normalizer.load(INVOICES / "invoice_1004_revised.json")
    assert identity(a)[:2] == identity(b)[:2] and (identity(a)[2], identity(b)[2]) == (0, 1)
    assert content_hash(a) != content_hash(b)


def test_seen_classifies_each_kind_of_arrival():
    """New, the same file again, another copy, a conflict, a newer revision of a paid or unpaid invoice, an older one, and one with no number."""
    ledger = Ledger(":memory:")
    first = invoice()
    assert ledger.seen(first).kind == "NEW"
    ledger.record(first, "HELD")
    assert ledger.seen(first).kind == "SAME_FILE"
    assert ledger.seen(invoice(source="copy.pdf")).kind == "DUPLICATE"
    assert ledger.seen(invoice(source="copy.pdf", total="9999")).kind == "CONFLICT"
    r1 = invoice(revision="R1", source="r1.json", total="6000")
    assert ledger.seen(r1).kind == "REVISION_OF_UNPAID"
    ledger.record(first, "PAID")
    found = ledger.seen(r1)
    assert (found.kind, found.earlier["source_file"], found.earlier["total"]) == ("REVISION_OF_PAID", "a.json", "5000")
    ledger.record(r1, "HELD")
    assert ledger.seen(invoice(source="late.json", revision=None, total="1")).kind == "CONFLICT"  # revision 0 is on record
    assert ledger.seen(invoice(number="INV-2000", revision=None)).kind == "NEW"
    ledger.record(invoice(number="INV-3000", revision="R2", source="r2.json"), "HELD")
    assert ledger.seen(invoice(number="INV-3000", revision="R1", source="r1.json")).kind == "SUPERSEDED"
    assert ledger.seen(invoice(number=None)).kind == "UNIDENTIFIED"


def test_an_invoice_is_paid_once_however_often_it_is_asked():
    """The second pay() for the same invoice calls nothing and reports the first payment."""
    calls = []
    ledger = Ledger(":memory:", pay=lambda vendor, amount: calls.append((vendor, amount)))
    inv = invoice()
    first, second = ledger.pay(inv), ledger.pay(inv)
    assert calls == [("Widgets Inc.", Decimal("5000.00"))]
    assert (first["status"], first["repeated"], second["status"], second["repeated"]) == ("PAID", False, "PAID", True)
    assert first["payment_key"] == second["payment_key"] == payment_key("widgetsinc", "1001", 0) == "widgetsinc/1001/r0"
    assert ledger.total_paid() == Decimal("5000")
    ledger.pay(invoice(revision="R1", source="r1.json", total="100"))  # a different invoice: its own key
    assert len(calls) == 2


def test_a_payment_that_never_confirmed_is_not_sent_again():
    """If the payment call fails, pay() reports it without raising and the PENDING row remains. A retry does not call the payment function again: the first call may have gone through."""
    def flaky(vendor, amount):
        raise TimeoutError("no answer from the bank")

    ledger = Ledger(":memory:", pay=flaky)
    first = ledger.pay(invoice())
    assert (first["status"], first["repeated"], first["error"]) == ("PENDING", False, "TimeoutError: no answer from the bank")
    calls = []
    ledger._pay = lambda vendor, amount: calls.append(amount)
    again = ledger.pay(invoice())
    assert calls == [] and (again["status"], again["repeated"]) == ("PENDING", True)
    assert ledger.total_paid() == 0


def test_only_a_payable_invoice_can_be_paid():
    """The last guard before money moves: no invoice number, no total, or a total that isn't positive is refused."""
    ledger = Ledger(":memory:", pay=lambda v, a: None)
    for bad in (invoice(number=None), invoice(total=None), invoice(total="0"), invoice(total="-250")):
        with pytest.raises(ValueError, match="refusing to pay"):
            ledger.pay(bad)
    assert ledger.payments() == []


def test_the_ledger_file_outlives_the_object(tmp_path):
    """A ledger on disk is still there for the next process: the invoice is recognised and not paid again."""
    path = tmp_path / "ledger.db"
    first = Ledger(path, pay=lambda v, a: None)
    first.pay(invoice())
    first.record(invoice(), "PAID")
    first.close()
    later = Ledger(path, pay=lambda v, a: pytest.fail("paid twice"))
    assert later.seen(invoice()).kind == "SAME_FILE" and later.pay(invoice())["repeated"] is True
