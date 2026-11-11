# Demo material

Numbers and examples to use in the demo, each with the command that reproduces it. Everything
here runs offline from the recordings in `data/replays`, with no API key.

This is a collection of material, not the demo script. Add to it as stages are built.

## Self-correction: the headline numbers

```bash
python -m extraction.report data/invoices
```

Saved output: [`self_correction_report.txt`](self_correction_report.txt). A test fails if it goes
stale.

| | Without self-correction | With |
|---|---|---|
| Flags that would hold an invoice (BLOCK or REVIEW), 20 files | 39 | 30 |
| Model calls, 10 TXT and PDF files | 10 | 11 |
| Files improved / worse / unchanged | | 3 / 0 / 17 |

How to say it: self-correction removed nine problems that were the model's or the scanner's, for
one extra model call, and changed nothing on the invoices that are wrong in themselves.

What not to claim: a success rate. Ten text files with one real retry shows it worked here and
did no harm. Fewer holding flags is a proxy for "better", not proof the values are right; the
recorded-sample tests check the values against the documents.

## Three invoices that tell the story

Each command prints the invoice as JSON; the fields to point at are named below.

### 1002: the model made a mistake, was told, and fixed it

```bash
python main.py --invoice_path=data/invoices/invoice_1002.txt --offline
```

- `extraction_attempts`: attempt 1 put the invoice total ($15,000.00) on the line item and was
  rejected (`NOT_ON_ONE_LINE`); attempt 2 is fully grounded.
- The judge is code, not the model: every value must appear in the document, and a line's values
  must sit on one row.
- It is still held, for the right reasons: 20 GadgetX against 5 in stock, and over $10K.
- Worth saying: across three recording runs the model returned this file three different ways at
  temperature 0. That is why the check is deterministic.

### 1012: scanner damage, read only where the invoice proves it

```bash
python main.py --invoice_path=data/invoices/invoice_1012.txt --offline
```

- The model copied `$3,500.O0` and `26-Jan-2O26` exactly as written. It was told not to fix them.
- Code read both, each with proof on the flag: 7 × $500 = $3,500.00, and Net 30 from 26 Jan to
  the 25 Feb due date.
- "Widget A" and "Gadget X" were matched to the inventory's names, so stock was checked.
- Four holding flags became none. It now carries warnings only, and is $25 under the $10K
  threshold: the case the approval policy has to catch.

### 1013: every line passes, the invoice does not

```bash
python main.py --invoice_path=data/invoices/invoice_1013.pdf --offline
```

- Each line is within stock on its own. Summed per item: 22 WidgetA against 15, 18 WidgetB
  against 10, 9 GadgetX against 5.
- The stated total is $22,562.80; lines plus tax come to $22,512.80. The $50 is not in any line.
- Self-correction left it alone. The model copied the document faithfully, and the document is wrong.

## Other facts worth having to hand

| Fact | Where it comes from |
|---|---|
| All 20 sample files run with no API key | `python main.py --invoice_path=data/invoices --offline` |
| Nine of ten text files were grounded on the first model attempt | `tests/test_recordings.py` |
| Worst flag per file: 4 BLOCK, 6 REVIEW, 3 WARN, 5 INFO only, 2 none | the folder run above |
| A document that says "ignore previous instructions" can only write text into a field | `tests/test_extraction.py` |
| The brief's own example path, `invoice1.txt`, does not exist | `python main.py --invoice_path=data/invoices/invoice1.txt` lists what does |

## Not built yet, so not demo material yet

Approval, payment, and duplicate detection across files (1011, 1012 and 1013 each arrive twice;
1004 has a revision).
