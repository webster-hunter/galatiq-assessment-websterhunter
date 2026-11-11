# Galatiq Case: Invoice Processing Automation

## Background

Acme Corp is a PE-backed manufacturing firm losing **$2M/year** on manual invoice processing. Invoices arrive via email as PDFs in messy formats with frequent errors. Staff manually extract data, validate against a legacy inventory database (inconsistent), obtain VP approval (via email chains), and process payment (via a banking API).

**Current pain points:**
- 30% error rate
- 5-day processing delays
- Frustrated stakeholders

## Objective

Build a **multi-agent system** that automates the end-to-end invoice processing workflow. The system must run as a working prototype — not just designs or slides.

## Workflow

The system should handle four stages:

1. **Ingestion** — Extract structured data from invoice documents (PDFs, text files). Fields include: Vendor, Amount, Items (with quantities), and Due Date. Expect unstructured text, typos, missing data, and potentially fraudulent entries.

2. **Validation** — Verify extracted data against a mock inventory database (SQLite). Flag mismatches such as quantity exceeding available stock or items not found in inventory.

3. **Approval** — Simulate VP-level review with rule-based decision-making (e.g., invoices over $10K require additional scrutiny). The agent should reason through approval/rejection with a reflection or critique loop.

4. **Payment** — If approved, call a mock payment function. If rejected, log the rejection with reasoning.

## Technical Requirements

- **LLM Integration**: Use xAI's Grok as the core reasoning engine (via the xAI API at https://grok.x.ai). Other models are acceptable if you don't have an API key.
- **Multi-Agent Orchestration**: Use a framework such as LangGraph, CrewAI, AutoGen, or a custom solution.
- **Agent Capabilities**: Function calling / tool use, structured outputs, and self-correction loops.
- **Runtime**: Assume no internet for external APIs — simulate everything locally.
- **Tech Stack**: Python (preferred), with libraries like `langchain`, `crewai`, `autogen`, `pdfplumber`, `PyMuPDF`, etc. Run locally — no cloud deployment.

## Provided Resources

### Mock Invoice Data

Sample invoices are provided in the `data/invoices/` directory in various formats (PDF, CSV, JSON, TXT). Use these as inputs for testing. The data intentionally includes a mix of clean entries and problematic ones — identifying and handling issues is part of the challenge.

### Mock Inventory Database (Required Setup)

Before running the system, you **must** create a local SQLite database that the validation agent will check invoices against. The sample invoices in `data/invoices/` reference specific items and quantities — your database needs to contain matching inventory records so the validation stage can flag mismatches, out-of-stock items, and unknown products.

Below is a starter schema and seed data that covers the core items referenced across the provided invoices:

```python
import sqlite3

conn = sqlite3.connect('inventory.db')  # Persist to file so all agents can access it
cursor = conn.cursor()

cursor.execute('CREATE TABLE IF NOT EXISTS inventory (item TEXT PRIMARY KEY, stock INTEGER)')
cursor.execute("""
    INSERT INTO inventory VALUES
    ('WidgetA', 15),
    ('WidgetB', 10),
    ('GadgetX', 5),
    ('FakeItem', 0)
""")
conn.commit()
```

**Why this matters:** The sample invoices are designed to test your validation logic against this database. For example:

| Scenario | Invoice | What should happen |
|---|---|---|
| Normal order within stock | INV-1001, INV-1004, INV-1006 | Items found, quantities valid — passes validation |
| Quantity exceeds stock | INV-1002 (requests 20× GadgetX, only 5 in stock) | Flagged as stock mismatch |
| Fraudulent / zero-stock item | INV-1003 (references FakeItem, 0 stock) | Flagged as out of stock or suspicious |
| Item not in database at all | INV-1008 (SuperGizmo, MegaSprocket), INV-1016 (WidgetC) | Flagged as unknown item |
| Invalid data | INV-1009 (negative quantity) | Flagged as data integrity issue |

You may extend the seed data with additional items or columns (e.g., unit price, category) to support richer validation — the above is the minimum needed to exercise the provided test invoices. If you want your system to also validate pricing or vendor information, consider adding tables for those as well.

### Mock Payment API

```python
def mock_payment(vendor, amount):
    print(f"Paid {amount} to {vendor}")
    return {"status": "success"}
```

### Grok API Setup

```python
from xai import Grok

client = Grok(api_key="your_key")
response = client.chat.completions.create(
    model="grok-3",
    messages=[{"role": "user", "content": "Reason about this..."}]
)
```

## Running the System

The system should be executable from the command line:

```bash
python main.py --invoice_path=data/invoices/invoice1.txt
```

Output should include structured logs and results.

## Evaluation Criteria

- **Functionality** — Does the system work end-to-end?
- **Code Quality** — Clean, testable, well-structured code with error handling and observability
- **Agentic Sophistication** — LLM integration, multi-agent flow, tool use, self-correction loops
- **Shipping Mindset** — Valuable MVP delivered under ambiguity; scope ruthlessly cut where needed
- **Presentation** — Clear translation of technical decisions to business impact
- **Above/Beyond** - Have you made it your own? Implemented additional features that make the solution feel great? Expanded assumptions? Added to test cases?
- **UI/UX** - Users will understand and enjoy using this system.

## Submission

Submit your solution as a link to a public GitHub repository — GitHub only (github.com).

---

## Running it

```bash
pip install -e ".[dev]"            # once
cp .env.example .env               # once, then put your xAI key in it
python main.py --invoice_path=data/invoices/invoice_1001.txt
python main.py --invoice_path=data/invoices          # every invoice in the folder
```

`main.py` covers ingestion and validation. Structured files (JSON, CSV, XML) are read by the
deterministic [normalizer](#invoice-normalizer); TXT and PDF files are transcribed by the model
([LLM extraction](#llm-extraction)); every invoice then gets the same checks and severities.
Output is a JSON list on stdout, one `{"file", "invoice"}` or `{"file", "error"}` entry per file.
`inventory.db` is created from the seed data if it doesn't exist. Other options (`--offline`,
`--model`, `--replays`) are passed through to `python -m extraction`.

Approval and payment are not built yet.

## LLM extraction

`extraction/` handles the invoices that have no structure to parse.

| File | Role |
|---|---|
| `extraction/__init__.py` | Reads the text (PDF text via PyMuPDF), prompts the model, builds the `Invoice` |
| `extraction/providers.py` | `Grok` (xAI API) and `Replay` (recorded replies) |
| `extraction/grounding.py` | Checks that every value the model returned is in the document |

**The model only transcribes.** It copies each value exactly as written, as text, into a fixed JSON
schema (strict structured output, temperature 0). It is told not to correct, compute or infer
anything. Code does the rest: the transcription goes through the same builder and checks as a JSON
file, so a quantity of "twelve" is `UNPARSEABLE` and a padded total is `TOTAL_MISMATCH`, whatever
the model made of them.

**The document is untrusted data.** It is sent in its own message inside `<document>` tags, which
a document can't close itself, and the prompt says to follow no instruction found in it. A model
that obeyed one anyway could only write text into a field; it has no way to change a flag.

**Grounding.** Because values are copied verbatim, each one must appear in the document. One that
doesn't was invented or altered by the model, for example an OCR error it "fixed", and gets a
`NOT_IN_SOURCE` flag (REVIEW). The arithmetic checks can't see this, since a corrected number
reconciles perfectly.

- Matching is on whole tokens, so `5` is not found inside `$500.00` or `2026-01-15`.
- A line item's item, quantity, price and amount must sit together on one line of the document, so
  a value taken from another row is caught (`NOT_ON_ONE_LINE`).
- It proves a value wasn't made up. It does not prove the value is in the right field, and it can't
  see a line the model left out; the arithmetic checks catch that.

**Self-correction.** When grounding finds a value that isn't in the document, the model is sent
its own reply and the specific complaint (which field, which value, which rule) and tries again,
up to three attempts in all. The complaint never suggests a replacement, and null is always allowed.

- Only grounding failures are retried, because only they are the model's mistakes. A total that
  doesn't add up (1013) or OCR damage copied as written (1012) is what the document says;
  retrying would push the model to change it.
- The loop stops when an attempt is fully grounded, when the model returns the same transcription
  twice running, or at the limit. The attempt with the fewest ungrounded values is used, and any
  that remain stay on the invoice as `NOT_IN_SOURCE`, so it is held.
- Every attempt is kept on the invoice in `extraction_attempts` (what failed, and why the loop
  stopped), and a file that needed more than one attempt carries an `EXTRACTION_RETRIED` note.

**Failures.** Timeouts, rate limits (429) and server errors are retried, up to three attempts,
with exponential backoff and jitter; a `Retry-After` header is honoured. A bad key or bad request
fails at once. A truncated, refused, non-JSON or oddly shaped reply is an error entry for that one
file, and the batch continues.

**Recordings.** `Replay` saves each model reply under `data/replays`, keyed by a hash of the prompt
and document text, and replays it on the next run. With recordings committed, the pipeline runs
with no API key (`--offline`, or simply no key set).

- A changed prompt or document misses and is recorded again.
- The model isn't part of the key, so anyone can replay; with a key set, a recording made by a
  different model is recorded again.
- PyMuPDF is pinned to one version, because the key includes the PDF's extracted text.

Recordings for all ten TXT and PDF samples are committed (made with `grok-4`), so
`python main.py --invoice_path=data/invoices --offline` runs all 20 files with no key.
`tests/test_recordings.py` replays them and asserts each file's vendor, total and flags.

### Extraction results on the samples

From the recorded replies, with the seed inventory. None of these files names a currency, so each
also carries the `CURRENCY_ASSUMED` note (INFO), left out below:

| Invoice | Result |
|---|---|
| 1001, 1011 (txt and pdf) | Clean |
| 1002 | Total $15,000.00 read from "Amt"; `STOCK_EXCEEDED` GadgetX (20 / 5); `OVER_APPROVAL_THRESHOLD`; `UNEXPECTED_FORMAT` for the bare number `1002`. Took two attempts (below) |
| 1003 | Due date `yesterday` `UNPARSEABLE`; `STOCK_EXCEEDED` FakeItem (100 / 0); `OVER_APPROVAL_THRESHOLD`; `UNEXPECTED_FORMAT` for terms `Immediate`. The "URGENT" note is transcribed into `notes` and changes nothing |
| 1008 | Both items `UNKNOWN_PRODUCT`; the email's From, To and Subject kept in `unexpected_fields` |
| 1010 | Reconciles with shipping; the rush line is item WidgetA with note "rush order", so `DUPLICATE_ITEM`; vendor is Consolidated Materials Group, not the Bill To party |
| 1012 (txt and pdf) | OCR damage copied as written by the model, then read by code: `$3,500.O0` is confirmed by 7 × $500 and `26-Jan-2O26` by Net 30 to the due date (`OCR_REPAIRED` ×2); "Widget A" and "Gadget X" are `UNKNOWN_PRODUCT`; `UNEXPECTED_FORMAT` for `INV 1012` and both item names; vendor is the FROM party, with "formerly FastShip Ltd." kept apart in `unexpected_fields` |
| 1013 (pdf) | All eight lines; `STOCK_EXCEEDED` on all three items; `TOTAL_MISMATCH` (stated 22,562.80, computed 22,512.80); `OVER_APPROVAL_THRESHOLD` |

Every vendor and total is correct, and nine of the ten files were grounded on the first attempt.
1002 needed the retry: the model first copied the invoice total ("Amt: $15,000.00") onto the line
item as well, although the prompt says a line amount must be on the line's own row. Grounding
flagged it (`NOT_ON_ONE_LINE`), the complaint was sent back, and the second attempt returned the
line without an amount, fully grounded. Both replies are recorded as the model produced them.

The model's output for 1002 varied between recording runs even at temperature 0 (total missing,
correct, total duplicated), which is why the judge is code and not a re-reading by the model.

Not done yet: matching name variants such as
"Widget A", and detecting duplicates across files (1011, 1012 and 1013 each arrive twice).

## Invoice Normalizer

The `normalizer` package turns invoice files into one standard structure and flags problems for
later validation. It is deterministic: the same file always produces the same output, with no LLM
involved. Agents call it as a command-line tool, or from Python with
`from normalizer import normalize` and `normalize(paths, inventory)`.

| Module | Job |
|---|---|
| `normalizer/parsing.py` | File formats into the `Invoice` structure; only unreadable values are flagged here |
| `normalizer/repair.py` | Reads OCR-damaged values, only where the invoice proves the reading |
| `normalizer/checks.py` | Format-agnostic checks, each adding a flag with its evidence |
| `normalizer/severity.py` | One table from flag code to severity |
| `normalizer/__init__.py` | `load`, `expand` and `normalize`: the three wired together |
| `normalizer/__main__.py` | The command-line tool |

### Usage

Requires Python 3.10 or later. The normalizer uses only the standard library; pytest is the one
development dependency.

```bash
pip install -e ".[dev]"                            # once: installs the package and pytest
python seed_inventory.py                           # once: builds inventory.db from the seed data above
python -m normalizer data/invoices --inventory inventory.db
python -m normalizer data/invoices/invoice_1009.json data/invoices/invoice_1007.csv
pytest                                             # tests live in tests/
```

- **Input:** any mix of files and folders. A folder contributes every supported file in it.
  Supported today: `.json`, `.csv` (both CSV shapes in the sample data) and `.xml`.
- **`--inventory`:** optional SQLite database, opened read-only. It turns on the unknown-product and
  stock checks. Without it those checks can't run, and every invoice says so with an
  `INVENTORY_NOT_CHECKED` flag (REVIEW), so an unchecked invoice never looks like one that passed.
- **Output:** a JSON list on stdout, one entry per file: `{"file", "invoice"}` or `{"file", "error"}`.
  One bad file never stops the batch. Only file problems become error entries (unreadable,
  malformed, unsupported type); any other exception is a bug in the normalizer and is raised.
- **Exit code:** 1 if any entry is an error, otherwise 0.

### Normalization rules

Every format is converted into the same `Invoice` structure: invoice number, vendor name and address,
date, due date, line items (item, quantity, unit price, stated amount, note), subtotal, tax rate, tax
amount, shipping, total, currency, payment terms, revision, notes and source file.

- **Money is exact.** Amounts are `Decimal`, never float, and appear in the JSON output as strings
  (`"22562.80"`). `$1,250.00` and `-$250` are read as plain amounts. Only plain numbers are read:
  `1_000`, `1e3` and digits from other scripts are `UNPARSEABLE`, not guessed.
- **Dates** are read from an explicit list of the formats vendors use: `2026-01-24`, `01/28/2026`,
  `January 27, 2026`, `Jan 27, 2026` (each with or without the comma) and `25-Feb-2026`. Slash dates are US order, so `01/02/2026` is
  2 January. Any other text (`yesterday`) is `UNPARSEABLE`, never guessed. The list doesn't depend on
  the Python version, so a file parses the same everywhere; a new vendor format is one more entry in
  `_DATE_FORMATS`. Output format is ISO.
- **Blank means missing.** Empty strings, blank CSV cells and empty XML elements become `null`,
  never a default value, and don't take a field's place: a later non-blank value still lands.
- **Values are kept as stated, not corrected.** A quantity of −5 stays −5 and is flagged.
- **An unreadable value never costs the invoice.** It becomes `null` plus an `UNPARSEABLE` flag that
  carries the raw value, and the rest of the invoice still loads and is still checked. Only a file that
  can't be read at all (not found, invalid JSON, wrong encoding) becomes an error entry.
- **OCR damage is repaired only with proof.** OCR writes look-alike letters for digits (`O` for
  `0`, `l` or `I` for `1`), so `$3,500.O0` and `26-Jan-2O26` arrive unreadable. Code tries the
  look-alike readings and applies one only if another value on the invoice confirms it: a line
  amount by quantity × unit price, a quantity or price by the line amount, a subtotal or total
  by the lines, a date by the payment terms and the other date. A proven repair sets the value
  and replaces `UNPARSEABLE` with `OCR_REPAIRED` (WARN), keeping the original text and the proof.
  An unproven reading is never applied: the field stays empty and `UNPARSEABLE` (REVIEW), with
  the reading attached as `suggested`. A readable wrong number is not damage and is never changed.
  To send every repaired invoice to a person, set `OCR_REPAIRED` to `REVIEW` in `severity.py`.
- **Nothing is silently dropped.** Fields the loader doesn't recognise, repeated CSV columns or
  labels, and cells beyond a CSV's header all go to `unexpected_fields`, labelled by where they were
  found (`vendor.tax_id`, `line_items[1].qty`, `row[4].Handling:`, `row[2].extra_cells`). The first
  value always wins; a second `Total:` row never replaces the first, and a JSON key given twice keeps
  its first value (`repeated_key.total`). A repeat of the same path is kept as `path#2`, `path#3`.
- **CSV shapes.** `field,value` files (1006) start a new line item at each `item` row, so repeated
  keys keep every item. Row-per-item files (1007, 1015) take invoice fields from the first row and
  read totals from footer rows; `Tax (6%):` gives both the rate (0.06) and the amount. Common label
  variants map to the same fields: `Sales Tax` (and `Sales Tax (5%)`), `Shipping & Handling`,
  `Grand Total` and `Total Amount`.
- **Reading CSV rows.** In a row-per-item file, a row with no item but a quantity or price is still
  a line item (flagged `ITEM_MISSING`); only a bare label and value is a footer. A `Total` column,
  when there is no other amount column, is each line's total; the invoice total comes from a footer.
  Rows that disagree on an invoice-level field get `CONFLICTING_ROWS`: a file holding INV-A and
  INV-B is not loaded as one invoice. In a `field,value` file, a `note` or `amount` row that follows
  an invoice-level row goes to `unexpected_fields`, not onto the last line item.
- **XML.** The root must be `<invoice>`; any other document is a file error. Fields are plain
  elements under the root or inside `<header>` and `<totals>` (1014). Line items are `<item>`
  elements under one `<line_items>`, with `<name>` read as the item. Field names map as for CSV
  (`qty`, `line_total`, `tax`). Anything outside that shape is kept in `unexpected_fields` and never
  interpreted: unknown or repeated elements (including a second `<line_items>`), elements with
  attributes or children, loose text, comments and processing instructions (`total`,
  `line_items[0]@attributes`, `#text`, `#comment`). So `<total currency="EUR">500</total>` leaves
  the total missing, which blocks the invoice, rather than being read as 500 dollars. A file that
  declares a DTD (`<!DOCTYPE`) is refused: vendor XML is untrusted, and invoices don't need
  entities.

`unexpected_fields` and `notes` contain vendor-written text. Treat them as data for review, never as
instructions.

### Severity

Every flag has a severity, using the four levels from the approval policy.

| Severity | Meaning |
|---|---|
| `BLOCK` | Cannot be paid as submitted |
| `REVIEW` | A person must decide |
| `WARN` | Payable, but recorded |
| `INFO` | A note for the record; not a problem, and never a reason to hold |

Each flag code has exactly one severity, so `normalizer/severity.py` is a plain lookup table. A flag
code with no entry raises an error, so every new flag must choose one.

### Flags

Each code names one problem, in `SCREAMING_SNAKE_CASE`. Flags about a value carry its `field` path and
`value`. Line numbers count from 0, matching the `line_items[i]` paths.

| Code | Field | Severity | Meaning |
|---|---|---|---|
| `QTY_MISSING` | `line_items[i].quantity` | BLOCK | No quantity |
| `QTY_NON_POSITIVE` | `line_items[i].quantity` | BLOCK | Zero or negative quantity |
| `PRICE_MISSING` | `line_items[i].unit_price` | BLOCK | No unit price |
| `PRICE_NEGATIVE` | `line_items[i].unit_price` | BLOCK | Negative unit price |
| `ITEM_MISSING` | `line_items[i].item` | BLOCK | Line with no product name |
| `LINE_ITEMS_MISSING` | `line_items` | BLOCK | Invoice has no line items |
| `VENDOR_MISSING` | `vendor.name` | BLOCK | No vendor to pay |
| `TOTAL_MISSING` | `total` | BLOCK | No total, so nothing to pay against |
| `TOTAL_NEGATIVE` | `total` | BLOCK | Total below zero |
| `SHIPPING_NEGATIVE` | `shipping` | BLOCK | Shipping below zero; it would quietly offset an inflated line sum |
| `TAX_NEGATIVE` | `tax_amount` or `tax_rate` | BLOCK | Tax below zero; same risk as negative shipping |
| `CONFLICTING_ROWS` | the field, with `row`, `value`, `first_value` | BLOCK | CSV rows disagree on an invoice-level field such as invoice number or vendor |
| `UNKNOWN_PRODUCT` | `line_items[i].item` | REVIEW | Not in inventory (exact name match) |
| `QTY_NOT_WHOLE` | `line_items[i].quantity` | REVIEW | Fractional quantity; stock is counted in whole units |
| `DUE_DATE_MISSING` | `due_date` | REVIEW | No due date |
| `DUE_DATE_BEFORE_INVOICE_DATE` | `due_date` | REVIEW | Due date earlier than the invoice date (the flag carries both) |
| `INVOICE_NUMBER_MISSING` | `invoice_number` | REVIEW | No invoice number, so duplicates of this invoice can't be detected |
| `OVER_APPROVAL_THRESHOLD` | `total`, with `threshold` | REVIEW | A dollar total of $10,000 or more: the brief's "over $10K requires additional scrutiny", with exactly $10,000 included. Not applied to a non-USD total |
| `CURRENCY_UNSUPPORTED` | `currency` | REVIEW | A stated currency isn't USD; amounts can't be compared without a rate |
| `EXTRACTION_RETRIED` | `attempts`, `outcome` (`RESOLVED` or `UNRESOLVED`), `stopped` | INFO | LLM extraction only: the file needed more than one model attempt |
| `CURRENCY_ASSUMED` | `currency`, with `assumed` | INFO | No currency is named (or only `$`), so the invoice is read as USD. Recorded so the assumption is visible, not silent |
| `UNPARSEABLE` | any field | REVIEW | Value couldn't be read; `value` holds the raw input. `suggested` is there when OCR damage has one plausible reading that nothing on the invoice confirms |
| `LINE_AMOUNT_MISMATCH` | `line_items[i].amount` | BLOCK | A line's stated amount ≠ quantity × unit price |
| `SUBTOTAL_MISMATCH` | `subtotal` | BLOCK | Σ (quantity × unit price) ≠ stated subtotal |
| `TAX_AMOUNT_MISMATCH` | `tax_amount` | BLOCK | Stated subtotal × tax rate ≠ stated tax |
| `TOTAL_MISMATCH` | `total` | BLOCK | Line sum + tax + shipping ≠ stated total; `tax_basis` says which tax was used |
| `STOCK_EXCEEDED` | item | REVIEW | Quantity summed across all of the item's lines exceeds stock |
| `STOCK_UNKNOWN` | item | REVIEW | Inventory has no usable stock number for the item |
| `NOT_IN_SOURCE` | the field, or `line_items[i]` with reason `NOT_ON_ONE_LINE` | REVIEW | LLM extraction only: the model returned a value the document doesn't contain |
| `INVENTORY_NOT_CHECKED` | `skipped`: the checks that didn't run | REVIEW | No inventory was given, so products and stock are unverified |
| `DUPLICATE_ITEM` | item | WARN | Same item on more than one line (often legitimate, such as discount lines) |
| `OCR_REPAIRED` | the field, with `value` (original text), `repaired` and `proof` | WARN | An OCR-damaged value was read and confirmed by another value on the invoice |
| `UNEXPECTED_FORMAT` | the field, with `expected` (the pattern) | WARN | A value isn't in its field's usual form; it is still read as written. Checked: invoice number `INV-<digits>` (not `1002`, `INV 1012`), revision `R<digits>`, currency as a three-letter code (not `$`), payment terms `Net <days>` (not `Immediate`), item name as one word (not `Widget A`). Patterns are in `checks.FORMATS` |
| `UNEXPECTED_FIELDS` | `fields`: every path in `unexpected_fields` | WARN | The file holds data the loader doesn't map; read `unexpected_fields` |

Example flags:

```json
{"code": "STOCK_EXCEEDED", "severity": "REVIEW", "item": "WidgetA", "requested": "22", "available": 15, "lines": [0, 3, 6]}
{"code": "TOTAL_MISMATCH", "severity": "BLOCK", "field": "total", "stated": "15525.00", "computed": "15635.00", "tax_basis": "STATED", "shipping": null}
{"code": "UNPARSEABLE", "severity": "REVIEW", "field": "due_date", "value": "yesterday"}
```

Notes on the checks:

- **Stock is summed per item, not checked per line.** Every line of 1013 is within stock on its own,
  but together they need 22 WidgetA against 15. Invalid quantities aren't counted toward the total.
- **Arithmetic is recomputed, never trusted.** Recomputed values are rounded to the cent (half up)
  before comparing. A difference that rounding alone can explain is not flagged: one cent on a line
  amount or the tax, and half a cent per line on a sum of lines (a vendor who rounds each line
  before adding). 1007's $110 and 1013's $50 are far outside that. The total is checked against the line items rather than the stated subtotal, so
  one wrong number gives one flag: 1009 gets only `SUBTOTAL_MISMATCH`. When no tax amount is
  stated, the total check uses the tax rate if there is one, otherwise zero tax, and the flag's
  `tax_basis` records which (`STATED`, `FROM_RATE` or `NONE_STATED`). Shipping is added after tax
  and isn't taxed: on 1010, 6,700 + 335 (5% of 6,700) + 150 shipping = 7,185. If tax or shipping
  was stated but unreadable, the total check is skipped rather than guessing zero; the
  `UNPARSEABLE` flag already explains the problem. Any other check whose inputs are missing is
  skipped.
- **Stock is checked per invoice and doesn't decrement** across invoices.

### Results on the sample data

With `--inventory inventory.db`. Notes at INFO are left out: the three CSVs name no currency, so each
also carries `CURRENCY_ASSUMED`.

| Invoice | Flags |
|---|---|
| 1004, 1004 R1, 1006, 1015 | None |
| 1005 | `STOCK_EXCEEDED` GadgetX (8 / 5); `OVER_APPROVAL_THRESHOLD` |
| 1007 | `STOCK_EXCEEDED` WidgetA (20 / 15) and WidgetB (15 / 10); `TOTAL_MISMATCH` (stated 15,525.00, computed 15,635.00); `OVER_APPROVAL_THRESHOLD` |
| 1009 | `QTY_NON_POSITIVE`, `VENDOR_MISSING`, `DUE_DATE_MISSING`, `TOTAL_NEGATIVE`, `SUBTOTAL_MISMATCH` (stated 1,000.00, computed −250.00) |
| 1013 | `DUPLICATE_ITEM` ×3; `STOCK_EXCEEDED` on all three items; `TOTAL_MISMATCH` (stated 22,562.80, computed 22,512.80); `OVER_APPROVAL_THRESHOLD` |
| 1014 | `CURRENCY_UNSUPPORTED` (EUR); arithmetic and stock are fine |
| 1016 | `UNKNOWN_PRODUCT` WidgetC |

### Not handled yet

- Scanned PDFs with no text layer (they need OCR first; the loader says so).
- Product-name variants such as "Widget A" or "WidgetA (rush order)" (exact match only).
- Currency conversion: a non-USD invoice is held for review, not converted.
- Confirming an assumed currency. An invoice that names none is read as USD and says so
  (`CURRENCY_ASSUMED`). A vendor who bills in another dollar and writes only `$` can't be told apart
  from the invoice alone; that needs a vendor record giving each vendor's billing currency.
- Multiple tax rows: only the first counts, and the rest go to `unexpected_fields`.
