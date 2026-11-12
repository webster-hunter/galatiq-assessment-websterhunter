# LLM extraction

Part of the reference documentation. Start with the [README](../README.md).

`extraction/` handles the invoices that have no structure to parse.

| File | Role |
|---|---|
| `extraction/__init__.py` | Reads the text (PDF text via PyMuPDF), prompts the model, builds the `Invoice` |
| `extraction/providers.py` | `Grok` (xAI API) and `Replay` (recorded replies) |
| `extraction/grounding.py` | Checks that every value the model returned is in the document |
| `extraction/report.py` | Measures what self-correction changes, file by file |

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
- A line item's quantity, price and amount must sit together on one line of the document, with
  its item name, so a value taken from another row is caught (`NOT_ON_ONE_LINE`). A long item
  name that wraps onto the line before or after its numbers is still one row.
- It proves a value wasn't made up. It does not prove the value is in the right field, and it can't
  see a line the model left out; the arithmetic checks catch that.

**Self-correction.** When grounding finds a value that isn't in the document, the model is sent
its own reply and the specific complaint (which field, which value, which rule) and tries again,
up to three attempts in all. The complaint never suggests a replacement, and null is always allowed.

- Only grounding failures are retried, because only they are the model's mistakes. A total that
  doesn't add up (1013) or OCR damage copied as written (1012) is what the document says;
  retrying would push the model to change it.
- A retry may change only what was complained about. One that changes anything else (drops a
  line item, blanks the total) is never used, and the next message says what to put back.
  Otherwise a model could clear a complaint by returning less, and the invoice would be
  rejected for missing data that was in the document.
- The loop stops when an attempt is fully grounded, when the model returns the same transcription
  twice running, or at the limit. Of the usable attempts, the one with the fewest ungrounded
  values is built, and any that remain stay on the invoice as `NOT_IN_SOURCE`, so it is held.
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
| 1008 | Both items `UNKNOWN_PRODUCT` and `PRODUCT_NOT_STOCKED`; the email's From, To and Subject kept in `unexpected_fields` |
| 1010 | Reconciles with shipping; the rush line is item WidgetA with note "rush order", so `DUPLICATE_ITEM`; vendor is Consolidated Materials Group, not the Bill To party |
| 1012 (txt and pdf) | OCR damage copied as written by the model, then read by code: `$3,500.O0` is confirmed by 7 × $500 and `26-Jan-2O26` by Net 30 to the due date (`OCR_REPAIRED` ×2); "Widget A" and "Gadget X" are matched to the inventory's WidgetA and GadgetX (`ITEM_NAME_REPAIRED` ×2), and all three items are within stock; `UNEXPECTED_FORMAT` for `INV 1012`. No REVIEW or BLOCK flag remains; vendor is the FROM party, with "formerly FastShip Ltd." kept apart in `unexpected_fields` |
| 1013 (pdf) | All eight lines; `STOCK_EXCEEDED` on all three items; `TOTAL_MISMATCH` (stated 22,562.80, computed 22,512.80); `OVER_APPROVAL_THRESHOLD` |

Every vendor and total is correct, and nine of the ten files were grounded on the first attempt.
1002 needed the retry: the model first copied the invoice total ("Amt: $15,000.00") onto the line
item as well, although the prompt says a line amount must be on the line's own row. Grounding
flagged it (`NOT_ON_ONE_LINE`), the complaint was sent back, and the second attempt returned the
line without an amount, fully grounded. Both replies are recorded as the model produced them.

The model's output for 1002 varied between recording runs even at temperature 0 (total missing,
correct, total duplicated), which is why the judge is code and not a re-reading by the model.

### Does self-correction help?

`python -m extraction.report data/invoices` runs every file twice from the recordings, without
self-correction (one model attempt, no repairs) and with it, and compares the flags that would
hold the invoice (BLOCK or REVIEW):

| | Without | With |
|---|---|---|
| Holding flags across the 20 files | 41 | 32 |
| Model calls for the 10 TXT and PDF files | 10 | 11 |
| Files improved / worse / unchanged | | 3 / 0 / 17 |

- **1002:** one retry removed the model's own error (`NOT_IN_SOURCE`). It is still held, for
  stock and for being over $10K, which are the invoice's real problems.
- **1012 (txt and pdf):** two OCR values and two item names repaired; four holding flags each
  become none.
- **1003, 1007, 1009, 1013:** unchanged. Self-correction fixes the model's and the scanner's
  errors and leaves the vendor's alone.

Fewer holding flags is a proxy: it shows problems that weren't the vendor's were removed, not
that the final values are right. `tests/test_recordings.py` checks those against the documents.
Ten text files with one real retry is a small sample; the report is there to be re-run as more
invoices are recorded.

Not done yet: detecting duplicates across files (1011, 1012 and 1013 each arrive twice), and
anything that weighs warnings together. 1012 now carries only warnings, but it is $25 under the
$10K threshold with four values repaired and a renamed vendor; holding that combination is a job
for the approval policy.
