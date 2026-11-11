"""First-pass severity for each flag code.

This is a grade, not a decision: the approval policy reads the severities (and the flags
themselves) to decide pay, hold or reject.
"""

# Most to least serious.
SEVERITIES = ("BLOCK", "REVIEW", "WARN", "INFO")
#   BLOCK   cannot be paid as submitted
#   REVIEW  a person must decide
#   WARN    payable, but recorded
#   INFO    normalization note (no flag uses it yet)

SEVERITY = {
    # Can't be paid as submitted.
    "QTY_MISSING": "BLOCK",
    "QTY_NON_POSITIVE": "BLOCK",
    "PRICE_MISSING": "BLOCK",
    "PRICE_NEGATIVE": "BLOCK",
    "ITEM_MISSING": "BLOCK",
    "LINE_ITEMS_MISSING": "BLOCK",  # a bill for nothing
    "VENDOR_MISSING": "BLOCK",
    "TOTAL_NEGATIVE": "BLOCK",
    "SUBTOTAL_MISMATCH": "BLOCK",
    "TAX_AMOUNT_MISMATCH": "BLOCK",
    "TOTAL_MISMATCH": "BLOCK",
    # A person must decide.
    "UNPARSEABLE": "REVIEW",  # value couldn't be read; the flag carries the raw value
    "UNKNOWN_PRODUCT": "REVIEW",
    "QTY_NOT_WHOLE": "REVIEW",  # stock is counted in whole units
    "DUE_DATE_MISSING": "REVIEW",  # can be looked up
    "STOCK_EXCEEDED": "REVIEW",
    "STOCK_UNKNOWN": "REVIEW",
    # Payable, but recorded.
    "DUPLICATE_ITEM": "WARN",  # often legitimate (discount or rush lines on 1013, 1010)
    "UNEXPECTED_FIELDS": "WARN",  # unmapped data could matter (a PO number, a bank change)
}


def severity(flag: dict) -> str:
    """Look up the severity for one flag.

    Args:
        flag: A flag dict with a "code".

    Returns:
        One of SEVERITIES.

    Raises:
        KeyError: The code has no entry in SEVERITY. Deliberate: a new flag must choose one.
    """
    return SEVERITY[flag["code"]]
