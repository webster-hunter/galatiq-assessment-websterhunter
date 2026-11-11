"""The pipeline graph: its shape, the path each invoice takes, and that it decides exactly as the policy does."""
import json
from decimal import Decimal
from pathlib import Path

import pytest

import main
from approval import decide
from extraction import Replay
from ledger import Ledger
from pipeline import build_graph, run

ROOT = Path(__file__).resolve().parent.parent
INVOICES, REPLAYS = ROOT / "data" / "invoices", ROOT / "data" / "replays"
INVENTORY = {"WidgetA": 15, "WidgetB": 10, "GadgetX": 5, "FakeItem": 0}


@pytest.fixture
def ledger():
    return Ledger(":memory:", pay=lambda vendor, amount: {"status": "success"})  # a fresh, silent ledger per test


@pytest.fixture
def graph(ledger):
    return build_graph(Replay(REPLAYS), INVENTORY, ledger)


def nodes(state):
    return [step["node"] for step in state["trail"]]


def test_the_graph_has_the_stages_and_three_branch_points(graph):
    """One fixed path in (ingest, validate, register, approve) with a branch after ingest (unreadable file), after register (already handled) and after approve (the three outcomes)."""
    drawn = graph.get_graph()
    assert set(drawn.nodes) == {"__start__", "ingest", "validate", "register", "skip", "approve", "pay", "hold",
                                "reject", "failed", "__end__"}
    fixed = {(e.source, e.target) for e in drawn.edges if not e.conditional}
    branches = {(e.source, e.target) for e in drawn.edges if e.conditional}
    assert fixed == {("__start__", "ingest"), ("validate", "register"), ("pay", "__end__"), ("hold", "__end__"),
                     ("reject", "__end__"), ("skip", "__end__"), ("failed", "__end__")}
    assert branches == {("ingest", "validate"), ("ingest", "failed"), ("register", "approve"), ("register", "skip"),
                        ("approve", "pay"), ("approve", "hold"), ("approve", "reject")}


@pytest.mark.parametrize("name, last, status", [
    ("invoice_1001.txt", "pay", "PAID"),
    ("invoice_1012.pdf", "hold", "HELD"),
    ("invoice_1003.txt", "reject", "REJECTED"),
    ("invoice_1009.json", "reject", "REJECTED"),
])
def test_each_outcome_takes_its_own_branch(graph, name, last, status):
    """The trail lists the nodes that ran, in order (each node appends one entry), ending at the branch the decision chose."""
    state = run(graph, INVOICES / name)
    assert nodes(state) == ["ingest", "validate", "register", "approve", last]
    assert state["result"]["status"] == status and "error" not in state


def test_the_graph_decides_every_new_invoice_as_the_policy_does(graph):
    """Wiring adds nothing and loses nothing: for every file that reaches approval, the graph's decision is decide() on that invoice's flags, and the branch matches it."""
    branch = {"APPROVE": "pay", "HOLD": "hold", "REJECT": "reject"}
    for p in sorted(INVOICES.iterdir()):
        state = run(graph, p)
        if "decision" in state:
            assert state["decision"] == decide(state["invoice"].flags), p.name
            assert nodes(state)[-1] == branch[state["decision"].outcome], p.name
        else:
            assert nodes(state)[-1] == "skip", p.name


def test_the_sample_folder_pays_six_invoices_once(graph, ledger):
    """All 20 files in order: 6 paid ($26,325.00), 8 held, 3 rejected, 3 second copies skipped. Run again: nothing more is paid."""
    def outcomes():
        return {p.name: run(graph, p)["result"]["status"] for p in sorted(INVOICES.iterdir())}

    first = outcomes()
    by_status = {s: sorted(n[8:] for n, v in first.items() if v == s) for s in set(first.values())}
    assert by_status == {
        "PAID": ["1001.txt", "1004.json", "1006.csv", "1010.txt", "1011.pdf", "1015.csv"],
        "HELD": ["1002.txt", "1004_revised.json", "1005.json", "1008.txt", "1012.pdf", "1014.xml", "1016.json"],
        "REJECTED": ["1003.txt", "1007.csv", "1009.json", "1013.json"],
        "SKIPPED_DUPLICATE": ["1011.txt", "1012.txt", "1013.pdf"],
    }
    assert (len(ledger.payments()), ledger.total_paid()) == (6, Decimal("26325"))

    second = outcomes()
    assert set(second.values()) == {"ALREADY_PROCESSED", "SKIPPED_DUPLICATE"}
    assert (len(ledger.payments()), ledger.total_paid()) == (6, Decimal("26325"))


def test_a_second_copy_is_skipped_without_a_decision(graph):
    """1011 arrives as PDF and TXT. The second is the same invoice by content: it ends at skip, with no decision and no payment, pointing at the first."""
    assert run(graph, INVOICES / "invoice_1011.pdf")["result"]["status"] == "PAID"
    state = run(graph, INVOICES / "invoice_1011.txt")
    assert nodes(state) == ["ingest", "validate", "register", "skip"] and "decision" not in state
    assert state["result"] == {"status": "SKIPPED_DUPLICATE", "earlier_file": "invoice_1011.pdf", "earlier_status": "PAID"}


def test_a_revision_of_a_paid_invoice_is_held_for_the_difference(graph, ledger):
    """1004 is paid ($1,890.00), then its revision R1 arrives ($5,940.00). The ledger flags it, and the policy holds it: a person settles the $4,050.00."""
    assert run(graph, INVOICES / "invoice_1004.json")["result"]["status"] == "PAID"
    state = run(graph, INVOICES / "invoice_1004_revised.json")
    [f] = [f for f in state["invoice"].flags if f["code"] == "REVISION_OF_PAID"]
    assert (f["severity"], f["earlier_file"], f["already_paid"], f["difference"]) == (
        "REVIEW", "invoice_1004.json", Decimal("1890"), Decimal("4050.00"))
    assert (state["decision"].outcome, state["decision"].because) == ("HOLD", ("REVISION_OF_PAID",))
    assert ledger.total_paid() == Decimal("1890")


def test_a_revision_of_an_unpaid_invoice_replaces_it(graph, ledger, tmp_path):
    """If the original was held, not paid, the revision supersedes it and is judged on its own."""
    original = json.loads((INVOICES / "invoice_1004.json").read_text())
    original["currency"] = "EUR"  # held for currency, so never paid
    (tmp_path / "invoice_1004.json").write_text(json.dumps(original))
    assert run(graph, tmp_path / "invoice_1004.json")["result"]["status"] == "HELD"
    state = run(graph, INVOICES / "invoice_1004_revised.json")
    assert state["result"]["status"] == "PAID" and state["result"]["amount"] == Decimal("5940.00")
    assert [f["severity"] for f in state["invoice"].flags if f["code"] == "SUPERSEDES_UNPAID"] == ["INFO"]
    assert [(r["revision"], r["status"]) for r in ledger.invoices()] == [(0, "SUPERSEDED"), (1, "PAID")]
    # And the original, arriving again after its revision, is recognised and left as superseded.
    again = run(graph, INVOICES / "invoice_1004.json")["result"]
    assert (again["status"], again["earlier_status"]) == ("ALREADY_PROCESSED", "SUPERSEDED")
    assert ledger.total_paid() == Decimal("5940")


def test_same_number_with_different_content_is_held(graph, ledger, tmp_path):
    """A second file claiming the same vendor, number and revision but a different total is not a duplicate to skip: it is held, and the first invoice's record is untouched."""
    assert run(graph, INVOICES / "invoice_1015.csv")["result"]["status"] == "PAID"
    other = tmp_path / "copy.csv"
    other.write_text((INVOICES / "invoice_1015.csv").read_text().replace("WidgetA,10,250.00,2500.00", "WidgetA,12,250.00,3000.00")
                     .replace("6500.00", "7000.00"))
    state = run(graph, other)
    assert state["decision"].because == ("DUPLICATE_CONFLICT",) and state["result"]["status"] == "HELD"
    assert [(r["source_file"], r["status"], r["total"]) for r in ledger.invoices()] == [("invoice_1015.csv", "PAID", "6500")]


def test_an_unreadable_file_goes_to_failed_and_never_reaches_approval(graph, tmp_path):
    """A file that can't be loaded takes the other branch after ingest: no invoice, no decision, an error result."""
    bad = tmp_path / "broken.json"
    bad.write_text("{not json")
    state = run(graph, bad)
    assert nodes(state) == ["ingest", "failed"]
    assert "invoice" not in state and "decision" not in state
    assert state["result"]["status"] == "ERROR" and state["error"].startswith("JSONDecodeError")
    assert run(graph, tmp_path / "notes.docx")["error"].startswith("ValueError: No loader")


def test_steps_are_reported_as_they_happen(graph):
    """run() streams: on_step gets each node's trail entry in order, and they are the same entries the final state holds."""
    seen = []
    state = run(graph, INVOICES / "invoice_1002.txt", seen.append)
    assert seen == state["trail"] and nodes(state) == ["ingest", "validate", "register", "approve", "hold"]
    assert seen[0] == {"node": "ingest", "method": "llm", "model_attempts": 2}  # 1002's recorded retry
    assert seen[2] == {"node": "register", "seen": "NEW"}


def test_main_trace_prints_one_json_line_per_step(capsys, tmp_path):
    """--trace writes the steps to stderr as JSON lines, leaving stdout as the JSON result."""
    code = main.main([f"--invoice_path={INVOICES / 'invoice_1007.csv'}", "--offline", "--trace", "--ledger", str(tmp_path / "l.db")])
    out = capsys.readouterr()
    [entry] = json.loads(out.out)
    lines = [json.loads(line) for line in out.err.splitlines() if line.startswith("{")]
    assert code == 0 and [ln["node"] for ln in lines] == ["ingest", "validate", "register", "approve", "reject"]
    assert all(ln["file"] == "invoice_1007.csv" for ln in lines)
    assert (entry["decision"]["outcome"], entry["result"]["status"]) == ("REJECT", "REJECTED")


def test_main_keeps_its_ledger_between_runs_and_fresh_resets_it(capsys, tmp_path):
    """Run a file twice through main.py: paid once, then recognised. --fresh forgets, and it is paid again."""
    args = [f"--invoice_path={INVOICES / 'invoice_1006.csv'}", "--offline", "--ledger", str(tmp_path / "l.db")]
    statuses = []
    for extra in ([], [], ["--fresh"]):
        assert main.main(args + extra) == 0
        out = capsys.readouterr()
        statuses.append(json.loads(out.out)[0]["result"]["status"])
    assert statuses == ["PAID", "ALREADY_PROCESSED", "PAID"]
    assert "Paid 2750.00 to Acme Industrial Supplies" in out.err  # the brief's mock_payment line, on stderr


def test_the_readme_diagram_matches_the_graph(graph):
    """Every edge of the compiled graph is drawn in the README's diagram, fixed edges solid and branches dotted."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    names = {"__start__": "START", "__end__": "END"}
    for e in graph.get_graph().edges:
        arrow = "-.->" if e.conditional else "-->"
        assert f"{names.get(e.source, e.source)} {arrow} {names.get(e.target, e.target)}" in readme, (e.source, e.target)
