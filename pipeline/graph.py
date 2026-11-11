"""The invoice pipeline as a LangGraph graph.

LangGraph runs a program as a graph. Three things define one:

    State   one dictionary that travels through the graph. Every node reads it and returns the
            keys it wants to change.
    Nodes   plain functions: state in, a partial update out. A node never calls the next node.
    Edges   what runs after what. A fixed edge always goes to the same node; a conditional edge
            calls a small function on the state and goes where it says.

This graph, with the decision the policy made choosing the branch:

    START -> ingest -> validate -> approve -+-> pay     -> END
                 |                          +-> hold    -> END
                 +-> failed -> END          +-> reject  -> END

Why a graph and not four function calls in a row: the next stages need what a script handles
badly. The reviewer and critic loop is an edge that points backwards. A held invoice is a run
that pauses and is resumed later by a person. Both are built-in graph features, and both slot
into this file as new nodes and edges without changing the stages themselves.
"""
import operator
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

import normalizer
from approval import APPROVE, HOLD, REJECT, Decision, decide
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
    decision: Decision
    result: dict
    error: str
    trail: Annotated[list[dict], operator.add]


def _step(node: str, **detail) -> list[dict]:
    """One trail entry, as the one-item list the `trail` reducer appends."""
    return [{"node": node, **detail}]


def build_graph(provider: Provider, inventory: dict | None = None):
    """Build and compile the pipeline.

    The provider and inventory are not in the State. State is the data about one invoice, and it
    is what LangGraph will save at each step once checkpoints are on; a model client and a stock
    table are tools the nodes use, the same for every invoice. So the nodes are defined in here
    and simply use them (a closure).

    Args:
        provider: The model provider for TXT and PDF files (Grok, Replay, a test stub).
        inventory: Item name -> stock on hand. None skips the product and stock checks.

    Returns:
        The compiled graph. Call .invoke({"path": ...}) for the final state, or .stream(...) to
        see each node's update as it happens.
    """

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

    def approve(state: State) -> dict:
        """Stage 3. The policy floor: the minimum outcome the flags require. (The reviewer agent will sit here.)"""
        decision = decide(state["invoice"].flags)
        return {"decision": decision,
                "trail": _step("approve", outcome=decision.outcome, rule=decision.rule, because=list(decision.because))}

    def pay(state: State) -> dict:
        """Stage 4, approved branch. Payment isn't built: this records that the invoice is cleared for it."""
        result = {"status": "APPROVED", "amount": state["invoice"].total, "payment": "NOT_BUILT"}
        return {"result": result, "trail": _step("pay", **result)}

    def hold(state: State) -> dict:
        """Held for a person. (This node will pause the run with an interrupt and wait for their answer.)"""
        result = {"status": "HELD", "for": list(state["decision"].because)}
        return {"result": result, "trail": _step("hold", **result)}

    def reject(state: State) -> dict:
        """Rejected: logged with the reasons, to go back to the vendor."""
        result = {"status": "REJECTED", "reasons": list(state["decision"].because)}
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

    def by_outcome(state: State) -> str:
        return {APPROVE: "pay", HOLD: "hold", REJECT: "reject"}[state["decision"].outcome]

    # --- Wiring. --------------------------------------------------------------------------------
    graph = StateGraph(State)  # the State type tells LangGraph the keys and their reducers

    for node in (ingest, validate, approve, pay, hold, reject, failed):
        graph.add_node(node.__name__, node)

    graph.add_edge(START, "ingest")  # START and END are LangGraph's own entry and exit markers
    # The third argument lists every node the router may return. It isn't required, but with it
    # LangGraph can check the labels and draw the branches.
    graph.add_conditional_edges("ingest", after_ingest, ["validate", "failed"])
    graph.add_edge("validate", "approve")
    graph.add_conditional_edges("approve", by_outcome, ["pay", "hold", "reject"])
    for last in ("pay", "hold", "reject", "failed"):
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
