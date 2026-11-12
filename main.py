"""The entry point the brief names: python main.py --invoice_path=data/invoices/invoice_1001.txt

Runs one invoice file, or every invoice in a folder, through the pipeline graph (pipeline/graph.py):

    ingest     structured files by the deterministic normalizer, TXT and PDF by LLM extraction
    validate   repairs, then every check against the inventory, each flag graded
    approve    the policy floor turns the flags into APPROVE, HOLD or REJECT
    reviewer   the reviewer agent looks into holds and warned approvals, and writes the reason

    register   the ledger: is this a duplicate, or a revision of one already handled?
    pay / hold / reject, or skip for an invoice that was already handled

A held invoice is a paused run, saved in checkpoints.db. review.py lists what is waiting and
resumes a run with a person's decision.

Prints a JSON list on stdout, one {"file", "result", "decision", "assessment", "trail", "invoice"} entry per
file ({"file", "error"} for a file that can't be read); exits 1 if any file is an error. With
--trace, each step is also printed to stderr as it happens, one JSON line per node.

The ledger (ledger.db) persists between runs, so running a file twice pays it once. --fresh
sets it aside (kept, renamed) and starts with an empty one, which means invoices it had paid
are paid again.
"""
import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import normalizer
from extraction import TEXT_SUFFIXES
from extraction.__main__ import emit, model_options, provider_from
from ledger import Ledger
from normalizer.__main__ import _json_default
from normalizer.parsing import LOADERS
from pipeline import build_graph, checkpointer, run
from rates import MockRates
from rates import seed as seed_rates
from seed_inventory import seed


def _sums(totals: dict) -> str:
    """Amounts per currency as text: "$26,325.00" or "$26,325.00 + 4,125.00 EUR"."""
    if not totals:
        return "$0.00"
    return " + ".join(f"${v:,.2f}" if c == "USD" else f"{v:,.2f} {c}" for c, v in sorted(totals.items(), key=lambda kv: kv[0] != "USD"))


def set_aside(ledger_path: Path) -> Path | None:
    """Move an existing ledger out of the way for --fresh, keeping it, and say what that means.

    The ledger is the only record of what has been paid. Deleting it would erase that record, and
    the next run would pay the same invoices again without a trace. So the file is renamed with a
    timestamp, never removed, and the payments it held are reported.

    Returns:
        Where the old ledger went, or None if there was none.
    """
    if not ledger_path.exists():
        return None
    old = Ledger(ledger_path)
    count, totals = len(old.payments()), _sums(old.paid_totals())
    old.close()
    kept = ledger_path.with_name(f"{ledger_path.name}.{datetime.now():%Y%m%d-%H%M%S-%f}.bak")
    ledger_path.rename(kept)
    print(f"--fresh: the ledger was set aside as {kept.name}. It recorded {count} payments, {totals}; "
          "with an empty ledger those invoices are not recognised and will be paid again.", file=sys.stderr)
    return kept


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Process an invoice file, or every invoice in a folder.")
    ap.add_argument("--invoice_path", required=True, help="an invoice file or a folder of invoices")
    ap.add_argument("--inventory", default="inventory.db",
                    help="SQLite inventory DB (default: inventory.db, created from the seed data if missing)")
    ap.add_argument("--ledger", default="ledger.db",
                    help="SQLite ledger of invoices seen and payments made (default: ledger.db)")
    ap.add_argument("--fresh", action="store_true",
                    help="set the existing ledger aside and start with an empty one. Invoices it had paid will be paid again")
    ap.add_argument("--rates", default="rates.db",
                    help="SQLite table of exchange rates, the mock rate service (default: rates.db, created from "
                         "made-up seed rates if missing)")
    ap.add_argument("--checkpoints", default="checkpoints.db",
                    help="SQLite file of saved runs, so a held invoice can be released later (default: checkpoints.db)")
    ap.add_argument("--trace", action="store_true", help="print each pipeline step to stderr as it happens")
    model_options(ap)
    args = ap.parse_args(argv)
    path = Path(args.invoice_path)
    if not path.exists():
        # The brief's own example, data/invoices/invoice1.txt, isn't in the sample data: say what is.
        folder = path.parent if path.parent.is_dir() else Path("data/invoices")
        print(f"No such file or folder: {path}", file=sys.stderr)
        if folder.is_dir():
            print("Available in " + str(folder) + ":\n  " + "\n  ".join(sorted(p.name for p in folder.iterdir() if p.is_file())),
                  file=sys.stderr)
        return 2
    if not Path(args.inventory).exists():
        seed(args.inventory)
    if not Path(args.rates).exists():
        seed_rates(args.rates)
    try:
        inventory = normalizer.inventory_from_db(args.inventory)
        rates = MockRates(args.rates)
    except sqlite3.Error as e:
        return emit([{"file": f"{args.inventory} / {args.rates}", "error": f"{type(e).__name__}: {e}"}])

    if args.fresh:
        set_aside(Path(args.ledger))
    ledger = Ledger(args.ledger)
    graph = build_graph(provider_from(args), inventory, ledger, checkpointer(args.checkpoints), rates)
    results = []
    for file in normalizer.expand([path], LOADERS.keys() | TEXT_SUFFIXES):
        on_step = None
        if args.trace:
            def on_step(step, file=file):
                print(json.dumps({"file": file.name, **step}, default=_json_default), file=sys.stderr)
        state = run(graph, file, on_step)
        if "error" in state:
            results.append({"file": str(file), "error": state["error"]})
        else:
            # The outcome goes first in each entry: it is what a reader wants before the detail.
            # A skipped invoice has no decision: it was already handled.
            decision = asdict(state["decision"]) if "decision" in state else None
            results.append({"file": str(file), "result": state["result"], "decision": decision,
                            "assessment": state.get("assessment"), "trail": state["trail"],
                            "invoice": asdict(state["invoice"])})
    paid = [r["result"] for r in results if r.get("result", {}).get("status") == "PAID" and not r["result"]["repeated"]]
    not_sent = [r["file"] for r in results if r.get("result", {}).get("status") == "PAYMENT_NOT_SENT"]
    if not_sent:
        print(f"{len(not_sent)} payment(s) could not be attempted and will be taken up again on the next run: "
              f"{', '.join(Path(f).name for f in not_sent)}", file=sys.stderr)
    unconfirmed = [r["file"] for r in results if r.get("result", {}).get("status") == "PAYMENT_UNCONFIRMED"]
    if unconfirmed:
        print(f"{len(unconfirmed)} payment(s) sent but not confirmed; check with the bank before anything is "
              f"paid again: {', '.join(Path(f).name for f in unconfirmed)}", file=sys.stderr)
    held = [r for r in results if r.get("result", {}).get("status") == "HELD"]
    if held:
        print(f"{len(held)} held for review. See them with: python review.py --ledger {args.ledger} "
              f"--checkpoints {args.checkpoints}", file=sys.stderr)
    now: dict = {}
    for p in paid:  # per currency: euros and dollars are never added together
        now[p["currency"]] = now.get(p["currency"], 0) + p["amount"]
    print(f"{len(results)} files: paid {len(paid)} now ({_sums(now)}). "
          f"Ledger {args.ledger}: {len(ledger.payments())} payments ({_sums(ledger.paid_totals())}) in all.", file=sys.stderr)
    return emit(results)


if __name__ == "__main__":
    sys.exit(main())
