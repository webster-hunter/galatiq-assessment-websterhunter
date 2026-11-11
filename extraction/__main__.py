"""CLI: python -m extraction <files-or-folders> [--inventory inventory.db] [--model M] [--offline]

Like python -m normalizer, but TXT and PDF files are transcribed by the model. Every model reply is
recorded under --replays. XAI_API_KEY and LLM_MODEL come from the environment or a .env
file (see .env.example). With no API key (or --offline) only recordings are used, so a reviewer
can run it without a key. Prints a JSON list on stdout; exits 1 if any entry is an error.
"""
import argparse
import json
import os
import sqlite3
import sys

try:
    from dotenv import load_dotenv
except ImportError:  # optional: without it, set XAI_API_KEY and LLM_MODEL in the environment
    def load_dotenv():
        return False

from normalizer import inventory_from_db
from normalizer.__main__ import _json_default

from . import Grok, Replay, normalize


def main(argv: list[str] | None = None) -> int:
    # Here, not in the library: only the CLI reads a .env. Variables already set win over the file.
    load_dotenv()
    ap = argparse.ArgumentParser(prog="python -m extraction",
                                 description="Normalize invoices, transcribing TXT and PDF with an LLM.")
    ap.add_argument("paths", nargs="+", help="invoice files and/or directories")
    ap.add_argument("--inventory", help="SQLite inventory DB; enables unknown-product and stock checks")
    ap.add_argument("--model", default=os.environ.get("LLM_MODEL", "grok-4"))
    ap.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", "https://api.x.ai/v1"),
                    help="xAI API root")
    ap.add_argument("--replays", default="data/replays", help="folder of recorded model replies")
    ap.add_argument("--offline", action="store_true", help="use recordings only, never call the API")
    args = ap.parse_args(argv)
    try:
        inventory = inventory_from_db(args.inventory) if args.inventory else None
    except sqlite3.Error as e:
        print(json.dumps([{"file": args.inventory, "error": f"{type(e).__name__}: {e}"}], indent=2))
        return 1
    live = None
    if not args.offline and os.environ.get("XAI_API_KEY"):
        live = Grok(args.model, args.base_url)
    results = normalize(args.paths, Replay(args.replays, live), inventory)
    print(json.dumps(results, indent=2, default=_json_default))
    return 1 if any("error" in r for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
