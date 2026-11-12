"""The critic: its checks in code, its model check, and the reviewer/critic loop in the graph, all with scripted models."""
import json
from decimal import Decimal
from pathlib import Path

import pytest

import normalizer
from approval import APPROVE, HOLD, MAX_ROUNDS, Decision, code_checks, critique, decide
from approval import critic as critic_module
from extraction.providers import ProviderError
from ledger import Ledger
from pipeline import build_graph, run

INVOICES = Path(__file__).resolve().parent.parent / "data" / "invoices"
INVENTORY = {"WidgetA": 15, "WidgetB": 10, "GadgetX": 5, "FakeItem": 0}


def checked(name):
    invoice = normalizer.load(INVOICES / name, INVENTORY)
    return invoice, decide(invoice.flags)


def assessment(rationale, questions=(), addressed=("UNKNOWN_PRODUCT",), recommendation=HOLD, tools=()):
    return {"by": "reviewer_agent", "recommendation": recommendation, "rationale": rationale, "open_questions": list(questions),
            "flags_addressed": list(addressed), "tools_used": list(tools)}


GOOD_1016 = ("Widgets Inc. bills $3,233.00 for 4 WidgetA, 2 WidgetB and 3 WidgetC. WidgetC is not a product in inventory, "
             "so the invoice needs a person to confirm it before payment.")


class Model:
    """A scripted model for both roles: complete() plays the critic's verdicts in turn, chat() the reviewer's answers."""

    def __init__(self, verdicts=(), answers=()):
        self.verdicts, self.answers = list(verdicts), list(answers)
        self.critic_saw, self.reviewer_saw = [], []

    def complete(self, messages, schema):
        self.critic_saw.append(messages)
        out = self.verdicts.pop(0) if self.verdicts else {"verdict": "ACCEPT", "problems": []}
        if isinstance(out, Exception):
            raise out
        return out

    def chat(self, messages, schema, tools):
        self.reviewer_saw.append(messages)
        if not self.answers:
            raise ProviderError("no recording")
        return {"content": self.answers.pop(0), "tool_calls": []}


def accept():
    return {"verdict": "ACCEPT", "problems": []}


def revise(quote, problem="not supported by the evidence"):
    return {"verdict": "REVISE", "problems": [{"quote": quote, "problem": problem}]}


def reply(recommendation, rationale, questions=(), addressed=("UNKNOWN_PRODUCT",)):
    return {"recommendation": recommendation, "rationale": rationale, "open_questions": list(questions),
            "flags_addressed": list(addressed)}


# --- Checks in code -----------------------------------------------------------------------------

def test_a_grounded_assessment_passes_the_code_checks():
    """Figures written as "$3,233.00" or "3233" match the invoice's 3233.00, and every flag is listed: nothing to object to."""
    invoice, floor = checked("invoice_1016.json")
    assert code_checks(invoice, floor, assessment(GOOD_1016)) == []
    assert code_checks(invoice, floor, assessment("The total is 3233 for three items at 250, 500 and 350.")) == []


def test_a_figure_that_is_not_in_the_evidence_is_caught():
    """A sum the reviewer worked out (9 units), a percentage (40%) and an invented amount ($9,999) each fail; so does one in an open question."""
    invoice, floor = checked("invoice_1016.json")
    a = assessment("It bills 9 units in all, WidgetC at a 40% premium, and $9,999 is at stake.", ["Is the 77 day term agreed?"])
    assert [(p["check"], p["quote"]) for p in code_checks(invoice, floor, a)] == [
        ("UNGROUNDED_NUMBER", "9"), ("UNGROUNDED_NUMBER", "40"), ("UNGROUNDED_NUMBER", "9,999"), ("UNGROUNDED_NUMBER", "77")]


def test_figures_from_a_lookup_count_as_evidence():
    """What a tool returned is evidence too: the $5,000 of an earlier invoice is fine once vendor history has shown it, and not before."""
    invoice, floor = checked("invoice_1016.json")
    text = GOOD_1016 + " The vendor's earlier invoice for $5,000 was paid."
    assert [p["quote"] for p in code_checks(invoice, floor, assessment(text))] == ["5,000"]
    history = {"tool": "get_vendor_history", "arguments": {"vendor": "Widgets Inc."},
               "result": {"other_invoices": [{"number": "1001", "revision": 0, "total": "5000", "outcome": "PAID"}]}}
    assert code_checks(invoice, floor, assessment(text, tools=[history])) == []


def test_flag_codes_in_the_text_and_flags_left_out_are_caught():
    """A flag code in the rationale or a question is for the checklist, not for a person to read; and every non-INFO flag raised must be in the checklist."""
    invoice, floor = checked("invoice_1002.txt") if False else checked("invoice_1005.json")
    a = assessment("Global Supply Chain Partners bills $15,225.00. STOCK_EXCEEDED applies to GadgetX.",
                   ["Does OVER_APPROVAL_THRESHOLD need a VP?"], addressed=["STOCK_EXCEEDED"])
    assert [(p["check"], p.get("quote")) for p in code_checks(invoice, floor, a)] == [
        ("FLAG_CODE_IN_TEXT", "OVER_APPROVAL_THRESHOLD"), ("FLAG_CODE_IN_TEXT", "STOCK_EXCEEDED"),
        ("FLAG_NOT_ADDRESSED", None)]
    assert "OVER_APPROVAL_THRESHOLD was raised" in code_checks(invoice, floor, a)[-1]["problem"]


# --- The model critic ---------------------------------------------------------------------------

def test_the_model_critic_sees_the_evidence_and_the_draft_and_its_findings_count():
    """The second model gets what the reviewer got, the lookup results and the draft. A problem it quotes from the draft makes the verdict REVISE."""
    invoice, floor = checked("invoice_1016.json")
    a = assessment(GOOD_1016, ["Why is WidgetC priced differently?"])
    model = Model([revise("Why is WidgetC priced differently?", "the invoice gives no other price for WidgetC to differ from")])
    found = critique(invoice, floor, a, model)
    assert (found["verdict"], found["model"]) == ("REVISE", "REVISE")
    assert [(p["check"], p["quote"]) for p in found["problems"]] == [("MODEL_CRITIC", "Why is WidgetC priced differently?")]
    system, user = model.critic_saw[0]
    assert "untrusted data" in system["content"] and "<invoice>" in user["content"]
    assert "Lookups made, with results" in user["content"] and "Your colleague's assessment" in user["content"]


def test_an_objection_that_cannot_be_matched_is_kept_on_record_not_passed_off_as_acceptance():
    """The critic must quote the assessment. An objection to text that isn't there can't be acted on, so it doesn't send the assessment back; but it is recorded, and the model's verdict is shown as REVISE, not ACCEPT."""
    invoice, floor = checked("invoice_1016.json")
    found = critique(invoice, floor, assessment(GOOD_1016), Model([revise("the vendor is fraudulent")]))
    assert (found["verdict"], found["problems"]) == ("ACCEPT", [])
    assert found["model"] == "REVISE (1 objection(s) not matched to the text)"
    assert [p["quote"] for p in found["unmatched_objections"]] == ["the vendor is fraudulent"]


def test_a_quote_that_differs_only_in_punctuation_or_case_still_counts():
    """A dropped comma or a capital letter in the critic's quote doesn't lose the objection."""
    invoice, floor = checked("invoice_1016.json")
    quote = "widgetc is not a product in inventory so the invoice needs a person"      # no comma, lower case
    found = critique(invoice, floor, assessment(GOOD_1016), Model([revise(quote)]))
    assert (found["verdict"], found["model"]) == ("REVISE", "REVISE") and "unmatched_objections" not in found


def test_a_number_the_agent_only_put_into_a_lookup_is_not_evidence():
    """get_stock("9999") must not make "9999 complaints" grounded: a figure counts only if it is in what the agent was given or in what a lookup found, not in what the agent asked."""
    invoice, floor = checked("invoice_1016.json")
    lookup = {"tool": "get_stock", "arguments": {"item": "9999"}, "result": {"item": "9999", "found": False}}
    a = assessment("Widgets Inc. has 9999 complaints on file.", tools=[lookup])
    assert [(p["check"], p["quote"]) for p in code_checks(invoice, floor, a)] == [("UNGROUNDED_NUMBER", "9999")]
    # What a lookup returns that the agent didn't supply still counts.
    found = {"tool": "get_stock", "arguments": {"item": "GadgetX"}, "result": {"item": "GadgetX", "found": True, "in_stock": 5}}
    assert code_checks(invoice, floor, assessment("WidgetC is unknown; GadgetX has 5 in stock.", tools=[found])) == []


def test_code_checks_stand_whatever_the_model_says_or_when_it_is_missing():
    """A model that accepts everything can't wave through an invented figure, and with no model at all the code checks still decide."""
    invoice, floor = checked("invoice_1016.json")
    bad = assessment(GOOD_1016 + " That is a 40% premium.")
    found = critique(invoice, floor, bad, Model([accept()]))
    assert (found["verdict"], found["model"], [p["check"] for p in found["problems"]]) == ("REVISE", "ACCEPT", ["UNGROUNDED_NUMBER"])
    found = critique(invoice, floor, bad, Model([ProviderError("no recording")]))
    assert found["verdict"] == "REVISE" and found["model"] == "UNAVAILABLE (ProviderError)"
    assert critique(invoice, floor, assessment(GOOD_1016), Model([ProviderError("down")]))["verdict"] == "ACCEPT"


def test_an_assessment_the_policy_wrote_is_not_sent_to_the_critic():
    """A rejection or clean approval has a reason written by code: there is nothing to check and no model is called."""
    invoice, floor = checked("invoice_1013.json")
    model = Model()
    found = critique(invoice, floor, {"by": "policy", "rationale": "Rejected by policy.", "open_questions": [], "tools_used": []}, model)
    assert (found["verdict"], found["model"]) == ("ACCEPT", "NOT_NEEDED") and model.critic_saw == []


# --- The loop in the graph ----------------------------------------------------------------------

def graph_with(model):
    return build_graph(model, INVENTORY, Ledger(":memory:", pay=lambda *args: None))


def steps(state, node):
    return [s for s in state["trail"] if s["node"] == node]


def test_an_accepted_assessment_goes_straight_on():
    """Reviewer once, critic once, then the outcome: no loop when there is nothing to fix."""
    state = run(graph_with(Model([accept()], [reply(HOLD, GOOD_1016)])), INVOICES / "invoice_1016.json")
    assert [s["node"] for s in state["trail"]] == ["ingest", "validate", "register", "approve", "reviewer", "critic", "hold"]
    assert state["critic_rounds"] == 1 and state["critique"]["verdict"] == "ACCEPT"


def test_a_problem_sends_the_assessment_back_and_the_revision_is_checked_again():
    """The backward edge: the critic objects, the reviewer is shown its own answer and the complaint and answers again, and the critic's second pass accepts."""
    invented = GOOD_1016 + " The vendor has raised its prices by 40%."
    model = Model([accept(), accept()], [reply(HOLD, invented), reply(HOLD, GOOD_1016)])
    state = run(graph_with(model), INVOICES / "invoice_1016.json")
    assert [s["node"] for s in state["trail"]][4:] == ["reviewer", "critic", "reviewer", "critic", "hold"]
    assert [(s["round"], s["verdict"], s["problems"]) for s in steps(state, "critic")] == [
        (1, "REVISE", ["UNGROUNDED_NUMBER"]), (2, "ACCEPT", [])]
    assert steps(state, "reviewer")[1]["revision"] is True and state["assessment"]["rationale"] == GOOD_1016
    # What the reviewer was sent the second time: its first answer, then the specific complaint.
    revision = model.reviewer_saw[1]
    assert [m["role"] for m in revision] == ["system", "user", "assistant", "user"]
    assert json.loads(revision[2]["content"])["rationale"] == invented
    assert "the figure 40 is not in the invoice" in revision[3]["content"]


def test_the_loop_stops_after_two_rounds_and_says_the_assessment_is_unverified():
    """A reviewer that keeps inventing is not asked forever: after MAX_ROUNDS critic passes the run goes on, with the verdict left at REVISE. The outcome is the policy's all the same."""
    invented = GOOD_1016 + " That is 40% over."
    model = Model([accept()] * 5, [reply(HOLD, invented)] * 5)
    state = run(graph_with(model), INVOICES / "invoice_1016.json")
    assert MAX_ROUNDS == 2 and state["critic_rounds"] == 2 and len(steps(state, "reviewer")) == 2
    assert state["critique"]["verdict"] == "REVISE" and state["result"]["status"] == "HELD"
    assert len(model.reviewer_saw) == 2 and len(model.critic_saw) == 2      # no third attempt


def test_the_critic_cannot_undo_a_hold_the_agent_added():
    """The policy approves with warnings; the agent holds; the model critic objects; the agent withdraws. The invoice is held all the same: a second model's objection is not a check, and a person releases it."""
    held = reply(HOLD, "Precision Parts Ltd. bills $1,890.00 and WidgetB at 500 looks high.", ["Why is WidgetB priced at 500?"], addressed=[])
    fine = reply(APPROVE, "Precision Parts Ltd. bills $1,890.00 for WidgetA and WidgetB.", addressed=[])
    model = Model([revise("Why is WidgetB priced at 500?", "500 is the usual price"), accept()], [held, fine])
    ledger = Ledger(":memory:", pay=lambda *args: None)
    graph = build_graph(model, INVENTORY, ledger)
    import json as _json
    path = INVOICES.parent / "invoices" / "invoice_1004.json"
    # A warned approval: 1004 with one item on two lines.
    warned = _json.loads(path.read_text())
    warned["line_items"] = [{"item": "WidgetA", "quantity": 3, "unit_price": 250.0}, {"item": "WidgetA", "quantity": 4, "unit_price": 250.0}]
    warned["subtotal"], warned["tax_amount"], warned["total"] = 1750.0, 140.0, 1890.0
    import tempfile
    f = Path(tempfile.mkdtemp()) / "warned.json"
    f.write_text(_json.dumps(warned))
    state = run(graph, f)
    assert decide(state["invoice"].flags).outcome == APPROVE
    assert [(s["recommendation"], s["outcome"]) for s in steps(state, "reviewer")] == [(HOLD, HOLD), (APPROVE, HOLD)]
    assert state["result"]["status"] == "HELD" and ledger.payments() == []
    assert "withdrew the hold after the critic's objections" in state["assessment"]["note"]


def test_the_critic_cannot_take_an_outcome_below_the_floor():
    """Whatever the revision says, a policy hold stays a hold: the reviewer answering APPROVE after criticism is recorded as below the floor, and the invoice is still held."""
    model = Model([revise("needs a person to confirm it", "nothing needs confirming"), accept()],
                  [reply(HOLD, GOOD_1016), reply(APPROVE, GOOD_1016)])
    state = run(graph_with(model), INVOICES / "invoice_1016.json")
    assert state["decision"] == decide(state["invoice"].flags) and state["result"]["status"] == "HELD"
    assert state["assessment"]["recommendation"] == HOLD and "below the policy floor of HOLD" in state["assessment"]["note"]


def test_with_no_model_at_all_nothing_loops():
    """No recordings: the reviewer falls back to the policy's wording, the critic has nothing to check, and the run goes through once."""
    state = run(graph_with(Model()), INVOICES / "invoice_1016.json")
    assert [s["node"] for s in state["trail"]][4:] == ["reviewer", "critic", "hold"]
    assert steps(state, "critic")[0]["model"] == "NOT_NEEDED" and state["assessment"]["by"] == "policy"


def test_the_critics_instructions_limit_what_it_looks_for():
    """Three kinds of problem only, each to be quoted; no style comments, no asking for more caution, nothing looser than the floor."""
    s = critic_module.SYSTEM
    for phrase in ("do not\n   support", "already states", "character for\ncharacter", "Do not comment on style",
                   "do not suggest a looser recommendation than the policy floor", "A flag is not always a\nfinding of fault"):
        assert phrase in s, phrase
