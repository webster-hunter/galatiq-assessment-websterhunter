"""The pipeline graph: its shape, the path each invoice takes, and that it decides exactly as the policy does."""
import json
from pathlib import Path

import pytest

import main
from approval import decide
from extraction import Replay
from pipeline import build_graph, run

ROOT = Path(__file__).resolve().parent.parent
INVOICES, REPLAYS = ROOT / "data" / "invoices", ROOT / "data" / "replays"
INVENTORY = {"WidgetA": 15, "WidgetB": 10, "GadgetX": 5, "FakeItem": 0}


@pytest.fixture(scope="module")
def graph():
    return build_graph(Replay(REPLAYS), INVENTORY)


def test_the_graph_has_the_four_stages_and_two_branch_points(graph):
    """One fixed path in (ingest, validate, approve), a branch after ingest for unreadable files, and a branch after approve for the three outcomes."""
    drawn = graph.get_graph()
    assert {n for n in drawn.nodes} == {"__start__", "ingest", "validate", "approve", "pay", "hold", "reject", "failed", "__end__"}
    fixed = {(e.source, e.target) for e in drawn.edges if not e.conditional}
    branches = {(e.source, e.target) for e in drawn.edges if e.conditional}
    assert fixed == {("__start__", "ingest"), ("validate", "approve"), ("pay", "__end__"), ("hold", "__end__"),
                     ("reject", "__end__"), ("failed", "__end__")}
    assert branches == {("ingest", "validate"), ("ingest", "failed"), ("approve", "pay"), ("approve", "hold"), ("approve", "reject")}


@pytest.mark.parametrize("name, path, status", [
    ("invoice_1001.txt", ["ingest", "validate", "approve", "pay"], "APPROVED"),
    ("invoice_1012.pdf", ["ingest", "validate", "approve", "hold"], "HELD"),
    ("invoice_1003.txt", ["ingest", "validate", "approve", "reject"], "REJECTED"),
    ("invoice_1009.json", ["ingest", "validate", "approve", "reject"], "REJECTED"),
])
def test_each_outcome_takes_its_own_branch(graph, name, path, status):
    """The trail lists the nodes that ran, in order (each node appends one entry), ending at the branch the decision chose."""
    state = run(graph, INVOICES / name)
    assert [step["node"] for step in state["trail"]] == path
    assert state["result"]["status"] == status and "error" not in state


def test_the_graph_decides_every_sample_as_the_policy_does(graph):
    """Wiring adds nothing and loses nothing: for all 20 files the graph's decision is decide() on that invoice's flags, and the branch matches it."""
    branch = {"APPROVE": "pay", "HOLD": "hold", "REJECT": "reject"}
    outcomes = []
    for p in sorted(INVOICES.iterdir()):
        state = run(graph, p)
        assert state["decision"] == decide(state["invoice"].flags), p.name
        assert state["trail"][-1]["node"] == branch[state["decision"].outcome], p.name
        outcomes.append(state["decision"].outcome)
    assert (outcomes.count("APPROVE"), outcomes.count("HOLD"), outcomes.count("REJECT")) == (8, 7, 5)


def test_an_unreadable_file_goes_to_failed_and_never_reaches_approval(graph, tmp_path):
    """A file that can't be loaded takes the other branch after ingest: no invoice, no decision, an error result."""
    bad = tmp_path / "broken.json"
    bad.write_text("{not json")
    state = run(graph, bad)
    assert [step["node"] for step in state["trail"]] == ["ingest", "failed"]
    assert "invoice" not in state and "decision" not in state
    assert state["result"]["status"] == "ERROR" and state["error"].startswith("JSONDecodeError")
    assert run(graph, tmp_path / "notes.docx")["error"].startswith("ValueError: No loader")


def test_steps_are_reported_as_they_happen(graph):
    """run() streams: on_step gets each node's trail entry in order, and they are the same entries the final state holds."""
    seen = []
    state = run(graph, INVOICES / "invoice_1002.txt", seen.append)
    assert seen == state["trail"] and [s["node"] for s in seen] == ["ingest", "validate", "approve", "hold"]
    assert seen[0] == {"node": "ingest", "method": "llm", "model_attempts": 2}  # 1002's recorded retry


def test_main_trace_prints_one_json_line_per_step(capsys):
    """--trace writes the steps to stderr as JSON lines, leaving stdout as the JSON result."""
    code = main.main([f"--invoice_path={INVOICES / 'invoice_1007.csv'}", "--offline", "--trace"])
    out = capsys.readouterr()
    [entry] = json.loads(out.out)
    lines = [json.loads(line) for line in out.err.splitlines()]
    assert code == 0 and [ln["node"] for ln in lines] == ["ingest", "validate", "approve", "reject"]
    assert all(ln["file"] == "invoice_1007.csv" for ln in lines)
    assert (entry["decision"]["outcome"], entry["result"]["status"]) == ("REJECT", "REJECTED")
    assert [s["node"] for s in entry["trail"]] == ["ingest", "validate", "approve", "reject"]


def test_the_readme_diagram_matches_the_graph(graph):
    """Every edge of the compiled graph is drawn in the README's diagram, fixed edges solid and branches dotted."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    names = {"__start__": "START", "__end__": "END"}
    for e in graph.get_graph().edges:
        arrow = "-.->" if e.conditional else "-->"
        assert f"{names.get(e.source, e.source)} {arrow} {names.get(e.target, e.target)}" in readme, (e.source, e.target)
