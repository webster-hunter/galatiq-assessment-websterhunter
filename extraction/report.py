"""Does self-correction help? Run every invoice twice and compare.

    python -m extraction.report data/invoices --inventory inventory.db

    without   one model attempt, no repairs: what the first transcription and the checks give
    with      the retry loop, OCR repair and item-name matching, as main.py runs it

For each file it reports the model attempts used, the repairs applied, and the flags that would
hold the invoice (BLOCK or REVIEW) before and after. Uses the recordings in data/replays, so it
needs no API key and gives the same numbers every time.

"Better" here means fewer holding flags. That is a proxy: it shows the loop removed problems
that were the model's or the scanner's, not that the final values are right. The recorded-sample
tests check those values against the documents.
"""
import argparse
import sqlite3
import sys

import normalizer
from normalizer.parsing import LOADERS

from . import MAX_ATTEMPTS, TEXT_SUFFIXES, ProviderError, Replay, extract

HOLDING = ("BLOCK", "REVIEW")
_ORDER = ("BLOCK", "REVIEW", "WARN", "INFO", "NONE")


def _run(path, provider, inventory, self_correct: bool):
    if path.suffix.lower() in TEXT_SUFFIXES:
        inv = extract(path, provider, max_attempts=MAX_ATTEMPTS if self_correct else 1)
    else:
        inv = LOADERS[path.suffix.lower()](path)
    return normalizer.check(inv, inventory, self_correct=self_correct)


def _worst(inv) -> str:
    return min((f["severity"] for f in inv.flags), key=_ORDER.index, default="NONE")


def _holding(inv) -> list[str]:
    return [f["code"] for f in inv.flags if f["severity"] in HOLDING]


def measure(paths, provider, inventory=None) -> list[dict]:
    """Compare each invoice with and without self-correction.

    Args:
        paths: File and folder paths (as normalize takes them).
        provider: The model provider; Replay(folder) measures the recorded replies.
        inventory: Item name -> stock on hand.

    Returns:
        One dict per file: file, attempts (model calls with self-correction; 0 for a parsed file),
        retry (RESOLVED, UNRESOLVED or None), ocr_repairs, name_repairs, before and after (each
        {"worst", "holding"}), and change: BETTER, WORSE or SAME by the number of holding flags.
        A file that can't be loaded has file and error only.
    """
    rows = []
    for p in normalizer.expand(paths, LOADERS.keys() | TEXT_SUFFIXES):
        try:
            before, after = _run(p, provider, inventory, False), _run(p, provider, inventory, True)
        except (*normalizer.LOAD_ERRORS, ProviderError) as e:
            rows.append({"file": p.name, "error": f"{type(e).__name__}: {e}"})
            continue
        codes = [f["code"] for f in after.flags]
        retried = next((f for f in after.flags if f["code"] == "EXTRACTION_RETRIED"), None)
        b, a = _holding(before), _holding(after)
        rows.append({
            "file": p.name,
            "attempts": len(after.extraction_attempts),
            "retry": retried["outcome"] if retried else None,
            "ocr_repairs": codes.count("OCR_REPAIRED"),
            "name_repairs": codes.count("ITEM_NAME_REPAIRED"),
            "before": {"worst": _worst(before), "holding": b},
            "after": {"worst": _worst(after), "holding": a},
            "change": "BETTER" if len(a) < len(b) else "WORSE" if len(a) > len(b) else "SAME",
        })
    return rows


def summarize(rows: list[dict]) -> dict:
    """Totals across measure()'s rows: files by change, model calls with and without, repairs, files released from holding."""
    ok = [r for r in rows if "error" not in r]
    text = [r for r in ok if r["attempts"]]
    return {
        "files": len(ok), "errors": len(rows) - len(ok),
        "better": sum(r["change"] == "BETTER" for r in ok),
        "worse": sum(r["change"] == "WORSE" for r in ok),
        "same": sum(r["change"] == "SAME" for r in ok),
        "model_calls_without": len(text), "model_calls_with": sum(r["attempts"] for r in text),
        "retries_resolved": sum(r["retry"] == "RESOLVED" for r in ok),
        "retries_unresolved": sum(r["retry"] == "UNRESOLVED" for r in ok),
        "ocr_repairs": sum(r["ocr_repairs"] for r in ok), "name_repairs": sum(r["name_repairs"] for r in ok),
        "holding_flags_before": sum(len(r["before"]["holding"]) for r in ok),
        "holding_flags_after": sum(len(r["after"]["holding"]) for r in ok),
        "no_longer_held": [r["file"] for r in ok if r["before"]["holding"] and not r["after"]["holding"]],
    }


def render(rows: list[dict]) -> str:
    """The comparison as a plain-text table with a summary underneath."""
    out = [f"{'file':<26}{'attempts':>9}{'retry':>12}{'ocr':>5}{'names':>7}   {'holding flags':<16}{'worst flag':<20}change",
           "-" * 103]
    for r in rows:
        if "error" in r:
            out.append(f"{r['file']:<26}{r['error']}")
            continue
        b, a = r["before"], r["after"]
        holding = f"{len(b['holding'])} -> {len(a['holding'])}"
        worst = b["worst"] if b["worst"] == a["worst"] else f"{b['worst']} -> {a['worst']}"
        out.append(f"{r['file']:<26}{r['attempts'] or '-':>9}{r['retry'] or '-':>12}{r['ocr_repairs'] or '-':>5}"
                   f"{r['name_repairs'] or '-':>7}   {holding:<16}{worst:<20}{'' if r['change'] == 'SAME' else r['change']}")
    s = summarize(rows)
    out += ["",
            f"{s['files']} files: {s['better']} better, {s['worse']} worse, {s['same']} unchanged"
            + (f", {s['errors']} could not be loaded" if s["errors"] else ""),
            f"Holding flags (BLOCK or REVIEW): {s['holding_flags_before']} without self-correction, {s['holding_flags_after']} with",
            f"Model calls: {s['model_calls_without']} without, {s['model_calls_with']} with "
            f"({s['retries_resolved']} retry resolved, {s['retries_unresolved']} unresolved)",
            f"Repairs: {s['ocr_repairs']} OCR values, {s['name_repairs']} item names",
            "No longer held: " + (", ".join(s["no_longer_held"]) or "none")]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m extraction.report",
                                 description="Compare each invoice with and without self-correction.")
    ap.add_argument("paths", nargs="+", help="invoice files and/or directories")
    ap.add_argument("--inventory", default="inventory.db", help="SQLite inventory DB (default: inventory.db)")
    ap.add_argument("--replays", default="data/replays", help="folder of recorded model replies")
    args = ap.parse_args(argv)
    try:
        inventory = normalizer.inventory_from_db(args.inventory)
    except sqlite3.Error as e:
        print(f"Can't read the inventory {args.inventory}: {e}. Run: python seed_inventory.py", file=sys.stderr)
        return 1
    rows = measure(args.paths, Replay(args.replays), inventory)
    print(render(rows))
    return 1 if any("error" in r for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
