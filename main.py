"""The entry point the brief names: python main.py --invoice_path=data/invoices/invoice_1001.txt

Runs one invoice file, or every invoice in a folder, through the pipeline graph (pipeline/graph.py):

    ingest     structured files by the deterministic normalizer, TXT and PDF by LLM extraction
    validate   repairs, then every check against the inventory, each flag graded
    approve    the policy floor turns the flags into APPROVE, HOLD or REJECT

    register   the ledger: is this a duplicate, or a revision of one already handled?
    pay / hold / reject, or skip for an invoice that was already handled

Prints a JSON list on stdout, one {"file", "result", "decision", "trail", "invoice"} entry per
file ({"file", "error"} for a file that can't be read); exits 1 if any file is an error. With
--trace, each step is also printed to stderr as it happens, one JSON line per node.

The ledger (ledger.db) persists between runs, so running a file twice pays it once. --fresh
starts with an empty one.
"""
import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

import normalizer
from extraction import TEXT_SUFFIXES
from extraction.__main__ import emit, model_options, provider_from
from ledger import Ledger
from normalizer.__main__ import _json_default
from normalizer.parsing import LOADERS
from pipeline import build_graph, run
from seed_inventory import seed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Process an invoice file, or every invoice in a folder.")
    ap.add_argument("--invoice_path", required=True, help="an invoice file or a folder of invoices")
    ap.add_argument("--inventory", default="inventory.db",
                    help="SQLite inventory DB (default: inventory.db, created from the seed data if missing)")
    ap.add_argument("--ledger", default="ledger.db",
                    help="SQLite ledger of invoices seen and payments made (default: ledger.db)")
    ap.add_argument("--fresh", action="store_true", help="start with an empty ledger, forgetting earlier runs")
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
    try:
        inventory = normalizer.inventory_from_db(args.inventory)
    except sqlite3.Error as e:
        return emit([{"file": args.inventory, "error": f"{type(e).__name__}: {e}"}])

    if args.fresh:
        Path(args.ledger).unlink(missing_ok=True)
    ledger = Ledger(args.ledger)
    graph = build_graph(provider_from(args), inventory, ledger)
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
                            "trail": state["trail"], "invoice": asdict(state["invoice"])})
    paid = [r["result"] for r in results if r.get("result", {}).get("status") == "PAID" and not r["result"]["repeated"]]
    print(f"{len(results)} files: paid {len(paid)} now, ${sum(p['amount'] for p in paid):,.2f}. "
          f"Ledger {args.ledger}: {len(ledger.payments())} payments, ${ledger.total_paid():,.2f} in all.", file=sys.stderr)
    return emit(results)


if __name__ == "__main__":
    sys.exit(main())
