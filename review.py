"""The review queue: see the invoices held for a person, and decide them.

    python review.py                                   list what is waiting
    python review.py show 3                            one invoice in full: its flags and the steps so far
    python review.py approve 3 --by "Dana Lee"         release it for payment
    python review.py reject 3 --by "Dana Lee" --note "Quantity exceeds the PO; send a corrected invoice"

A held invoice is a pipeline run paused at its await_review step (see pipeline/graph.py). Deciding
it resumes that run, in this process, from where main.py left it: approve goes on to payment,
reject records the reason. Who decided, and why, is kept in the ledger.

What approval pays: the invoice total, in the invoice's own currency; for a revision of an invoice
already paid, the difference. The reviewer never types an amount or a rate. An invoice in another
currency is listed with its dollar estimate from the rate service, and is approved like any other.

Some holds can't be approved, only rejected: an invoice with no invoice number, or one that
conflicts with an invoice already on record. An invoice in a currency the rate service has no
rate for waits until it has one.
"""
import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

from extraction import Replay
from ledger import Ledger
from ledger.ledger import currency_of
from normalizer.__main__ import _json_default
from pipeline import build_graph, cannot_approve, checkpointer, conversion, release
from rates import MockRates
from pipeline.graph import _thread


def _amount(entry: dict, invoice, rates) -> str:
    """The total as a reviewer should read it: dollars, or the invoiced currency with its dollar estimate."""
    if not entry["total"]:
        return "-"
    total = f"{float(entry['total']):,.2f}"
    if invoice is None or currency_of(invoice) == "USD":
        return total
    estimate = conversion(invoice, rates)
    return f"{total} {invoice.currency} " + (f"(~${estimate['usd_estimate']:,.2f})" if estimate else "(no rate)")


def _table(entries: list[dict], invoices: dict, rates, stuck: list[dict] = ()) -> str:
    """The review list. `stuck` are entries already decided whose run never finished; they are listed too, marked."""
    if not entries and not stuck:
        return "Nothing is waiting for review."
    lines = [f"{'#':>3}  {'file':<28}{'vendor':<30}{'total':>28}  held for", "-" * 124]
    for e in entries:
        lines.append(f"{e['id']:>3}  {e['source_file']:<28}{(e['vendor'] or '-')[:28]:<30}"
                     f"{_amount(e, invoices.get(e['id']), rates):>28}  {e['reasons']}")
    for e in stuck:
        lines.append(f"{e['id']:>3}  {e['source_file']:<28}{(e['vendor'] or '-')[:28]:<30}"
                     f"{_amount(e, invoices.get(e['id']), rates):>28}  DECIDED ({e['action']} by {e['reviewer']}) BUT NOT "
                     f"CARRIED OUT: run `{e['action']} {e['id']} --by NAME` again to finish it")
    lines.append(f"\n{len(entries)} waiting. Decide one with: python review.py approve N --by NAME   or   reject N --by NAME --note WHY")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="List and decide invoices held for review.")
    ap.add_argument("--ledger", default="ledger.db", help="the ledger main.py used (default: ledger.db)")
    ap.add_argument("--checkpoints", default="checkpoints.db", help="the saved runs main.py used (default: checkpoints.db)")
    ap.add_argument("--rates", default="rates.db", help="the exchange-rate table (default: rates.db)")
    sub = ap.add_subparsers(dest="command")
    sub.add_parser("list", help="what is waiting (the default)")
    show = sub.add_parser("show", help="one held invoice in full")
    show.add_argument("queue_id", type=int)
    for name in ("approve", "reject"):
        p = sub.add_parser(name, help=f"{name} a held invoice")
        p.add_argument("queue_id", type=int)
        p.add_argument("--by", required=True, help="who is deciding (recorded)")
        p.add_argument("--note", help="why" + (" (required)" if name == "reject" else ""))
    args = ap.parse_args(argv)

    if not Path(args.ledger).exists():
        print(f"No ledger at {args.ledger}: nothing has been processed yet.", file=sys.stderr)
        return 1
    ledger = Ledger(args.ledger)
    # The saved runs must already exist: opening a path that doesn't would create an empty file,
    # and every held invoice would then look as if its run were lost.
    if not Path(args.checkpoints).exists():
        print(f"No saved runs at {args.checkpoints}. Held invoices can be listed from the ledger but not shown in full "
              "or decided: pass --checkpoints with the file main.py used.", file=sys.stderr)
        if args.command not in (None, "list"):
            return 1
        print(_table(ledger.queue(), {}, None, ledger.decided_not_finished()))
        return 0
    try:
        rates = MockRates(args.rates)
    except sqlite3.Error:
        rates = None  # no rate table: an invoice held without a rate stays unapprovable, and says so
    # Resuming needs the same graph shape and the same checkpoints the run was paused under. The
    # nodes left to run (pay or reject) use neither the model nor the inventory.
    graph = build_graph(Replay("data/replays"), None, ledger, checkpointer(args.checkpoints), rates)

    def saved(entry):
        return graph.get_state(_thread(entry["thread_id"])).values

    if args.command in (None, "list"):
        waiting, stuck = ledger.queue(), ledger.decided_not_finished()
        states = {e["id"]: saved(e) for e in waiting + stuck}
        print(_table(waiting, {i: s.get("invoice") for i, s in states.items()}, rates, stuck))
        advice = [(e["id"], states[e["id"]].get("assessment")) for e in waiting]
        if any(a and a["by"] == "reviewer_agent" for _, a in advice):
            print("\nThe reviewer agent's advice (a recommendation, not a decision):")
            for queue_id, a in advice:
                if a and a["by"] == "reviewer_agent":
                    checked = states[queue_id].get("critique", {}).get("verdict")
                    print(f"{queue_id:>3}  {a['recommendation']}"
                          + ("" if checked == "ACCEPT" else " (NOT VERIFIED: the critic's objections were not all resolved)")
                          + f": {a['rationale']}")
                    for q in a["open_questions"]:
                        print(f"       ? {q}")
        return 0

    if args.command == "show":
        entry = ledger.queue_entry(args.queue_id)
        if entry is None:
            print(f"No queue number {args.queue_id}.", file=sys.stderr)
            return 1
        state = saved(entry)
        out = {"queue": entry}
        if state:
            out |= {"cannot_approve_because": cannot_approve(state["invoice"], rates),
                    "usd_estimate": conversion(state["invoice"], rates), "decision": asdict(state["decision"]),
                    "assessment": state.get("assessment"), "critique": state.get("critique"), "trail": state["trail"],
                    "invoice": asdict(state["invoice"])}
        print(json.dumps(out, indent=2, default=_json_default))
        return 0
    try:
        state = release(graph, ledger, args.queue_id, args.command, args.by, args.note, rates=rates)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    print(json.dumps({"file": state["invoice"].source_file, "result": state["result"], "review": state["review"],
                      "trail": state["trail"]}, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    sys.exit(main())
