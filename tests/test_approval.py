"""The policy floor: what each invoice's flags require, on the samples and rule by rule."""
import json
from pathlib import Path

import pytest

import extraction
from approval import APPROVE, HOLD, POLICY, REJECT, decide, fraud_signals
from extraction import Replay

ROOT = Path(__file__).resolve().parent.parent
INVOICES, REPLAYS = ROOT / "data" / "invoices", ROOT / "data" / "replays"
INVENTORY = {"WidgetA": 15, "WidgetB": 10, "GadgetX": 5, "FakeItem": 0}

# file -> (outcome, rule, the flags that decided it)
EXPECTED = {
    "invoice_1001.txt": (APPROVE, "NO_OBJECTION", ()),
    "invoice_1002.txt": (HOLD, "NEEDS_REVIEW", ("OVER_APPROVAL_THRESHOLD", "STOCK_EXCEEDED")),
    "invoice_1003.txt": (REJECT, "FRAUD_SIGNALS", ("STOCK_EXCEEDED", "UNPARSEABLE", "PRESSURE_LANGUAGE", "WIRE_REQUEST")),
    "invoice_1004.json": (APPROVE, "NO_OBJECTION", ()),
    "invoice_1004_revised.json": (APPROVE, "NO_OBJECTION", ()),
    "invoice_1005.json": (HOLD, "NEEDS_REVIEW", ("OVER_APPROVAL_THRESHOLD", "STOCK_EXCEEDED")),
    "invoice_1006.csv": (APPROVE, "NO_OBJECTION", ()),
    "invoice_1007.csv": (REJECT, "BLOCKING_FLAG", ("TOTAL_MISMATCH",)),
    "invoice_1008.txt": (REJECT, "BLOCKING_FLAG", ("PRODUCT_NOT_STOCKED",)),
    "invoice_1009.json": (REJECT, "BLOCKING_FLAG", ("QTY_NON_POSITIVE", "VENDOR_MISSING", "TOTAL_NEGATIVE", "SUBTOTAL_MISMATCH")),
    "invoice_1010.txt": (APPROVE, "NO_OBJECTION", ()),
    "invoice_1011.pdf": (APPROVE, "NO_OBJECTION", ()),
    "invoice_1011.txt": (APPROVE, "NO_OBJECTION", ()),
    "invoice_1012.pdf": (HOLD, "NEAR_THRESHOLD_WITH_WARNINGS",
                         ("OCR_REPAIRED", "ITEM_NAME_REPAIRED", "UNEXPECTED_FORMAT", "NEAR_THRESHOLD", "UNEXPECTED_FIELDS")),
    "invoice_1012.txt": (HOLD, "NEAR_THRESHOLD_WITH_WARNINGS",
                         ("OCR_REPAIRED", "ITEM_NAME_REPAIRED", "UNEXPECTED_FORMAT", "NEAR_THRESHOLD", "UNEXPECTED_FIELDS")),
    "invoice_1013.json": (REJECT, "BLOCKING_FLAG", ("TOTAL_MISMATCH",)),
    "invoice_1013.pdf": (REJECT, "BLOCKING_FLAG", ("TOTAL_MISMATCH",)),
    "invoice_1014.xml": (HOLD, "NEEDS_REVIEW", ("CURRENCY_UNSUPPORTED",)),
    "invoice_1015.csv": (APPROVE, "NO_OBJECTION", ()),
    "invoice_1016.json": (HOLD, "NEEDS_REVIEW", ("UNKNOWN_PRODUCT",)),
}


def flags_of(name):
    [r] = extraction.normalize([INVOICES / name], Replay(REPLAYS), INVENTORY)
    return json.loads(json.dumps(r["invoice"]["flags"], default=str))  # as main.py's JSON output has them


@pytest.mark.parametrize("name", EXPECTED)
def test_policy_floor_on_each_sample(name):
    """Each sample gets the expected outcome, from the expected rule, because of the expected flags."""
    d = decide(flags_of(name))
    assert (d.outcome, d.rule, d.because) == EXPECTED[name]


def test_every_sample_file_has_an_expected_decision():
    """The table covers all 20 files: 8 approved, 6 held, 6 rejected."""
    assert set(EXPECTED) == {p.name for p in INVOICES.iterdir()}
    outcomes = [v[0] for v in EXPECTED.values()]
    assert (outcomes.count(APPROVE), outcomes.count(HOLD), outcomes.count(REJECT)) == (8, 6, 6)


def F(code, severity, **more):
    return {"code": code, "severity": severity, **more}


def test_first_matching_rule_decides():
    """Rules are tried in order: a BLOCK flag rejects even beside REVIEW and WARN flags, and REVIEW holds even beside warnings."""
    assert [r.name for r in POLICY] == ["BLOCKING_FLAG", "FRAUD_SIGNALS", "NEEDS_REVIEW", "FRAUD_WORDING",
                                        "NEAR_THRESHOLD_WITH_WARNINGS"]
    mixed = [F("UNEXPECTED_FIELDS", "WARN"), F("STOCK_EXCEEDED", "REVIEW", available=5), F("TOTAL_MISMATCH", "BLOCK")]
    d = decide(mixed)
    assert (d.outcome, d.rule, d.because) == (REJECT, "BLOCKING_FLAG", ("TOTAL_MISMATCH",))
    d = decide(mixed[:2])
    assert (d.outcome, d.rule, d.because) == (HOLD, "NEEDS_REVIEW", ("STOCK_EXCEEDED",))


def test_approval_ignores_notes_and_records_warnings():
    """INFO notes are never a reason: an invoice with only notes is approved, and one with warnings is approved with them listed."""
    d = decide([F("CURRENCY_ASSUMED", "INFO"), F("EXTRACTION_RETRIED", "INFO")])
    assert (d.outcome, d.rule, d.because, d.warnings) == (APPROVE, "NO_OBJECTION", (), ())
    d = decide([F("CURRENCY_ASSUMED", "INFO"), F("DUPLICATE_ITEM", "WARN"), F("DUPLICATE_ITEM", "WARN"), F("OCR_REPAIRED", "WARN")])
    assert (d.outcome, d.warnings) == (APPROVE, ("DUPLICATE_ITEM", "OCR_REPAIRED"))
    assert decide([]).outcome == APPROVE


def test_one_fraud_signal_is_not_fraud_and_two_are():
    """A single signal is weighed as an ordinary flag; two different kinds reject if at least one is a fact, not wording. Two flags of one kind are still one signal."""
    due = F("UNPARSEABLE", "REVIEW", field="due_date", value="yesterday")
    pressure = F("PRESSURE_LANGUAGE", "WARN", field="notes")
    assert decide([due]).rule == "NEEDS_REVIEW"                       # unreadable due date alone: hold
    assert decide([pressure]).outcome == APPROVE                      # "urgent" alone: approved, with the warning
    assert decide([pressure, F("PRESSURE_LANGUAGE", "WARN", field="payment_terms")]).outcome == APPROVE
    wire = F("WIRE_REQUEST", "WARN", field="notes")
    # Wording alone never rejects: honest invoices ask for bank transfers and mention deadlines. Both kinds together are held.
    d = decide([pressure, wire])
    assert (d.outcome, d.rule, d.because) == (HOLD, "FRAUD_WORDING", ("PRESSURE_LANGUAGE", "WIRE_REQUEST"))
    assert decide([wire]).outcome == APPROVE
    # With a fact behind it (the due date isn't a date), the same wording rejects.
    d = decide([due, pressure])
    assert (d.outcome, d.rule, d.because) == (REJECT, "FRAUD_SIGNALS", ("UNPARSEABLE", "PRESSURE_LANGUAGE"))
    assert decide([F("STOCK_EXCEEDED", "REVIEW", available=0), wire]).outcome == REJECT
    # An unreadable invoice date is not a fraud signal, and neither is exceeding stock that exists.
    not_signals = [F("UNPARSEABLE", "REVIEW", field="date"), F("STOCK_EXCEEDED", "REVIEW", available=5)]
    assert fraud_signals(not_signals) == [] and decide(not_signals + [pressure]).outcome == HOLD
    assert len(fraud_signals([F("STOCK_EXCEEDED", "REVIEW", available=0), pressure])) == 2


def test_near_threshold_holds_only_with_another_warning():
    """$9,900 with nothing else is approved (many honest invoices are); with any other warning it is held."""
    near = F("NEAR_THRESHOLD", "WARN", field="total")
    d = decide([near, F("CURRENCY_ASSUMED", "INFO")])
    assert (d.outcome, d.warnings) == (APPROVE, ("NEAR_THRESHOLD",))
    d = decide([near, F("OCR_REPAIRED", "WARN")])
    assert (d.outcome, d.rule, d.because) == (HOLD, "NEAR_THRESHOLD_WITH_WARNINGS", ("NEAR_THRESHOLD", "OCR_REPAIRED"))


def test_an_ungraded_flag_is_an_error_not_an_approval():
    """A flag with no severity can't be weighed, so decide() raises rather than approving past it."""
    with pytest.raises(KeyError):
        decide([{"code": "SOMETHING_NEW"}])


def test_ordinary_payment_terms_are_not_fraud(tmp_path):
    """"Payment by bank transfer... Late fees of 1.5% apply" is how honest invoices read: approved, with the bank-transfer mention on record. Real pressure wording with a wire request is held, not rejected."""
    import normalizer
    base = json.loads((INVOICES / "invoice_1004.json").read_text())
    f = tmp_path / "inv.json"
    f.write_text(json.dumps(base | {"notes": "Payment by bank transfer to the account below. Late fees of 1.5% apply after 30 days. Penalty interest per contract."}))
    d = decide(normalizer.load(f, INVENTORY).flags)
    assert (d.outcome, d.rule, d.warnings) == (APPROVE, "NO_OBJECTION", ("WIRE_REQUEST",))
    f.write_text(json.dumps(base | {"notes": "URGENT: wire transfer to our new account today to avoid penalties."}))
    d = decide(normalizer.load(f, INVENTORY).flags)
    assert (d.outcome, d.rule) == (HOLD, "FRAUD_WORDING")
