"""The entry point the brief names: python main.py --invoice_path=data/invoices/invoice_1001.txt

Runs ingestion and validation for one invoice file or a folder of them: structured files (JSON,
CSV, XML) go through the deterministic normalizer, TXT and PDF through LLM extraction, and every
invoice gets the same checks. Prints a JSON list on stdout; exits 1 if any file is an error.

Any other option is passed on to `python -m extraction` (--offline, --model, --replays, ...).
"""
import argparse
import sys
from pathlib import Path

from extraction.__main__ import main as run
from seed_inventory import seed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Process an invoice file, or every invoice in a folder.")
    ap.add_argument("--invoice_path", required=True, help="an invoice file or a folder of invoices")
    ap.add_argument("--inventory", default="inventory.db",
                    help="SQLite inventory DB (default: inventory.db, created from the seed data if missing)")
    args, rest = ap.parse_known_args(argv)
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
    return run([str(path), "--inventory", args.inventory, *rest])


if __name__ == "__main__":
    sys.exit(main())
