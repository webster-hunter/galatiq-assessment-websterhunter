"""The invoice pipeline as a LangGraph graph.

LangGraph runs a program as a graph. Three things define one:

    State   one dictionary that travels through the graph. Every node reads it and returns the
            keys it wants to change.
    Nodes   plain functions: state in, a partial update out. A node never calls the next node.
    Edges   what runs after what. A fixed edge always goes to the same node; a conditional edge
            calls a small function on the state and goes where it says.

This graph, with the decision the policy made choosing the branch after approve:

    START -> ingest -> validate -> register -> approve -> reviewer -> critic -+-> pay ------------------> END
                 |                     |                     ^           |    +-> reject ---------------> END
                 +-> failed -> END     +-> skip -> END       +--revise---+    +-> hold -> await_review -+-> pay
                                                                                                        +-> reject

A loop. `critic -> reviewer` is an edge that points backwards: the critic checks the reviewer's
assessment, and if it finds problems the run goes round again with the complaints attached. In
LangGraph a cycle is nothing special, just a conditional edge whose target is an earlier node.
What a cycle needs is a way to stop. Here that is a counter in the state, `critic_rounds`,
which the routing function reads: after MAX_ROUNDS the run goes forward whatever the verdict.
(LangGraph also has its own backstop, a recursion limit that aborts a run after too many steps.
That is a crash guard, not a design: the counter is what ends this loop on purpose.)

Pausing for a person. A held invoice is a run that stops and waits. Two LangGraph features do it:

    checkpointer  saves the state after every node, under a thread ID. A run can then be picked
                  up later, by another process, exactly where it stopped.
    interrupt()   called inside a node, it stops the run there and hands a question back to the
                  caller. Resuming the same thread with an answer makes interrupt() return that
                  answer, and the node carries on.

So `await_review` is one line of waiting: the graph pauses in it for as long as it takes, and a
reviewer's decision (the review screen, web/) resumes it into `pay` or `reject`.

Nodes with side effects. `register`, `pay`, `hold` and `reject` write to the ledger, and `pay`
moves money. LangGraph may run a node more than once: after a crash, or when a paused run is
resumed. So a node that changes the outside world has to be safe to repeat. Here that safety is
in the ledger, not in the graph: recording an invoice is an upsert, and a payment carries a key
the ledger accepts only once.

Why a graph and not four function calls in a row: the next stages need what a script handles
badly. The reviewer and critic loop is an edge that points backwards. A held invoice is a run
that pauses and is resumed later by a person. Both are built-in graph features, and both slot
into this file as new nodes and edges without changing the stages themselves.
"""
import copy
import dataclasses
import json
import operator
import sys
import sqlite3
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path, PurePath
from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

import normalizer
from approval import APPROVE, HOLD, MAX_ROUNDS, REJECT, Decision, assess, critique, decide
from ledger import Ledger
from ledger import ledger as seen
from ledger.ledger import currency_of
from extraction import FALLBACK_SUFFIXES, Provider, ProviderError, read
from normalizer.checks import usable_rate
from normalizer.models import Invoice


class State(TypedDict, total=False):
    """Everything known about one invoice as it moves through the graph.

    total=False: no key is required, because the state fills up as it goes. A run starts with
    only `path`; ingest adds `invoice`; approve adds `decision`; a final node adds `result`.

    When a node returns {"decision": d}, LangGraph merges that into the state. By default a
    returned key REPLACES the old value. `trail` is declared differently: Annotated[list,
    operator.add] attaches a "reducer", a function LangGraph uses to combine the old value with
    the new one. With operator.add, each node's one-item list is appended to the list so far.
    So every node can report what it did without knowing what came before.
    """
    path: str
    invoice: Invoice
    recorded: bool  # True once this invoice has its own row in the ledger, so later nodes may update it
    decision: Decision
    assessment: dict  # the reason for the decision: {"by", "recommendation", "rationale", "open_questions", "tools_used"}
    critique: dict    # the critic's last verdict on it: {"verdict", "problems", "model"}
    critic_rounds: int  # how many times the critic has run for this invoice; what ends the loop
    review: dict    # a person's decision on a held invoice: {"action", "reviewer", "note", "amount"[, "converted"]}
                    # amount: set only for a revision of a paid invoice (the difference). converted: the dollar
                    # estimate, for an invoice in another currency.
    result: dict
    error: str
    trail: Annotated[list[dict], operator.add]


def _step(node: str, **detail) -> list[dict]:
    """One trail entry, as the one-item list the `trail` reducer appends."""
    return [{"node": node, **detail}]


# The checkpointer stores each state as bytes and rebuilds it on load. These are the classes of
# ours it is allowed to rebuild: naming them is LangGraph's guard against a tampered checkpoint
# file instantiating arbitrary code.
_STATE_TYPES = [("normalizer.models", "Invoice"), ("normalizer.models", "LineItem"), ("approval.policy", "Decision")]


def checkpointer(path: str | Path | None = None):
    """Where run states are saved.

    Args:
        path: A SQLite file, so a run paused in one process can be resumed from another (main.py
            holds an invoice, the review screen releases it later). None keeps states in memory: pausing
            and resuming still work, within this process only.
    """
    serde = JsonPlusSerializer(allowed_msgpack_modules=_STATE_TYPES)
    if path is None:
        return InMemorySaver(serde=serde)
    # check_same_thread=False: LangGraph may use the connection from a worker thread.
    return SqliteSaver(sqlite3.connect(str(path), check_same_thread=False), serde=serde)


def build_graph(provider: Provider, inventory: dict | None = None, ledger: Ledger | None = None, checkpoints=None,
                rates=None, fallback_types=FALLBACK_SUFFIXES):
    """Build and compile the pipeline.

    The provider, inventory and ledger are not in the State. State is the data about one invoice, and it
    is what LangGraph will save at each step once checkpoints are on; a model client and a stock
    table are tools the nodes use, the same for every invoice. So the nodes are defined in here
    and simply use them (a closure).

    Args:
        provider: The model provider for TXT and PDF files (Grok, Replay, a test stub).
        inventory: Item name -> stock on hand. None skips the product and stock checks.
        ledger: Where invoices and payments are recorded. Defaults to one in memory, which lasts
            as long as the graph: duplicates within a batch are caught, nothing is kept after.
        checkpoints: The checkpointer (see checkpointer()). Defaults to one in memory.
        rates: The rate service for invoices in another currency (see the rates package). None:
            such an invoice has no dollar estimate and is held as unsupported.
        fallback_types: Extensions of the file types with no parser that are given to the model
            when they are text (see extraction.read). Any other unknown type is reported unread.

    Returns:
        The compiled graph. Call .invoke({"path": ...}) for the final state, or .stream(...) to
        see each node's update as it happens.
    """

    ledger = ledger if ledger is not None else Ledger(":memory:")

    # --- Nodes. Each takes the state and returns only the keys it changes. --------------------

    def ingest(state: State) -> dict:
        """Stage 1. Read the file into an Invoice: a parser for structured files, the model for text.

        A structured file in a layout the parser doesn't know falls back to the model (see
        extraction.read). The trail says which means was used.
        """
        path = Path(state["path"])
        try:
            invoice, method = read(path, provider, fallback_types=fallback_types)
        except (*normalizer.LOAD_ERRORS, ProviderError) as e:
            # A node reports failure through the state, like any other result. The edge after
            # ingest reads `error` and routes to the `failed` node. Any other exception is a bug
            # in this code and is left to raise.
            error = f"{type(e).__name__}: {e}"
            return {"error": error, "trail": _step("ingest", error=error)}
        return {"invoice": invoice,
                "trail": _step("ingest", method=method, model_attempts=len(invoice.extraction_attempts))}

    def validate(state: State) -> dict:
        """Stage 2. Repair what the invoice proves, run every check against the inventory, grade each flag."""
        # check() adds flags to the invoice it is given. A node must not change the state it was
        # handed: if LangGraph runs it again (after a crash, or resuming a paused run) it would
        # then start from an invoice that already has those flags and add them a second time.
        # So work on a copy and return it.
        invoice = normalizer.check(copy.deepcopy(state["invoice"]), inventory, rates=rates)
        return {"invoice": invoice, "trail": _step("validate", flags=[f["code"] for f in invoice.flags])}

    def register(state: State) -> dict:
        """Ask the ledger whether this invoice has been seen, and record it if it is new.

        This is the only stage that compares an invoice with others. What it finds either ends
        the run here (an exact duplicate needs no decision) or travels on as a flag, so that the
        policy, not this node, decides what a revision or a conflict means.
        """
        invoice = copy.deepcopy(state["invoice"])  # a copy, for the same reason as in validate
        found = ledger.seen(invoice)
        earlier = found.earlier or {}
        step = {"seen": found.kind, **({"earlier_file": earlier["source_file"], "earlier_status": earlier["status"]} if earlier else {})}

        # Already handled: nothing to decide. Setting `result` here is what sends the run to `skip`.
        skips = {seen.SAME_FILE: "ALREADY_PROCESSED", seen.DUPLICATE: "SKIPPED_DUPLICATE", seen.SUPERSEDED: "SKIPPED_SUPERSEDED"}
        if found.kind in skips:
            result = {"status": skips[found.kind], "earlier_file": earlier["source_file"], "earlier_status": earlier["status"]}
            return {"result": result, "trail": _step("register", **step)}

        def flag(code, **evidence):
            f = {"code": code, **evidence}
            invoice.flags.append({"code": code, "severity": normalizer.severity(f), **evidence})

        if found.kind == seen.CONFLICT:
            # Same identity, different content. Not recorded: the earlier invoice keeps its row.
            flag("DUPLICATE_CONFLICT", earlier_file=earlier["source_file"], earlier_total=earlier["total"])
            return {"invoice": invoice, "recorded": False, "trail": _step("register", **step)}
        if found.kind == seen.UNIDENTIFIED:  # no invoice number; INVOICE_NUMBER_MISSING already holds it
            return {"recorded": False, "trail": _step("register", **step)}
        if found.kind == seen.REVISION_OF_PAID:
            # paid_total covers every earlier revision that was paid, not only the latest one.
            flag("REVISION_OF_PAID", earlier_file=earlier["source_file"], already_paid=found.paid_total,
                 difference=invoice.total - found.paid_total if invoice.total is not None else None)
        elif found.kind == seen.REVISION_OF_UNPAID:
            flag("SUPERSEDES_UNPAID", earlier_file=earlier["source_file"], earlier_status=earlier["status"])
        elif found.kind == seen.SAME_BILL_OTHER_VENDOR:
            flag("POSSIBLE_DUPLICATE", earlier_file=earlier["source_file"], earlier_vendor=earlier["vendor"],
                 earlier_status=earlier["status"])
        for replaced in found.replaces:  # earlier revisions still held or unfinished: this one takes their place
            ledger.supersede(replaced, invoice.source_file)
        # NEW, UNFINISHED (an earlier run recorded it and stopped), a revision, or a possible duplicate:
        # all go on to approval. record() is an upsert, so repeating it is harmless.
        ledger.record(invoice, seen.RECEIVED)
        return {"invoice": invoice, "recorded": True, "trail": _step("register", **step)}

    def skip(state: State, config) -> dict:
        """The invoice was already handled, as this file or as another copy. Nothing is decided or paid."""
        finish(state, config, "skip", state["result"])
        return {"trail": _step("skip", **state["result"])}

    def approve(state: State) -> dict:
        """Stage 3. The policy floor: the minimum outcome the flags require. (The reviewer agent will sit here.)"""
        decision = decide(state["invoice"].flags)
        return {"decision": decision,
                "trail": _step("approve", outcome=decision.outcome, rule=decision.rule, because=list(decision.because))}

    def reviewer(state: State) -> dict:
        """Stage 3, second half. The reviewer agent looks into a hold or a warned approval and writes the reason.

        It is the only node after ingest that calls a model. What it can change is fixed in
        approval/reviewer.py: an approval may become a hold, nothing else. For a rejection or a
        clean approval it calls nothing and the reason is written from the flags. If the model
        can't be reached, the policy's decision stands as it is.
        """
        # On the way back round, the state holds the critic's complaints about the last assessment.
        # The decision is always worked out from the policy floor, never from an earlier round's
        # result, so a hold the reviewer withdraws on revision really is withdrawn.
        floor = decide(state["invoice"].flags)
        revising = state.get("critique", {}).get("verdict") == "REVISE"
        decision, assessment = assess(state["invoice"], floor, provider, inventory, ledger,
                                      previous=state["assessment"] if revising else None,
                                      problems=state["critique"]["problems"] if revising else None)
        step = {"by": assessment["by"], "recommendation": assessment["recommendation"], "outcome": decision.outcome,
                "tools": [t["tool"] for t in assessment["tools_used"]], **({"revision": True} if revising else {}),
                **({"note": assessment["note"]} if "note" in assessment else {})}
        return {"decision": decision, "assessment": assessment, "trail": _step("reviewer", **step)}

    def critic(state: State) -> dict:
        """Check the reviewer's assessment: in code, then with a second model call.

        Returns the verdict and counts the round. It decides nothing about where the run goes
        next; the routing function after it reads the verdict and the count.
        """
        rounds = state.get("critic_rounds", 0) + 1
        found = critique(state["invoice"], decide(state["invoice"].flags), state["assessment"], provider)
        step = {"round": rounds, "verdict": found["verdict"], "model": found["model"],
                "problems": [p["check"] for p in found["problems"]],
                **({"unmatched_objections": len(found["unmatched_objections"])} if "unmatched_objections" in found else {})}
        return {"critique": found, "critic_rounds": rounds, "trail": _step("critic", **step)}

    def settle(state: State, status: str, detail: str) -> None:
        """Write the outcome to the invoice's ledger row, if it has one of its own."""
        if state.get("recorded"):
            ledger.record(state["invoice"], status, detail)

    # Outcomes that settle nothing, so the same file reaches them again on every run over its
    # folder. Each is logged the first time only.
    REPEATABLE = ("SKIPPED_DUPLICATE", "SKIPPED_SUPERSEDED", "ERROR", "PAYMENT_NOT_SENT")

    def finish(state: State, config, node: str, result: dict) -> None:
        """Log how this run ended, with the evidence, so it can be looked up after its saved state is gone.

        Called by each node that ends a run, after it has done its work. The log is a record of
        what happened, not part of it: if writing it fails, the failure is reported on stderr and
        the run carries on. A payment that has gone out must never be reported as a failed run
        because its log entry could not be written.

        A file that was simply processed before (ALREADY_PROCESSED) is not logged again: its
        first run is the record. Nor is an outcome that the same file reaches on every run: a
        copy skipped again, a file that still can't be read.
        """
        if result["status"] == "ALREADY_PROCESSED":
            return
        try:
            _log(state, config, node, result)
        except Exception as e:  # anything at all: see above
            print(f"warning: the outcome of {Path(state['path']).name} ({result['status']}) could not be logged "
                  f"({type(e).__name__}: {e}); the outcome itself stands", file=sys.stderr)

    def _log(state: State, config, node: str, result: dict) -> None:
        invoice, review, assessment = state.get("invoice"), state.get("review"), state.get("assessment") or {}
        decision, status = state.get("decision"), result["status"]
        path = str(Path(state["path"]).resolve())
        if status in REPEATABLE and not review and ledger.has_run(path, status):
            return
        if review:
            by = "person"
        elif result["status"].startswith("SKIPPED"):
            by = "ledger"
        elif invoice is None:
            by = "none"
        else:
            by = "policy+agent" if assessment.get("by") == "reviewer_agent" else "policy"
        if status.startswith("SKIPPED"):
            note = f"A copy of {result['earlier_file']}, which was {result['earlier_status'].lower()}"
        else:
            note = result.get("error") or (review or {}).get("note")
        reasons = list(decision.because or decision.warnings) if decision and not status.startswith("SKIPPED") else []
        # The state's trail doesn't yet hold this node's own entry: it is added when the node returns.
        summary = {"invoice": invoice, "decision": decision, "assessment": state.get("assessment"),
                   "critique": state.get("critique"), "review": review, "result": result,
                   "trail": state.get("trail", []) + _step(node, **result)}
        ledger.log_run(config["configurable"]["thread_id"], path, invoice.source_file if invoice else Path(state["path"]).name,
                       invoice.vendor_name if invoice else None, invoice.total if invoice else None,
                       currency_of(invoice) if invoice else None, status, by, review["reviewer"] if review else None,
                       reasons, note, json.dumps(summary, default=_plain))

    def pay(state: State, config) -> dict:
        """Stage 4, approved branch. Pay through the ledger, which pays any one invoice only once.

        Reached two ways: straight from approve, or from await_review when a person released a
        held invoice. In the second case `review` says who, and may give an amount to pay in
        place of the total (the difference, for a revision of an invoice already paid).
        """
        review = state.get("review") or {}
        payment = ledger.pay(state["invoice"], review.get("amount"))
        # PENDING means the payment was sent, now or earlier, and never confirmed. It is not PAID
        # and must not be retried: it is reported for a person to check.
        # NOT_SENT means it couldn't be attempted. Nothing went out, so the invoice goes back to
        # RECEIVED: the next run over its file takes it up again instead of treating it as done.
        status = {"PAID": "PAID", "PENDING": "PAYMENT_UNCONFIRMED", "NOT_SENT": "PAYMENT_NOT_SENT"}[payment["status"]]
        # For another currency: what was paid is in that currency; the dollar figure is an estimate.
        estimate = review.get("converted") or conversion(state["invoice"])
        result = {**payment, "status": status, **({"approved_by": review["reviewer"]} if review else {}),
                  **({"usd_estimate": estimate} if estimate else {})}
        settle(state, seen.RECEIVED if status == "PAYMENT_NOT_SENT" else status, payment.get("error") or payment["payment_key"])
        finish(state, config, "pay", result)
        return {"result": result, "trail": _step("pay", **result)}

    def hold(state: State, config) -> dict:
        """Held for a person: put the invoice in the review queue and record the outcome so far.

        A node may take a second argument, `config`, which carries the run's settings. The thread
        ID is in it: stored with the queue entry, it is how the review screen later finds this exact
        paused run.

        This node finishes normally, so its update (result = HELD) is saved. The waiting happens
        in the next node. Splitting them matters: if the interrupt were in here, everything above
        it would run again on resume (see await_review), and the HELD result would never have
        been saved for the caller to see.
        """
        reasons = list(state["decision"].because)
        queue_id = ledger.enqueue(config["configurable"]["thread_id"], state["invoice"], reasons)
        result = {"status": "HELD", "queue_id": queue_id, "for": reasons}
        settle(state, "HELD", ", ".join(reasons))
        return {"result": result, "trail": _step("hold", **result)}

    def await_review(state: State) -> dict:
        """Wait for a person's decision. The run pauses inside this node, possibly for days.

        interrupt() stops the run and returns its argument to whoever started it. Nothing after
        it executes now. When the thread is resumed with Command(resume=answer), LangGraph runs
        this node AGAIN FROM ITS FIRST LINE, and this time interrupt() returns the answer instead
        of stopping. That re-run is why a node must be safe to repeat up to its interrupt; here
        there is nothing before it, so there is nothing to repeat.
        """
        review = interrupt({"queue_id": state["result"]["queue_id"], "file": state["invoice"].source_file,
                            "held_for": state["result"]["for"]})
        return {"review": review, "trail": _step("await_review", **review)}

    def reject(state: State, config) -> dict:
        """Rejected: logged with the reasons, to go back to the vendor. By the policy, or by a reviewer."""
        review = state.get("review")
        if review:
            result = {"status": "REJECTED", "rejected_by": review["reviewer"], "reasons": [review.get("note") or "rejected on review"]}
        else:
            result = {"status": "REJECTED", "reasons": list(state["decision"].because)}
        settle(state, "REJECTED", ", ".join(result["reasons"]))
        finish(state, config, "reject", result)
        return {"result": result, "trail": _step("reject", **result)}

    def failed(state: State, config) -> dict:
        """The file couldn't be read at all, so there is no invoice to decide on."""
        result = {"status": "ERROR", "error": state["error"]}
        finish(state, config, "failed", result)
        return {"result": result, "trail": _step("failed", **result)}

    # --- Routers. A conditional edge calls one of these and goes to the node it names. --------
    # A router only reads the state and returns a label. It decides nothing itself: the decision
    # was made by a node and is already in the state. That keeps "what was decided" (data, in
    # the state, saved and inspectable) separate from "where to go next" (the graph's shape).

    def after_ingest(state: State) -> str:
        return "failed" if state.get("error") else "validate"

    def after_register(state: State) -> str:
        return "skip" if "result" in state else "approve"

    def by_review(state: State) -> str:
        return "pay" if state["review"]["action"] == "approve" else "reject"

    def by_outcome(state: State) -> str:
        return {APPROVE: "pay", HOLD: "hold", REJECT: "reject"}[state["decision"].outcome]

    def after_critic(state: State) -> str:
        # Back to the reviewer while there are problems and rounds left; otherwise on to the outcome.
        # An assessment that still has problems after the last round goes forward marked as such
        # (critique.verdict stays REVISE): the outcome is the policy's or stricter either way.
        if state["critique"]["verdict"] == "REVISE" and state["critic_rounds"] < MAX_ROUNDS:
            return "reviewer"
        return by_outcome(state)

    # --- Wiring. --------------------------------------------------------------------------------
    graph = StateGraph(State)  # the State type tells LangGraph the keys and their reducers

    for node in (ingest, validate, register, skip, approve, reviewer, critic, pay, hold, await_review, reject, failed):
        graph.add_node(node.__name__, node)

    graph.add_edge(START, "ingest")  # START and END are LangGraph's own entry and exit markers
    # The third argument lists every node the router may return. It isn't required, but with it
    # LangGraph can check the labels and draw the branches.
    graph.add_conditional_edges("ingest", after_ingest, ["validate", "failed"])
    graph.add_edge("validate", "register")
    graph.add_conditional_edges("register", after_register, ["approve", "skip"])
    graph.add_edge("approve", "reviewer")
    graph.add_edge("reviewer", "critic")
    graph.add_conditional_edges("critic", after_critic, ["reviewer", "pay", "hold", "reject"])
    graph.add_edge("hold", "await_review")
    graph.add_conditional_edges("await_review", by_review, ["pay", "reject"])
    for last in ("pay", "reject", "skip", "failed"):
        graph.add_edge(last, END)

    # compile() checks the wiring (every node reachable, every edge pointing at a real node) and
    # returns the runnable object. The checkpointer is what makes interrupt() possible: a run can
    # only be paused if there is somewhere to keep its state.
    return graph.compile(checkpointer=checkpoints if checkpoints is not None else checkpointer())


def _plain(value):
    """For json.dumps: the types in a run's state that JSON has no form for."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, PurePath):
        return value.as_posix()  # forward slashes on every system, so a log reads the same wherever it was written
    return str(value)  # a record for people to read: an unfamiliar value is better written as text than not at all


def _thread(thread_id: str) -> dict:
    """The config that names a run. Every checkpointed call needs one: it is the key the state is saved under."""
    return {"configurable": {"thread_id": thread_id}}


def _stream(graph, start, thread_id: str, on_step, seen: int = 0) -> State:
    """Run (or resume) a thread until it ends or pauses, and return the state it reached.

    seen is how many trail entries were already reported: a resumed run starts with the trail
    from before it paused, and only what happens now is passed to on_step.
    """
    state: State = {}
    for state in graph.stream(start, _thread(thread_id), stream_mode="values"):
        for step in state.get("trail", [])[seen:]:
            if on_step:
                on_step(step)
        seen = len(state.get("trail", []))
    # A run that reached END will never be resumed, so its saved states are dead weight: a full
    # copy of the invoice per step. Only a paused run (next is non-empty) is kept.
    if not graph.get_state(_thread(thread_id)).next:
        graph.checkpointer.delete_thread(thread_id)
    return state


def run(graph, path: str | Path, on_step=None, thread_id: str | None = None) -> State:
    """Run one invoice through the graph and return the state it reached.

    graph.invoke(start) would run to END and return the final state in one call. This uses
    graph.stream() instead, which yields as the run progresses, so each step can be shown while
    it happens. stream_mode="values" yields the whole state after every step: first the starting
    state, then once per node. (The other common mode, "updates", yields only what each node
    returned.)

    The run ends in one of two ways: it reaches END, or it pauses at await_review. Either way the
    last state yielded is returned. For a held invoice that state already has result HELD with
    its queue number; the run itself is saved under thread_id and waits there.

    Args:
        graph: The compiled graph from build_graph().
        path: The invoice file.
        on_step: Called with each node's trail entry as soon as that node finishes. None: silent.
        thread_id: The name this run is saved under. A new one is made if not given: each
            invoice is its own thread.

    Returns:
        The state reached.
    """
    # trail starts as an empty list so the reducer always has a list to add to.
    return _stream(graph, {"path": str(path), "trail": []}, thread_id or uuid.uuid4().hex, on_step)


def cannot_approve(invoice: Invoice, rates=None) -> str | None:
    """Why a held invoice can't be released for payment, or None if it can.

    A reviewer can overrule a hold, within limits. These holds mean the invoice as it stands
    can't be paid correctly whoever approves it, so the only way out of the queue is to reject
    it (the vendor then sends a corrected one), or, for a missing exchange rate, to wait until
    the rate service has one.

    An invoice in another currency with a rate is approved like any other: it is paid in its own
    currency, and the reviewer sees the dollar estimate.

    Args:
        invoice: The held invoice.
        rates: The rate service, asked again now for an invoice that had no usable rate when it
            was checked: one may have been published since.
    """
    codes = {f["code"] for f in invoice.flags}
    if "INVOICE_NUMBER_MISSING" in codes:
        return "it has no invoice number, so the payment can't be recorded against it"
    if "DUPLICATE_CONFLICT" in codes:
        return "another invoice with the same vendor, number and revision is already on record"
    if invoice.total is None or invoice.total <= 0:
        return "it has no positive total to pay"
    if "CURRENCY_UNSUPPORTED" in codes and conversion(invoice, rates) is None:
        return (f"it is billed in {invoice.currency} and the rate service has no usable rate for it, so its cost in "
                "dollars isn't known. Approve it once a rate is available")
    amount, already_paid = payable(invoice)
    if already_paid is not None and (amount is None or amount <= 0):
        return f"it revises an invoice already paid {already_paid}, and nothing more is owed"
    return None


def payable(invoice: Invoice) -> tuple[Decimal | None, Decimal | None]:
    """What approving a held invoice pays, and what was paid before it.

    Normally the invoice total. For a revision of an invoice that was already paid, the
    difference. This is the one place that rule is written: release() pays by it and the review
    screen shows it.

    Returns:
        (amount to pay, amount already paid on earlier versions or None if this isn't a revision of a paid invoice).
    """
    revision = next((f for f in invoice.flags if f["code"] == "REVISION_OF_PAID"), None)
    if revision:
        return revision["difference"], revision["already_paid"]
    return invoice.total, None


def conversion(invoice: Invoice, rates=None) -> dict | None:
    """The dollar estimate for an invoice in another currency, or None if there is none (or it is in dollars).

    Taken from the invoice's CURRENCY_CONVERTED flag if it has one. Otherwise, for an invoice that
    had no usable rate when it was checked, the rate service is asked again.
    """
    for f in invoice.flags:
        if f["code"] == "CURRENCY_CONVERTED":
            return {k: f[k] for k in ("usd_estimate", "usd_per_unit", "rate_date", "rate_source")}
    if rates is None or not any(f["code"] == "CURRENCY_UNSUPPORTED" for f in invoice.flags) or invoice.total is None:
        return None
    rate, problem = usable_rate(rates, invoice.currency, invoice.date)  # the same rule the checks applied
    if problem:
        return None
    return {"usd_estimate": rate.to_usd(invoice.total), "usd_per_unit": rate.usd_per_unit, "rate_date": rate.as_of,
            "rate_source": rate.source}


def release(graph, ledger: Ledger, queue_id: int, action: str, reviewer: str, note: str | None = None, on_step=None,
            rates=None) -> State:
    """Apply a person's decision to a held invoice and let its run finish.

    Looks up the paused run by its queue number, checks the decision is allowed, records who
    decided what, and resumes the run. Command(resume=...) is how a paused thread is continued:
    its value becomes what interrupt() returns inside await_review.

    What is paid on approval:
    - normally, the invoice total, in the invoice's currency;
    - for a revision of an invoice that was already paid, the difference.
    The reviewer never enters an amount or a rate. For an invoice in another currency the dollar
    estimate comes from the rate service and is recorded with the decision.

    Args:
        graph: The compiled graph, built with the same checkpointer the run was paused under.
        ledger: The ledger holding the review queue.
        queue_id: The queue number shown when the invoice was held.
        action: "approve" or "reject".
        reviewer: Who is deciding. Recorded.
        note: Why. Required for a rejection, since it is what goes back to the vendor.
        on_step: Called with each new trail entry as the run continues.
        rates: The rate service, for an invoice that had no usable rate when it was held.

    Returns:
        The final state of the run.

    Raises:
        ValueError: No such waiting entry, an action that isn't approve or reject, a missing
            name or note, or an approval that isn't possible (see cannot_approve).
    """
    entry = ledger.queue_entry(queue_id)
    # A decision is recorded first and carried out second. An entry that is RESOLVED while its run
    # is still paused is one where the second step never happened: finish it now.
    unfinished = entry is not None and entry["status"] == "RESOLVED" and bool(graph.get_state(_thread(entry["thread_id"])).next)
    if entry is None or (entry["status"] != "WAITING" and not unfinished):
        raise ValueError(f"no invoice is waiting under queue number {queue_id}"
                         + (f" (it is {entry['status']})" if entry else ""))
    if unfinished and action != entry["action"]:
        raise ValueError(f"queue number {queue_id} was already decided: {entry['action']} by {entry['reviewer']}. "
                         f"That decision was recorded but not carried out; repeat it ({entry['action']}) to finish it.")
    if unfinished:  # carry out the decision as it was recorded, whoever is finishing it
        reviewer, note = entry["reviewer"], entry["note"]
    if action not in ("approve", "reject"):
        raise ValueError(f"action must be approve or reject, not {action!r}")
    if not reviewer or not reviewer.strip():
        raise ValueError("a reviewer name is required: the decision is recorded against it")
    if action == "reject" and not (note or "").strip():
        raise ValueError("a rejection needs a note: it is the reason that goes back to the vendor")
    # get_state() reads a thread's saved state without running anything.
    saved = graph.get_state(_thread(entry["thread_id"]))
    if not saved.next:
        # The saved run is gone (the checkpoints file was deleted or replaced). There is nothing
        # to resume, and leaving the invoice HELD would strand it: put it back to be processed.
        why = "its saved run was not found, so it could not be released"
        again = ledger.reopen(entry, why)
        raise ValueError(f"queue number {queue_id} ({entry['source_file']}) has no saved run to resume, so it can't be "
                         f"decided here. It has been closed as LOST" + (" and the invoice put back as unprocessed" if again else "")
                         + f": run main.py on {entry['source_file']} again and it will be checked and queued afresh.")
    invoice = saved.values["invoice"]
    amount, basis = None, None
    if action == "approve":
        if reason := cannot_approve(invoice, rates):
            raise ValueError(f"{invoice.source_file} can't be approved: {reason}, or reject it with a note.")
        basis = conversion(invoice, rates)
        to_pay, already_paid = payable(invoice)
        amount = to_pay if already_paid is not None else None   # None: the pay step uses the invoice total
    review = {"action": action, "reviewer": reviewer.strip(), "note": (note or "").strip() or None, "amount": amount,
              **({"converted": basis} if basis else {})}
    # The decision is taken by whoever records it first. Two people deciding the same invoice at
    # once both get this far; the UPDATE lets one through, and the other must not resume the run.
    if not ledger.resolve(entry["thread_id"], action, review["reviewer"], review["note"], amount) and not unfinished:
        raise ValueError(f"queue number {queue_id} was decided by someone else a moment ago; nothing was changed")
    return _stream(graph, Command(resume=review), entry["thread_id"], on_step, seen=len(saved.values["trail"]))
