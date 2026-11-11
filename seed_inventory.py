"""Create inventory.db from the README seed data. Safe to re-run: it resets to the seed values.

Usage: python seed_inventory.py [db_path]   (default: inventory.db in the current directory)
"""
import sqlite3
import sys
from contextlib import closing

SEED = [("WidgetA", 15), ("WidgetB", 10), ("GadgetX", 5), ("FakeItem", 0)]


def seed(db_path: str = "inventory.db") -> None:
    """Create or reset the inventory table with the README seed data.

    Safe to re-run: seed items are reset to their seed stock, and other rows are left alone.

    Args:
        db_path: Path to the SQLite database; created if missing.

    Returns:
        None.
    """
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS inventory (item TEXT PRIMARY KEY, stock INTEGER)")
        # OR REPLACE: the README's plain INSERT fails on a second run (primary key clash).
        conn.executemany("INSERT OR REPLACE INTO inventory VALUES (?, ?)", SEED)


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "inventory.db"
    seed(path)
    print(f"Seeded {path}")
