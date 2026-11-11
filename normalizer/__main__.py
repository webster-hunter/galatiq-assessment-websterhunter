"""CLI: python -m normalizer <files-or-folders> [--inventory inventory.db]

Prints a JSON list on stdout, one entry per file. Exits 1 if any entry is an error.
"""
import argparse
import json
import sqlite3
import sys
from datetime import date
from decimal import Decimal

from . import inventory_from_db, normalize


def _json_default(v):
    """Serialize the types json.dumps can't handle, for the CLI output.

    Args:
        v: A value json.dumps couldn't serialize.

    Returns:
        A Decimal as a string (keeps it exact for whoever reads the JSON); a date in ISO format.

    Raises:
        TypeError: Any other type.
    """
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, date):
        return v.isoformat()
    raise TypeError(f"Not JSON serializable: {type(v).__name__}")


def main(argv: list[str] | None = None) -> int:
    """Run the CLI.

    Args:
        argv: Command-line arguments; None reads sys.argv.

    Returns:
        The exit code: 1 if any entry is an error (including an unreadable inventory DB), else 0.
    """
    ap = argparse.ArgumentParser(
        prog="python -m normalizer",
        description="Normalize invoice files into the standard Invoice format (JSON on stdout).")
    ap.add_argument("paths", nargs="+", help="invoice files and/or directories")
    ap.add_argument("--inventory", help="SQLite inventory DB; enables unknown-product and stock checks")
    args = ap.parse_args(argv)
    try:
        inventory = inventory_from_db(args.inventory) if args.inventory else None
    except sqlite3.Error as e:
        # Same shape as a file error, so an agent always gets JSON on stdout.
        print(json.dumps([{"file": args.inventory, "error": f"{type(e).__name__}: {e}"}], indent=2))
        return 1
    results = normalize(args.paths, inventory)
    print(json.dumps(results, indent=2, default=_json_default))
    return 1 if any("error" in r for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
