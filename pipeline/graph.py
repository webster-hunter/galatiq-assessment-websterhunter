"""The invoice pipeline as a LangGraph graph.

LangGraph runs a program as a graph. Three things define one:

    State   one dictionary that travels through the graph. Every node reads it and returns the
            keys it wants to change.
    Nodes   plain functions: state in, a partial update out. A node never calls the next node.
    Edges   what runs after what. A fixed edge always goes to the same node; a conditional edge
            calls a small function on the state and goes where it says.

This graph, with the decision the policy made choosing the last branch:

    START -> ingest -> validate -> register -> approve -+-> pay     -> END
                 |                     |                +-> hold    -> END
                 +-> failed -> END     +-> skip -> END  +-> reject  -> END

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
import operator
from decimal import Decimal
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

import normalizer
from approval import APPROVE, HOLD, REJECT, Decision, decide
from ledger import Ledger
from ledger import ledger as seen
from extraction import TEXT_SUFFIXES, Provider, ProviderError, extract
from normalizer.models import Invoice
from normalizer.parsing import LOADERS


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
    result: dict
    error: str
    trail: Annotated[list[dict], operator.add]


def _step(node: str, **detail) -> list[dict]:
    """One trail entry, as the one-item list the `trail` reducer appends."""
    return [{"node": node, **detail}]


def build_graph(provider: Provider, inventory: dict | None = None, ledger: Ledger | None = None):
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

    Returns:
        The compiled graph. Call .invoke({"path": ...}) for the final state, or .stream(...) to
        see each node's update as it happens.
    """

    ledger = ledger if ledger is not None else Ledger(":memory:")

    # --- Nodes. Each takes the state and returns only the keys it changes. --------------------

    def ingest(state: State) -> dict:
        """Stage 1. Read the file into an Invoice: a parser for structured files, the model for text."""
        path = Path(state["path"])
        suffix = path.suffix.lower()
        try:
            if suffix in TEXT_SUFFIXES:
                invoice = extract(path, provider)
            elif suffix in LOADERS:
                invoice = LOADERS[suffix](path)
            else:
                raise ValueError(f"No loader for {suffix} files: {path}")
        except (*normalizer.LOAD_ERRORS, ProviderError) as e:
            # A node reports failure through the state, like any other result. The edge after
            # ingest reads `error` and routes to the `failed` node. Any other exception is a bug
            # in this code and is left to raise.
            error = f"{type(e).__name__}: {e}"
            return {"error": error, "trail": _step("ingest", error=error)}
        return {"invoice": invoice,
                "trail": _step("ingest", method="llm" if suffix in TEXT_SUFFIXES else "parser",
                               model_attempts=len(invoice.extraction_attempts))}

    def validate(state: State) -> dict:
        """Stage 2. Repair what the invoice proves, run every check against the inventory, grade each flag."""
        invoice = normalizer.check(state["invoice"], inventory)
        return {"invoice": invoice, "trail": _step("validate", flags=[f["code"] for f in invoice.flags])}

    def register(state: State) -> dict:
        """Ask the ledger whether this invoice has been seen, and record it if it is new.

        This is the only stage that compares an invoice with others. What it finds either ends
        the run here (an exact duplicate needs no decision) or travels on as a flag, so that the
        policy, not this node, decides what a revision or a conflict means.
        """
        invoice = state["invoice"]
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
            paid = Decimal(earlier["total"])
            flag("REVISION_OF_PAID", earlier_file=earlier["source_file"], already_paid=paid,
                 difference=invoice.total - paid if invoice.total is not None else None)
        elif found.kind == seen.REVISION_OF_UNPAID:
            ledger.supersede(earlier, invoice.source_file)
            flag("SUPERSEDES_UNPAID", earlier_file=earlier["source_file"], earlier_status=earlier["status"])
        ledger.record(invoice, "RECEIVED")
        return {"invoice": invoice, "recorded": True, "trail": _step("register", **step)}

    def skip(state: State) -> dict:
        """The invoice was already handled, as this file or as another copy. Nothing is decided or paid."""
        return {"trail": _step("skip", **state["result"])}

    def approve(state: State) -> dict:
        """Stage 3. The policy floor: the minimum outcome the flags require. (The reviewer agent will sit here.)"""
        decision = decide(state["invoice"].flags)
        return {"decision": decision,
                "trail": _step("approve", outcome=decision.outcome, rule=decision.rule, because=list(decision.because))}

    def settle(state: State, status: str, detail: str) -> None:
        """Write the outcome to the invoice's ledger row, if it has one of its own."""
        if state.get("recorded"):
            ledger.record(state["invoice"], status, detail)

    def pay(state: State) -> dict:
        """Stage 4, approved branch. Pay through the ledger, which pays any one invoice only once."""
        payment = ledger.pay(state["invoice"])
        result = {"status": payment["status"], **payment}
        settle(state, payment["status"], payment["payment_key"])
        return {"result": result, "trail": _step("pay", **result)}

    def hold(state: State) -> dict:
        """Held for a person. (This node will pause the run with an interrupt and wait for their answer.)"""
        result = {"status": "HELD", "for": list(state["decision"].because)}
        settle(state, "HELD", ", ".join(state["decision"].because))
        return {"result": result, "trail": _step("hold", **result)}

    def reject(state: State) -> dict:
        """Rejected: logged with the reasons, to go back to the vendor."""
        result = {"status": "REJECTED", "reasons": list(state["decision"].because)}
        settle(state, "REJECTED", ", ".join(state["decision"].because))
        return {"result": result, "trail": _step("reject", **result)}

    def failed(state: State) -> dict:
        """The file couldn't be read at all, so there is no invoice to decide on."""
        result = {"status": "ERROR", "error": state["error"]}
        return {"result": result, "trail": _step("failed", **result)}

    # --- Routers. A conditional edge calls one of these and goes to the node it names. --------
    # A router only reads the state and returns a label. It decides nothing itself: the decision
    # was made by a node and is already in the state. That keeps "what was decided" (data, in
    # the state, saved and inspectable) separate from "where to go next" (the graph's shape).

    def after_ingest(state: State) -> str:
        return "failed" if state.get("error") else "validate"

    def after_register(state: State) -> str:
        return "skip" if "result" in state else "approve"

    def by_outcome(state: State) -> str:
        return {APPROVE: "pay", HOLD: "hold", REJECT: "reject"}[state["decision"].outcome]

    # --- Wiring. --------------------------------------------------------------------------------
    graph = StateGraph(State)  # the State type tells LangGraph the keys and their reducers

    for node in (ingest, validate, register, skip, approve, pay, hold, reject, failed):
        graph.add_node(node.__name__, node)

    graph.add_edge(START, "ingest")  # START and END are LangGraph's own entry and exit markers
    # The third argument lists every node the router may return. It isn't required, but with it
    # LangGraph can check the labels and draw the branches.
    graph.add_conditional_edges("ingest", after_ingest, ["validate", "failed"])
    graph.add_edge("validate", "register")
    graph.add_conditional_edges("register", after_register, ["approve", "skip"])
    graph.add_conditional_edges("approve", by_outcome, ["pay", "hold", "reject"])
    for last in ("pay", "hold", "reject", "skip", "failed"):
        graph.add_edge(last, END)

    # compile() checks the wiring (every node reachable, every edge pointing at a real node) and
    # returns the runnable object. Later this is also where the checkpointer is passed in.
    return graph.compile()


def run(graph, path: str | Path, on_step=None) -> State:
    """Run one invoice through the graph and return the final state.

    graph.invoke(start) would run to END and return the final state in one call. This uses
    graph.stream() instead, which yields as the run progresses, so each step can be shown while
    it happens. stream_mode="values" yields the whole state after every step: first the starting
    state, then once per node. (The other common mode, "updates", yields only what each node
    returned.) The last state yielded is the final one.

    Args:
        graph: The compiled graph from build_graph().
        path: The invoice file.
        on_step: Called with each node's trail entry as soon as that node finishes. None: silent.

    Returns:
        The final state.
    """
    state: State = {}
    # trail starts as an empty list so the reducer always has a list to add to.
    for state in graph.stream({"path": str(path), "trail": []}, stream_mode="values"):
        if on_step and state["trail"]:
            on_step(state["trail"][-1])
    return state
