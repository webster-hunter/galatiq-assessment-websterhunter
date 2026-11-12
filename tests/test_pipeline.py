"""The pipeline graph: its shape, the path each invoice takes, and that it decides exactly as the policy does."""
import json
from decimal import Decimal
from pathlib import Path

import pytest

import main
from approval import decide
from extraction import Replay
from ledger import Ledger
from pipeline import build_graph, cannot_approve, checkpointer, release, run

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


def test_the_graph_has_the_stages_and_four_branch_points(graph):
    """One fixed path in (ingest, validate, register, approve) with a branch after ingest (unreadable file), after register (already handled), after approve (the three outcomes) and after await_review (the reviewer's decision)."""
    drawn = graph.get_graph()
    assert set(drawn.nodes) == {"__start__", "ingest", "validate", "register", "skip", "approve", "pay", "hold",
                                "await_review", "reject", "failed", "__end__"}
    fixed = {(e.source, e.target) for e in drawn.edges if not e.conditional}
    branches = {(e.source, e.target) for e in drawn.edges if e.conditional}
    assert fixed == {("__start__", "ingest"), ("validate", "register"), ("hold", "await_review"), ("pay", "__end__"),
                     ("reject", "__end__"), ("skip", "__end__"), ("failed", "__end__")}
    assert branches == {("ingest", "validate"), ("ingest", "failed"), ("register", "approve"), ("register", "skip"),
                        ("approve", "pay"), ("approve", "hold"), ("approve", "reject"),
                        ("await_review", "pay"), ("await_review", "reject")}


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
    code = main.main([f"--invoice_path={INVOICES / 'invoice_1007.csv'}", "--offline", "--trace", "--ledger", str(tmp_path / "l.db"), "--checkpoints", str(tmp_path / "cp.db")])
    out = capsys.readouterr()
    [entry] = json.loads(out.out)
    lines = [json.loads(line) for line in out.err.splitlines() if line.startswith("{")]
    assert code == 0 and [ln["node"] for ln in lines] == ["ingest", "validate", "register", "approve", "reject"]
    assert all(ln["file"] == "invoice_1007.csv" for ln in lines)
    assert (entry["decision"]["outcome"], entry["result"]["status"]) == ("REJECT", "REJECTED")


def test_main_keeps_its_ledger_between_runs_and_fresh_resets_it(capsys, tmp_path):
    """Run a file twice through main.py: paid once, then recognised. --fresh forgets, and it is paid again."""
    args = [f"--invoice_path={INVOICES / 'invoice_1006.csv'}", "--offline", "--ledger", str(tmp_path / "l.db"), "--checkpoints", str(tmp_path / "cp.db")]
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


# --- Fixes from the code review ---------------------------------------------------------------------

def revision_of_1004(tmp_path, label):
    r = json.loads((INVOICES / "invoice_1004_revised.json").read_text())
    r["revision"] = label
    path = tmp_path / f"invoice_1004_{label}.json"
    path.write_text(json.dumps(r))
    return path


def test_a_second_revision_still_knows_the_original_was_paid(graph, ledger, tmp_path):
    """1004 paid ($1,890), R1 held, then R2 arrives. R2 is held for the difference from what was paid, not paid in full; R1 leaves the review queue."""
    assert run(graph, INVOICES / "invoice_1004.json")["result"]["status"] == "PAID"
    assert run(graph, INVOICES / "invoice_1004_revised.json")["result"]["status"] == "HELD"
    state = run(graph, revision_of_1004(tmp_path, "R2"))
    [f] = [f for f in state["invoice"].flags if f["code"] == "REVISION_OF_PAID"]
    assert (f["earlier_file"], f["already_paid"], f["difference"]) == ("invoice_1004.json", Decimal("1890"), Decimal("4050.00"))
    assert state["result"]["status"] == "HELD" and ledger.total_paid() == Decimal("1890")
    assert [(r["revision"], r["status"]) for r in ledger.invoices()] == [(0, "PAID"), (1, "SUPERSEDED"), (2, "HELD")]


def test_an_invoice_left_undecided_by_a_failed_run_is_finished_next_time(tmp_path):
    """A run that dies after recording the invoice leaves it RECEIVED. The next run does not skip it as already processed: it decides it and pays it."""
    ledger = Ledger(":memory:", pay=lambda vendor, amount: None)
    graph = build_graph(Replay(REPLAYS), INVENTORY, ledger)
    invoice = run(graph, INVOICES / "invoice_1006.csv")["invoice"]
    ledger._db.execute("UPDATE invoices SET status='RECEIVED'")   # as a crash between register and pay would leave it
    ledger._db.execute("DELETE FROM payments")
    state = run(graph, INVOICES / "invoice_1006.csv")
    assert state["trail"][2] == {"node": "register", "seen": "UNFINISHED", "earlier_file": "invoice_1006.csv",
                                 "earlier_status": "RECEIVED"}
    assert state["result"]["status"] == "PAID" and ledger.total_paid() == invoice.total


def test_a_failed_payment_is_reported_and_the_batch_continues():
    """The bank times out on one invoice: that invoice is PAYMENT_UNCONFIRMED, the next one is still processed, and a rerun neither pays the first again nor hides it."""
    def bank(vendor, amount):
        if vendor == "Widgets Inc.":
            raise TimeoutError("no answer")

    ledger = Ledger(":memory:", pay=bank)
    graph = build_graph(Replay(REPLAYS), INVENTORY, ledger)
    first = run(graph, INVOICES / "invoice_1001.txt")["result"]
    assert (first["status"], first["error"]) == ("PAYMENT_UNCONFIRMED", "TimeoutError: no answer")
    assert run(graph, INVOICES / "invoice_1006.csv")["result"]["status"] == "PAID"
    again = run(graph, INVOICES / "invoice_1001.txt")["result"]
    assert (again["status"], again["earlier_status"]) == ("ALREADY_PROCESSED", "PAYMENT_UNCONFIRMED")
    assert [p["status"] for p in ledger.payments()] == ["PENDING", "PAID"]


def test_a_revision_of_an_unconfirmed_payment_is_not_paid_in_full(tmp_path):
    """Money may have moved for the original, so its revision is held like a revision of a paid invoice."""
    def bank(vendor, amount):
        raise TimeoutError("no answer")

    ledger = Ledger(":memory:", pay=bank)
    graph = build_graph(Replay(REPLAYS), INVENTORY, ledger)
    assert run(graph, INVOICES / "invoice_1004.json")["result"]["status"] == "PAYMENT_UNCONFIRMED"
    state = run(graph, INVOICES / "invoice_1004_revised.json")
    assert state["decision"].because == ("REVISION_OF_PAID",) and state["result"]["status"] == "HELD"


def test_the_same_bill_under_another_vendor_name_is_held(graph, ledger, tmp_path):
    """The same number, lines and total with the vendor written differently is not a new invoice to pay: it is held as a possible duplicate, pointing at the first."""
    assert run(graph, INVOICES / "invoice_1015.csv")["result"]["status"] == "PAID"
    other = tmp_path / "other_vendor.csv"
    other.write_text((INVOICES / "invoice_1015.csv").read_text().replace("Reliable Components Inc.", "Reliable Components (formerly RC Ltd.)"))
    state = run(graph, other)
    [f] = [f for f in state["invoice"].flags if f["code"] == "POSSIBLE_DUPLICATE"]
    assert (f["severity"], f["earlier_file"], f["earlier_vendor"]) == ("REVIEW", "invoice_1015.csv", "Reliable Components Inc.")
    assert state["result"]["status"] == "HELD" and ledger.total_paid() == Decimal("6500")


def test_an_original_is_processed_when_its_only_newer_revision_was_rejected(graph, ledger, tmp_path):
    """R1 arrives first and is rejected (its total doesn't add up). The original, arriving after, is not skipped as superseded: it is the only good version, and is paid."""
    bad = json.loads((INVOICES / "invoice_1004_revised.json").read_text())
    bad["total"] = 9999.00
    (tmp_path / "a_revised.json").write_text(json.dumps(bad))
    assert run(graph, tmp_path / "a_revised.json")["result"]["status"] == "REJECTED"
    state = run(graph, INVOICES / "invoice_1004.json")
    assert state["result"]["status"] == "PAID" and state["trail"][2]["seen"] == "NEW"


def test_nodes_do_not_change_the_state_they_are_given(ledger):
    """validate and register work on a copy of the invoice. Run on the same input state twice, each returns the same flags: nothing is added twice."""
    graph = build_graph(Replay(REPLAYS), INVENTORY, ledger)
    nodes_ = graph.get_graph().nodes
    ingested = nodes_["ingest"].data.invoke({"path": str(INVOICES / "invoice_1013.json"), "trail": []})
    before = list(ingested["invoice"].flags)
    first = nodes_["validate"].data.invoke(ingested)
    second = nodes_["validate"].data.invoke(ingested)
    assert ingested["invoice"].flags == before                       # the input was not touched
    assert first["invoice"].flags == second["invoice"].flags and len(first["invoice"].flags) == 8
    # register on a revision of a paid invoice: one REVISION_OF_PAID each time, never two.
    run(graph, INVOICES / "invoice_1004.json")
    checked = nodes_["validate"].data.invoke(nodes_["ingest"].data.invoke({"path": str(INVOICES / "invoice_1004_revised.json"), "trail": []}))
    for _ in range(2):
        out = nodes_["register"].data.invoke(checked)
        assert [f["code"] for f in out["invoice"].flags].count("REVISION_OF_PAID") == 1
    assert not any(f["code"] == "REVISION_OF_PAID" for f in checked["invoice"].flags)


def test_fresh_keeps_the_old_ledger_and_says_what_it_means(capsys, tmp_path):
    """--fresh renames the ledger, never deletes it, and warns that the payments it recorded will be made again."""
    path = tmp_path / "l.db"
    args = [f"--invoice_path={INVOICES / 'invoice_1006.csv'}", "--offline", "--ledger", str(path), "--checkpoints", str(tmp_path / "cp.db")]
    assert main.main(args) == 0
    capsys.readouterr()
    assert main.main(args + ["--fresh"]) == 0
    err = capsys.readouterr().err
    [kept] = list(tmp_path.glob("l.db.*.bak"))
    assert "1 payments, $2,750.00" in err and "will be paid again" in err and kept.name in err
    assert Ledger(kept).total_paid() == Decimal("2750") and Ledger(path).total_paid() == Decimal("2750")


# --- The hold queue: a held invoice is a paused run, resumed by a person's decision ------------------

def held(graph, name):
    """Run a sample that the policy holds, and return (queue number, state)."""
    state = run(graph, INVOICES / name)
    assert state["result"]["status"] == "HELD"
    return state["result"]["queue_id"], state


def test_a_held_invoice_pauses_and_waits_in_the_queue(graph, ledger):
    """The run stops after hold: the state has the HELD result and a queue number, the trail ends at hold, and the queue lists it with its reasons."""
    queue_id, state = held(graph, "invoice_1005.json")
    assert nodes(state) == ["ingest", "validate", "register", "approve", "hold"]
    [entry] = ledger.queue()
    assert (entry["id"], entry["source_file"], entry["status"], entry["reasons"]) == (
        queue_id, "invoice_1005.json", "WAITING", "OVER_APPROVAL_THRESHOLD, STOCK_EXCEEDED")
    saved = graph.get_state({"configurable": {"thread_id": entry["thread_id"]}})
    assert saved.next == ("await_review",)                       # where the run is paused
    assert saved.interrupts[0].value["queue_id"] == queue_id     # the question it is waiting on
    assert ledger.total_paid() == 0


def test_approving_resumes_the_run_and_pays(graph, ledger):
    """A reviewer's approval continues the same run from await_review into pay. Who approved is on the result and in the queue record."""
    queue_id, _ = held(graph, "invoice_1005.json")
    seen = []
    state = release(graph, ledger, queue_id, "approve", "Dana Lee", "Stock due Friday", on_step=seen.append)
    assert nodes(state) == ["ingest", "validate", "register", "approve", "hold", "await_review", "pay"]
    assert [s["node"] for s in seen] == ["await_review", "pay"]     # only the new steps are reported
    assert (state["result"]["status"], state["result"]["approved_by"], state["result"]["amount"]) == ("PAID", "Dana Lee", Decimal("15225.00"))
    assert state["decision"].outcome == "HOLD"                      # the policy's decision is kept; the review sits beside it
    [entry] = ledger.queue(status=None)
    assert (entry["status"], entry["action"], entry["reviewer"], entry["note"]) == ("RESOLVED", "approve", "Dana Lee", "Stock due Friday")
    assert ledger.queue() == [] and ledger.invoices()[0]["status"] == "PAID"


def test_rejecting_resumes_the_run_and_records_the_reason(graph, ledger):
    """A rejection goes from await_review to reject with the reviewer's note as the reason, and pays nothing."""
    queue_id, _ = held(graph, "invoice_1016.json")
    state = release(graph, ledger, queue_id, "reject", "Dana Lee", "WidgetC is not a product we buy")
    assert nodes(state)[-2:] == ["await_review", "reject"]
    assert state["result"] == {"status": "REJECTED", "rejected_by": "Dana Lee", "reasons": ["WidgetC is not a product we buy"]}
    assert ledger.invoices()[0]["status"] == "REJECTED" and ledger.payments() == []


def test_a_released_revision_pays_only_the_difference(graph, ledger):
    """1004 was paid $1,890.00; approving its held revision ($5,940.00) pays $4,050.00, so the invoice is paid $5,940.00 in all."""
    run(graph, INVOICES / "invoice_1004.json")
    queue_id, _ = held(graph, "invoice_1004_revised.json")
    state = release(graph, ledger, queue_id, "approve", "Dana Lee")
    assert state["result"]["amount"] == Decimal("4050.00") and ledger.total_paid() == Decimal("5940")
    assert [Decimal(p["amount"]) for p in ledger.payments()] == [Decimal("1890"), Decimal("4050")]


def test_a_later_revision_counts_what_was_actually_paid(graph, ledger, tmp_path):
    """After the original ($1,890) and the released R1 difference ($4,050), an R2 for the same total owes nothing more: it is held, and can only be rejected."""
    run(graph, INVOICES / "invoice_1004.json")
    queue_id, _ = held(graph, "invoice_1004_revised.json")
    release(graph, ledger, queue_id, "approve", "Dana Lee")
    state = run(graph, revision_of_1004(tmp_path, "R2"))
    [f] = [f for f in state["invoice"].flags if f["code"] == "REVISION_OF_PAID"]
    assert (f["already_paid"], f["difference"]) == (Decimal("5940"), Decimal("0.00"))
    with pytest.raises(ValueError, match="nothing more is owed"):
        release(graph, ledger, state["result"]["queue_id"], "approve", "Dana Lee")
    assert ledger.total_paid() == Decimal("5940")


@pytest.mark.parametrize("name, why", [
    ("invoice_1014.xml", "billed in EUR"),
])
def test_some_holds_can_only_be_rejected(graph, ledger, name, why):
    """A reviewer can overrule a hold, within limits: an invoice in another currency can't be paid in dollars whoever approves it. It stays in the queue until rejected."""
    queue_id, state = held(graph, name)
    assert why in cannot_approve(state["invoice"])
    with pytest.raises(ValueError, match="can't be approved"):
        release(graph, ledger, queue_id, "approve", "Dana Lee")
    assert [e["id"] for e in ledger.queue()] == [queue_id] and ledger.payments() == []
    assert release(graph, ledger, queue_id, "reject", "Dana Lee", "We pay in USD only")["result"]["status"] == "REJECTED"


def test_a_decision_needs_a_name_a_reason_to_reject_and_a_waiting_invoice(graph, ledger):
    """Refused without changing anything: no reviewer, a rejection with no note, an unknown action, an unknown queue number, and deciding the same invoice twice."""
    queue_id, _ = held(graph, "invoice_1008.txt")
    for args, message in ((("approve", " "), "reviewer name"), (("reject", "Dana"), "needs a note"), (("pay", "Dana"), "approve or reject")):
        with pytest.raises(ValueError, match=message):
            release(graph, ledger, queue_id, *args)
    with pytest.raises(ValueError, match="no invoice is waiting under queue number 99"):
        release(graph, ledger, 99, "approve", "Dana")
    assert [e["status"] for e in ledger.queue(status=None)] == ["WAITING"]
    release(graph, ledger, queue_id, "approve", "Dana")
    with pytest.raises(ValueError, match="it is RESOLVED"):
        release(graph, ledger, queue_id, "approve", "Dana")
    assert len(ledger.payments()) == 1


def test_a_held_revision_replaced_by_a_newer_one_leaves_the_queue(graph, ledger, tmp_path):
    """R1 is waiting for review when R2 arrives. R1's queue entry closes as superseded, so the reviewer sees one invoice, not two."""
    run(graph, INVOICES / "invoice_1004.json")
    r1, _ = held(graph, "invoice_1004_revised.json")
    state = run(graph, revision_of_1004(tmp_path, "R2"))
    assert [(e["id"], e["status"]) for e in ledger.queue(status=None)] == [(r1, "SUPERSEDED"), (state["result"]["queue_id"], "WAITING")]
    with pytest.raises(ValueError, match="it is SUPERSEDED"):
        release(graph, ledger, r1, "approve", "Dana")


def test_a_run_paused_in_one_process_is_resumed_from_another(tmp_path):
    """The real arrangement: main.py holds an invoice and exits; later, a new ledger connection and a new graph over the same files release it."""
    files = dict(ledger=tmp_path / "ledger.db", checkpoints=tmp_path / "checkpoints.db")

    def process():
        ledger = Ledger(files["ledger"], pay=lambda vendor, amount: None)
        return ledger, build_graph(Replay(REPLAYS), INVENTORY, ledger, checkpointer(files["checkpoints"]))

    ledger, graph = process()
    queue_id, _ = held(graph, "invoice_1012.pdf")
    ledger.close()

    ledger, graph = process()                      # nothing carried over in memory
    [entry] = ledger.queue()
    assert entry["id"] == queue_id
    state = release(graph, ledger, queue_id, "approve", "Dana Lee", "Vendor confirmed by phone")
    # The invoice came back from the checkpoint file intact: exact amounts, repaired values and all.
    assert state["invoice"].total == Decimal("9975.00") and state["invoice"].line_items[1].amount == Decimal("3500.00")
    assert state["result"]["status"] == "PAID" and ledger.total_paid() == Decimal("9975")


def test_review_command_lists_shows_and_decides(capsys, tmp_path):
    """review.py against what main.py left behind: the list, one invoice in full, a refused approval (exit 1), and a rejection."""
    import review
    where = ["--ledger", str(tmp_path / "l.db"), "--checkpoints", str(tmp_path / "cp.db")]
    main.main([f"--invoice_path={INVOICES / 'invoice_1014.xml'}", "--offline", *where])
    assert "1 held for review" in capsys.readouterr().err

    assert review.main(where) == 0
    listing = capsys.readouterr().out
    assert "invoice_1014.xml" in listing and "CURRENCY_UNSUPPORTED" in listing and "1 waiting" in listing

    assert review.main([*where, "show", "1"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert "EUR" in shown["cannot_approve_because"] and shown["invoice"]["currency"] == "EUR"

    assert review.main([*where, "approve", "1", "--by", "Dana"]) == 1
    assert "can't be approved" in capsys.readouterr().err

    assert review.main([*where, "reject", "1", "--by", "Dana", "--note", "USD only"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["result"]["status"], out["review"]["reviewer"]) == ("REJECTED", "Dana")
    assert review.main(where) == 0 and "Nothing is waiting" in capsys.readouterr().out
