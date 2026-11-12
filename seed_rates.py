"""Create rates.db with made-up exchange rates for the sample dates. Safe to re-run.

These are NOT real market rates. The brief rules out internet access, so the rate service is a
local mock (see rates/), and this is its data, as seed_inventory.py is the inventory's.

Usage: python seed_rates.py [db_path]   (default: rates.db in the current directory)
"""
import sys

from rates import seed

if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "rates.db"
    seed(path)
    print(f"Seeded {path}")
