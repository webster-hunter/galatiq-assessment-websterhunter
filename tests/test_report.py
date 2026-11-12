"""The self-correction report on the recorded samples: what the retry loop and the repairs change, measured."""
from pathlib import Path

from extraction import Replay
from extraction.report import main, measure, render, summarize

ROOT = Path(__file__).resolve().parent.parent
INVOICES, REPLAYS = ROOT / "data" / "invoices", ROOT / "data" / "replays"
INVENTORY = {"WidgetA": 15, "WidgetB": 10, "GadgetX": 5, "FakeItem": 0}


def rows():
    return {r["file"]: r for r in measure([INVOICES], Replay(REPLAYS), INVENTORY)}


def test_self_correction_helps_three_files_and_harms_none():
    """Across the 20 samples: 1002 (retry) and both copies of 1012 (repairs) end with fewer holding flags; nothing gets worse; it costs one extra model call."""
    s = summarize(list(rows().values()))
    assert (s["files"], s["errors"], s["better"], s["worse"], s["same"]) == (20, 0, 3, 0, 17)
    assert (s["model_calls_without"], s["model_calls_with"]) == (10, 11)
    assert (s["retries_resolved"], s["retries_unresolved"], s["ocr_repairs"], s["name_repairs"]) == (1, 0, 4, 4)
    assert (s["holding_flags_before"], s["holding_flags_after"]) == (41, 32)
    assert s["no_longer_held"] == ["invoice_1012.pdf", "invoice_1012.txt"]


def test_what_changed_for_each_improved_file():
    """1002: the model's own error is gone after one retry, the vendor's problems remain. 1012: four holding flags become none."""
    r = rows()
    a = r["invoice_1002.txt"]
    assert (a["attempts"], a["retry"], a["change"]) == (2, "RESOLVED", "BETTER")
    assert a["before"]["holding"] == ["NOT_IN_SOURCE", "OVER_APPROVAL_THRESHOLD", "STOCK_EXCEEDED"]
    assert a["after"]["holding"] == ["OVER_APPROVAL_THRESHOLD", "STOCK_EXCEEDED"]
    b = r["invoice_1012.txt"]
    assert (b["attempts"], b["ocr_repairs"], b["name_repairs"], b["change"]) == (1, 2, 2, "BETTER")
    assert b["before"]["holding"] == ["UNPARSEABLE", "UNPARSEABLE", "UNKNOWN_PRODUCT", "UNKNOWN_PRODUCT"]
    assert (b["before"]["worst"], b["after"]["worst"], b["after"]["holding"]) == ("REVIEW", "WARN", [])


def test_the_vendors_problems_are_left_alone():
    """Self-correction changes nothing on the invoices that are wrong in themselves: 1003, 1007, 1009, 1013 keep every holding flag."""
    r = rows()
    for name in ("invoice_1003.txt", "invoice_1007.csv", "invoice_1009.json", "invoice_1013.json", "invoice_1013.pdf"):
        assert r[name]["change"] == "SAME" and r[name]["before"]["holding"] == r[name]["after"]["holding"], name
        assert r[name]["before"]["holding"], name


def test_report_prints_a_table_and_a_missing_recording_is_an_error_row(tmp_path, capsys):
    """The CLI prints one line per file and the summary; a file with no recording is reported, not fatal."""
    table = render(list(rows().values()))
    assert "invoice_1012.txt" in table and "REVIEW -> WARN" in table and "3 better, 0 worse, 17 unchanged" in table
    out = measure([INVOICES / "invoice_1001.txt", INVOICES / "invoice_1004.json"], Replay(tmp_path), INVENTORY)
    assert out[0]["error"].startswith("ProviderError") and out[1]["change"] == "SAME"
    assert main([str(INVOICES / "invoice_1004.json"), "--inventory", str(tmp_path / "missing.db")]) == 1
    assert "seed_inventory.py" in capsys.readouterr().err


def test_the_saved_demo_report_is_current():
    """demo/self_correction_report.txt is what the report prints today, so the demo numbers can't drift from the code."""
    saved = (ROOT / "demo" / "self_correction_report.txt").read_text(encoding="utf-8")
    assert saved.rstrip("\n") == render(list(rows().values()))
