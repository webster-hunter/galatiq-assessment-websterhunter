"""Deterministic invoice normalizer: files in, standard Invoices with graded flags out.

    parsing.py   file formats -> Invoice (only UNPARSEABLE flags)
    repair.py    OCR-damaged values and item-name variants, read only where there is proof
    checks.py    format-agnostic checks -> flags with evidence
    severity.py  flag code -> BLOCK / REVIEW / WARN / INFO
    this file    load, expand, normalize: the three wired together

Agents call normalize() from Python, or the CLI: python -m normalizer --help
"""
import csv
from dataclasses import asdict
from pathlib import Path
from xml.parsers.expat import ExpatError

from .checks import flag, inventory_from_db
from .models import Invoice, LineItem
from .parsing import LOADERS
from .repair import repair, repair_item_names
from .severity import SEVERITIES, SEVERITY, severity

__all__ = ["Invoice", "LineItem", "SEVERITIES", "SEVERITY", "check", "expand", "flag", "inventory_from_db",
           "load", "normalize", "repair", "severity"]


# What an unreadable or malformed file raises. ValueError covers invalid JSON, wrong encoding, an
# unsupported extension and the loaders' own refusals. Anything else is a bug in this package and
# is left to propagate with its traceback, not reported as if the vendor's file were bad.
LOAD_ERRORS = (OSError, ValueError, ExpatError, csv.Error)


def load(path: str | Path, inventory: dict[str, int] | None = None) -> Invoice:
    """Load one invoice file, run every check, and grade each flag's severity.

    Args:
        path: An invoice file with a supported extension (see parsing.LOADERS).
        inventory: Item name -> stock on hand. None skips the unknown-product and stock checks.

    Returns:
        The normalized Invoice with all flags, each carrying a severity.

    Raises:
        ValueError: No loader for the file's extension.
        Whatever the format's loader raises when the file itself can't be read.
    """
    suffix = Path(path).suffix.lower()
    if suffix not in LOADERS:
        raise ValueError(f"No loader for {suffix} files yet: {path}")
    return check(LOADERS[suffix](path), inventory)


def check(inv: Invoice, inventory: dict[str, int] | None = None, self_correct: bool = True) -> Invoice:
    """Repair what the invoice proves, run every check on a built Invoice, and grade each flag. Shared by every loader, LLM extraction included.

    Args:
        inv: An Invoice straight from a loader (only UNPARSEABLE or CONFLICTING_ROWS flags so far).
        inventory: Item name -> stock on hand. None skips the unknown-product and stock checks.
        self_correct: False skips the repairs, to measure what they change (see extraction.report).

    Returns:
        The same Invoice, with the check flags added and a severity on every flag.
    """
    if self_correct:
        repair(inv)  # before the checks, so a repaired value is checked like any other
        if inventory is not None:
            repair_item_names(inv, inventory)
    inv.flags += flag(inv, inventory)  # here, not in each loader, so every format gets the same checks
    # Severity is set once, here, so parse-time and check-time flags are graded by the same table.
    inv.flags = [{"code": f["code"], "severity": severity(f), **f} for f in inv.flags]
    return inv


def expand(paths, suffixes=LOADERS) -> list[Path]:
    """Turn a mix of files and folders into the list of files to load.

    Args:
        paths: File and folder paths, as strings or Paths.
        suffixes: Which extensions to take from a folder (lowercase, with the dot).

    Returns:
        Files as given, in order; each folder replaced by its supported files, sorted by name
        (not recursive).
    """
    out = []
    for p in map(Path, paths):
        if p.is_dir():
            out += sorted(f for f in p.iterdir() if f.suffix.lower() in suffixes)
        else:
            out.append(p)
    return out


def normalize(paths, inventory: dict[str, int] | None = None) -> list[dict]:
    """Load every invoice in paths. This is the entry point agents call from Python.

    A file that can't be loaded (see LOAD_ERRORS) becomes an error entry instead of stopping the
    batch. Any other exception is a bug here, and propagates.

    Args:
        paths: File and folder paths (see expand).
        inventory: Item name -> stock on hand. None skips the unknown-product and stock checks.

    Returns:
        One dict per file: {"file", "invoice"} with the invoice as plain dicts, or
        {"file", "error"} with the error type and message.
    """
    results = []
    for p in expand(paths):
        try:
            results.append({"file": str(p), "invoice": asdict(load(p, inventory))})
        except LOAD_ERRORS as e:
            results.append({"file": str(p), "error": f"{type(e).__name__}: {e}"})
    return results
