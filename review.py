"""The review queue: see the invoices held for a person, and decide them.

    python review.py                                   list what is waiting
    python review.py show 3                            one invoice in full: its flags and the steps so far
    python review.py approve 3 --by "Dana Lee"         release it for payment
    python review.py approve 6 --by "Dana Lee" --amount 4480.00 --note "EUR at 1.0861, treasury rate 26 Jan"
    python review.py reject 3 --by "Dana Lee" --note "Quantity exceeds the PO; send a corrected invoice"

A held invoice is a pipeline run paused at its await_review step (see pipeline/graph.py). Deciding
it resumes that run, in this process, from where main.py left it: approve goes on to payment,
reject records the reason. Who decided, and why, is kept in the ledger.

What approval pays: the invoice total; for a revision of an invoice already paid, the difference;
for an invoice in another currency, the dollar amount the reviewer gives with --amount (the
system holds no exchange rates, so the person who knows the rate states it, in the note).

Some holds can't be approved, only rejected: an invoice with no invoice number, or one that
conflicts with an invoice already on record.
"""
import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from extraction import Replay
from ledger import Ledger
from normalizer.__main__ import _json_default
from normalizer.parsing import _money
from pipeline import build_graph, cannot_approve, checkpointer, needs_dollar_amount, release
from pipeline.graph import _thread


def _table(entries: list[dict]) -> str:
    if not entries:
        return "Nothing is waiting for review."
    lines = [f"{'#':>3}  {'file':<28}{'vendor':<32}{'total':>12}  held for", "-" * 110]
    for e in entries:
        total = f"{float(e['total']):,.2f}" if e["total"] else "-"
        lines.append(f"{e['id']:>3}  {e['source_file']:<28}{(e['vendor'] or '-')[:30]:<32}{total:>12}  {e['reasons']}")
    lines.append(f"\n{len(entries)} waiting. Decide one with: python review.py approve N --by NAME   or   reject N --by NAME --note WHY")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="List and decide invoices held for review.")
    ap.add_argument("--ledger", default="ledger.db", help="the ledger main.py used (default: ledger.db)")
    ap.add_argument("--checkpoints", default="checkpoints.db", help="the saved runs main.py used (default: checkpoints.db)")
    sub = ap.add_subparsers(dest="command")
    sub.add_parser("list", help="what is waiting (the default)")
    show = sub.add_parser("show", help="one held invoice in full")
    show.add_argument("queue_id", type=int)
    for name in ("approve", "reject"):
        p = sub.add_parser(name, help=f"{name} a held invoice")
        p.add_argument("queue_id", type=int)
        p.add_argument("--by", required=True, help="who is deciding (recorded)")
        p.add_argument("--note", help="why" + (" (required)" if name == "reject" else " (required with --amount)"))
        if name == "approve":
            p.add_argument("--amount", help="dollars to pay, for an invoice billed in another currency")
    args = ap.parse_args(argv)

    if not Path(args.ledger).exists():
        print(f"No ledger at {args.ledger}: nothing has been processed yet.", file=sys.stderr)
        return 1
    ledger = Ledger(args.ledger)
    if args.command in (None, "list"):
        print(_table(ledger.queue()))
        return 0

    # Resuming needs the same graph shape and the same checkpoints the run was paused under. The
    # nodes left to run (pay or reject) use neither the model nor the inventory.
    graph = build_graph(Replay("data/replays"), None, ledger, checkpointer(args.checkpoints))
    if args.command == "show":
        entry = ledger.queue_entry(args.queue_id)
        if entry is None:
            print(f"No queue number {args.queue_id}.", file=sys.stderr)
            return 1
        saved = graph.get_state(_thread(entry["thread_id"])).values
        out = {"queue": entry}
        if saved:
            out |= {"cannot_approve_because": cannot_approve(saved["invoice"]),
                    "approval_needs_dollar_amount": needs_dollar_amount(saved["invoice"]), "decision": asdict(saved["decision"]),
                    "trail": saved["trail"], "invoice": asdict(saved["invoice"])}
        print(json.dumps(out, indent=2, default=_json_default))
        return 0
    try:
        amount = None
        if getattr(args, "amount", None) is not None:
            try:
                amount = _money(args.amount)  # "4480", "4,480.00" and "$4,480.00" all read as the same amount
            except (ValueError, ArithmeticError):
                raise ValueError(f"--amount must be a plain dollar amount, not {args.amount!r}") from None
        state = release(graph, ledger, args.queue_id, args.command, args.by, args.note, amount=amount)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    print(json.dumps({"file": state["invoice"].source_file, "result": state["result"], "review": state["review"],
                      "trail": state["trail"]}, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    sys.exit(main())
