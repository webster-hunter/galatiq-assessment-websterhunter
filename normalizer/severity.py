"""First-pass severity for each flag code.

This is a grade, not a decision: the approval policy reads the severities (and the flags
themselves) to decide pay, hold or reject.
"""

# Most to least serious.
SEVERITIES = ("BLOCK", "REVIEW", "WARN", "INFO")
#   BLOCK   cannot be paid as submitted
#   REVIEW  a person must decide
#   WARN    payable, but recorded
#   INFO    a note for the record; not a problem and never a reason to hold

SEVERITY = {
    # Can't be paid as submitted.
    "QTY_MISSING": "BLOCK",
    "QTY_NON_POSITIVE": "BLOCK",
    "PRICE_MISSING": "BLOCK",
    "PRICE_NEGATIVE": "BLOCK",
    "ITEM_MISSING": "BLOCK",
    "LINE_ITEMS_MISSING": "BLOCK",  # a bill for nothing
    "VENDOR_MISSING": "BLOCK",
    "TOTAL_MISSING": "BLOCK",  # no amount to pay
    "TOTAL_NEGATIVE": "BLOCK",
    "SHIPPING_NEGATIVE": "BLOCK",
    "TAX_NEGATIVE": "BLOCK",  # negative tax would offset inflated lines
    "CONFLICTING_ROWS": "BLOCK",  # CSV rows disagree on invoice number, vendor, ...: more than one invoice
    "LINE_AMOUNT_MISMATCH": "BLOCK",
    "SUBTOTAL_MISMATCH": "BLOCK",
    "TAX_AMOUNT_MISMATCH": "BLOCK",
    "TOTAL_MISMATCH": "BLOCK",
    # A person must decide.
    "UNPARSEABLE": "REVIEW",  # value couldn't be read; the flag carries the raw value
    "UNKNOWN_PRODUCT": "REVIEW",
    "QTY_NOT_WHOLE": "REVIEW",  # stock is counted in whole units
    "DUE_DATE_MISSING": "REVIEW",  # can be looked up
    "DUE_DATE_BEFORE_INVOICE_DATE": "REVIEW",  # data error, or pressure to pay at once
    "INVOICE_NUMBER_MISSING": "REVIEW",  # payable in principle, but duplicates can't be caught
    "OVER_APPROVAL_THRESHOLD": "REVIEW",  # the brief: over $10K needs additional scrutiny
    "CURRENCY_UNSUPPORTED": "REVIEW",  # no conversion: hold rather than compare EUR to USD
    "STOCK_EXCEEDED": "REVIEW",
    "STOCK_UNKNOWN": "REVIEW",
    # From LLM extraction: the model returned a value the document doesn't contain.
    "NOT_IN_SOURCE": "REVIEW",
    "INVENTORY_NOT_CHECKED": "REVIEW",  # no inventory given, so products and stock are unverified
    # Payable, but recorded.
    "DUPLICATE_ITEM": "WARN",  # often legitimate (discount or rush lines on 1013, 1010)
    "UNEXPECTED_FORMAT": "WARN",  # read fine, but not in the usual form ("1002", "Widget A", "Immediate")
    "UNEXPECTED_FIELDS": "WARN",  # unmapped data could matter (a PO number, a bank change)
    # For the record only.
    "CURRENCY_ASSUMED": "INFO",  # no currency named, so read as USD
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
