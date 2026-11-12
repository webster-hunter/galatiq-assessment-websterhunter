"""Start the review screen:  python -m web   then open http://127.0.0.1:8000"""
import argparse

import uvicorn

from . import create_app


def main() -> None:
    ap = argparse.ArgumentParser(description="Serve the invoice review screen on this machine.")
    ap.add_argument("--ledger", default="ledger.db", help="the ledger main.py used (default: ledger.db)")
    ap.add_argument("--checkpoints", default="checkpoints.db", help="the saved runs main.py used (default: checkpoints.db)")
    ap.add_argument("--rates", default="rates.db", help="the exchange-rate table (default: rates.db)")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    # 127.0.0.1 only: there is no login, so the screen is not offered to the network.
    uvicorn.run(create_app(args.ledger, args.checkpoints, args.rates), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
