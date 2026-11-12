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
    "PRODUCT_NOT_STOCKED": "BLOCK",  # an unknown product whose name resembles nothing in inventory: not a misspelling
    # A person must decide.
    # A file no parser knows how to read (an unknown type such as .dat, or a JSON, CSV or XML file
    # in an unknown layout), read by the model instead. Held so a person sees the first invoices
    # in a new format; change to WARN to let clean ones through.
    "UNFAMILIAR_FORMAT": "REVIEW",
    # Read by the model from a file (a PDF, a text file, an unfamiliar format) that names other invoice
    # numbers too: it may hold several invoices, of which only one was read. Held, so it is never paid unseen.
    "POSSIBLY_SEVERAL_INVOICES": "REVIEW",
    "UNKNOWN_PRODUCT": "REVIEW",  # may be a misspelling of a real product, or something we don't stock
    "UNPARSEABLE": "REVIEW",  # value couldn't be read; the flag carries the raw value
    "QTY_NOT_WHOLE": "REVIEW",  # stock is counted in whole units
    "DUE_DATE_MISSING": "REVIEW",  # can be looked up
    "DUE_DATE_BEFORE_INVOICE_DATE": "REVIEW",  # data error, or pressure to pay at once
    "INVOICE_NUMBER_MISSING": "REVIEW",  # payable in principle, but duplicates can't be caught
    "OVER_APPROVAL_THRESHOLD": "REVIEW",  # the brief: over $10K needs additional scrutiny
    "CURRENCY_UNSUPPORTED": "REVIEW",  # another currency and no usable rate: can't be weighed in dollars
    # Another currency, with a dollar estimate from the rate service. Held so a person sees each
    # foreign-currency payment; change to WARN to let clean ones through on the estimate.
    "CURRENCY_CONVERTED": "REVIEW",
    "STOCK_EXCEEDED": "REVIEW",
    "STOCK_UNKNOWN": "REVIEW",
    # From LLM extraction: the model returned a value the document doesn't contain.
    "NOT_IN_SOURCE": "REVIEW",
    "INVENTORY_NOT_CHECKED": "REVIEW",
    # From the ledger, which compares an invoice with those already processed.
    "REVISION_OF_PAID": "REVIEW",  # revises an invoice that was already paid: a person settles the difference
    "DUPLICATE_CONFLICT": "REVIEW",
    "POSSIBLE_DUPLICATE": "REVIEW",  # same number and content as an invoice on record under another vendor name  # same vendor, number and revision as an earlier invoice, different content  # no inventory given, so products and stock are unverified
    # Payable, but recorded.
    "DUPLICATE_ITEM": "WARN",  # often legitimate (discount or rush lines on 1013, 1010)
    # A value damaged by OCR, read and confirmed by an independent value on the invoice. Change to
    # REVIEW to have a person look at every repaired invoice.
    "OCR_REPAIRED": "WARN",
    "ITEM_NAME_REPAIRED": "WARN",  # "Widget A" matched to the inventory's WidgetA
    "NEAR_THRESHOLD": "WARN",  # just under the approval limit; the policy weighs it with other flags
    "PRESSURE_LANGUAGE": "WARN",  # "URGENT", "pay immediately", "penalties"
    "WIRE_REQUEST": "WARN",  # asks for a wire or names a changed account
    "UNEXPECTED_FORMAT": "WARN",  # read fine, but not in the usual form ("1002", "Widget A", "Immediate")
    "UNEXPECTED_FIELDS": "WARN",  # unmapped data could matter (a PO number, a bank change)
    # For the record only.
    "CURRENCY_ASSUMED": "INFO",  # no currency named, so read as USD
    "SUPERSEDES_UNPAID": "INFO",  # replaces an earlier revision that had not been paid
    "EXTRACTION_RETRIED": "INFO",  # LLM extraction needed more than one attempt; the flag says how it ended
}


# What each flag means, in a sentence a person (or a model) can read. A flag records what a check
# noticed. It is not always a finding of fault: UNEXPECTED_FIELDS means the file had data with no
# place in the invoice structure, not that the data is suspicious. Sent with the flags to the
# reviewer agent and its critic, which otherwise guess at a code's meaning from its name.
MEANING = {
    "QTY_MISSING": "a line has no quantity",
    "QTY_NON_POSITIVE": "a line's quantity is zero or negative",
    "QTY_NOT_WHOLE": "a line's quantity is not a whole number",
    "PRICE_MISSING": "a line has no unit price",
    "PRICE_NEGATIVE": "a line's unit price is negative",
    "ITEM_MISSING": "a line has no product name",
    "LINE_ITEMS_MISSING": "the invoice has no line items",
    "VENDOR_MISSING": "the invoice names no vendor",
    "INVOICE_NUMBER_MISSING": "the invoice has no invoice number",
    "TOTAL_MISSING": "the invoice states no total",
    "TOTAL_NEGATIVE": "the stated total is below zero",
    "SHIPPING_NEGATIVE": "the stated shipping charge is below zero",
    "TAX_NEGATIVE": "the stated tax amount or rate is below zero",
    "CONFLICTING_ROWS": "rows of a CSV disagree on an invoice-level value such as the vendor or invoice number",
    "LINE_AMOUNT_MISMATCH": "a line's stated amount is not its quantity times its unit price",
    "SUBTOTAL_MISMATCH": "the stated subtotal is not the sum of the lines",
    "TAX_AMOUNT_MISMATCH": "the stated tax is not the subtotal times the stated rate",
    "TOTAL_MISMATCH": "the stated total is not the lines plus tax and shipping",
    "UNPARSEABLE": "a value could not be read as the kind of value its field needs; `value` is the text as written",
    "UNKNOWN_PRODUCT": "a line's product name is not in inventory",
    "POSSIBLY_SEVERAL_INVOICES": "the model read one invoice (`read`) from a file that also names the invoice numbers in "
                                 "`also_in_file`; the file may hold several invoices, and the others were not read",
    "UNFAMILIAR_FORMAT": "a file no parser can read: an unknown file type, or a JSON, CSV or XML file whose field names "
                         "are not the usual ones; `read_by` says whether the model read it instead (its values were then "
                         "checked against the file like any transcription)",
    "PRODUCT_NOT_STOCKED": "a line's product name is not in inventory and resembles no product that is",
    "DUE_DATE_MISSING": "the invoice states no due date",
    "DUE_DATE_BEFORE_INVOICE_DATE": "the due date is earlier than the invoice date",
    "OVER_APPROVAL_THRESHOLD": "the total, in dollars, is at or above the amount that requires a person's review",
    "CURRENCY_UNSUPPORTED": "the invoice is in another currency and no usable exchange rate was found",
    "CURRENCY_CONVERTED": "the invoice is in another currency; `usd_estimate` is its estimated cost in dollars",
    "STOCK_EXCEEDED": "the quantity billed for a product, summed over its lines, is more than is in stock",
    "STOCK_UNKNOWN": "inventory has no usable stock figure for a product",
    "NOT_IN_SOURCE": "a value the model transcribed does not appear in the document",
    "INVENTORY_NOT_CHECKED": "no inventory was available, so products and stock were not checked",
    "REVISION_OF_PAID": "this revises an invoice that was already paid; `already_paid` and `difference` say how much",
    "DUPLICATE_CONFLICT": "an invoice with the same vendor, number and revision is on record with different content",
    "POSSIBLE_DUPLICATE": "an invoice with the same number and content is on record under a different vendor name",
    "OCR_REPAIRED": "a value damaged in scanning was read and confirmed by another value on the invoice; `proof` says how",
    "ITEM_NAME_REPAIRED": "a product name was matched to an inventory name that differs only in spacing or case",
    "NEAR_THRESHOLD": "the total is just under the amount that requires a person's review",
    "PRESSURE_LANGUAGE": "the invoice's text pushes for fast payment; `matched` lists the words",
    "WIRE_REQUEST": "the invoice's text asks for a wire or bank transfer, or names a changed account",
    "UNEXPECTED_FORMAT": "a value is not written in its field's usual form; it was still read as written",
    "DUPLICATE_ITEM": "the same product is on more than one line; often legitimate (a discount or rush line)",
    "UNEXPECTED_FIELDS": "the file holds data with no place in the invoice structure (a Bill To line, a PO number); "
                         "this is not a fault in itself",
    "CURRENCY_ASSUMED": "the invoice names no currency, so it was read as US dollars",
    "SUPERSEDES_UNPAID": "this revision replaces an earlier one that had not been paid",
    "EXTRACTION_RETRIED": "transcribing the document took more than one model attempt",
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
