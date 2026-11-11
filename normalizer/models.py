"""The standard invoice structure every source format is converted into."""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass
class LineItem:
    """One line of an invoice. Values are as stated in the source, typed; None if missing or unreadable."""
    item: str | None
    quantity: Decimal | None
    unit_price: Decimal | None
    amount: Decimal | None = None  # stated line total, if the invoice gives one
    note: str | None = None


@dataclass
class Invoice:
    """A normalized invoice. Money is Decimal, dates are date; None means missing or unreadable."""
    invoice_number: str | None
    vendor_name: str | None
    vendor_address: str | None
    date: date | None
    due_date: date | None
    line_items: list[LineItem]
    subtotal: Decimal | None
    tax_rate: Decimal | None
    tax_amount: Decimal | None
    shipping: Decimal | None  # added after tax (1010: 6,700 + 335 tax + 150 = 7,185)
    total: Decimal | None
    currency: str | None
    payment_terms: str | None
    revision: str | None = None
    notes: str | None = None
    source_file: str | None = None
    # Source fields the loader doesn't map, keyed by path ("vendor.tax_id", "line_items[2].discount").
    # Kept verbatim for the agent to review: nothing in the file is silently dropped.
    unexpected_fields: dict = field(default_factory=dict)
    # Issues for validation to judge, each with a severity, e.g.
    # {"code": "DUPLICATE_ITEM", "severity": "WARN", "item": "WidgetA", "lines": [0, 3]}.
    flags: list[dict] = field(default_factory=list)
