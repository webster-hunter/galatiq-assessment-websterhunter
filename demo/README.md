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

## The whole folder: what gets paid, and that it is paid once

```bash
python main.py --invoice_path=data/invoices --offline --ledger demo.db > /dev/null    # first run
python main.py --invoice_path=data/invoices --offline --ledger demo.db > /dev/null    # second run
```

`demo.db` is a ledger of its own for the demo (delete it to start over). Each run prints a one-line summary on stderr (and the brief's "Paid ... to ..." line per payment).

| | First run | Second run |
|---|---|---|
| Paid | 5 invoices, $19,140.00 | 0 |
| Held | 8 | |
| Rejected | 4 | |
| Skipped as a duplicate | 3 | 3 |
| Already processed | | 17 |

- **Paid:** 1001, 1004, 1006, 1011, 1015.
- **Duplicates caught:** 1011, 1012 and 1013 each arrive in two formats. The second copy is
  recognised by content, not file name, and skipped. Without that, 1011 would be paid twice
  ($3,000.00), and 1012 would be in the review queue twice.
- **1004's revision is held.** The original ($1,890.00) is paid first; the revision ($5,940.00)
  then arrives and is held with the $4,050.00 difference for a person to settle.
- **1003 is rejected on four fraud signals** (two of them facts, not wording), each from a check in code: FakeItem has no stock,
  the due date is "yesterday", the notes say "URGENT... penalties", and they ask for a wire.
- **1012 is held although nothing on it is wrong any more.** It is $25 under the $10K limit and
  carries repairs and other warnings.

How to say it: nothing is paid twice, whether the same invoice arrives as a second file or the
whole folder is run again.

What not to claim: that payments are real or retried. `mock_payment` always succeeds; what is
tested is that a payment which never confirmed is not blindly sent again.

## Holding an invoice for a person, and releasing it

After the folder run above (same `demo.db`; add `--checkpoints demo-cp.db` to both commands to
keep the demo's saved runs apart too):

```bash
python review.py --ledger demo.db                                     # eight invoices waiting, with the agent's advice
python review.py --ledger demo.db approve 2 --by "Dana Lee"           # 1004 revised: pays the $4,050.00 difference
python review.py --ledger demo.db approve 7 --by "Dana Lee"           # 1014: listed as 4,125.00 EUR (~$4,480.16); pays 4,125.00 EUR
```

What to point at:

- **`main.py` has exited.** Each held invoice is a pipeline run saved mid-flight; `review.py` is a
  separate process that picks one up and lets it finish.
- **The revision pays $4,050.00, not $5,940.00,** because $1,890.00 already went out.
- **The EUR invoice is approved like any other.** The queue shows what it costs in dollars, from
  the rate service, and the reviewer types nothing. The vendor is paid 4,125.00 EUR, what they
  billed. Say plainly that the rate service is a local mock with made-up rates, as the brief's
  no-internet rule requires; a real one is one class with the same method.
- **Every decision carries a name and a note** in the ledger.

How to say it: the system never asks a person to do what it can do, and never does what a person
should decide. A model can make it stricter; only a named person can release a hold.

## The reviewer agent: a model that looks things up, and can only add caution

```bash
python main.py --invoice_path=data/invoices --offline --ledger demo.db --trace 2>&1 >/dev/null | grep '"reviewer"'
```

One line per invoice showing who wrote the reason (the agent or the policy), its recommendation,
and which tools it called. `python review.py --ledger demo.db` then prints its advice under the queue.

What to point at:

- **1016.** The agent chose two lookups itself: products like WidgetC (it found WidgetA and
  WidgetB) and the vendor's history (one earlier invoice, paid). Its question to the reviewer:
  is WidgetC something we mean to order?
- **1010.** The policy approves it. The agent held it, because WidgetA is billed at $250 on one
  line and $300 on another. This is the one place a model changed an outcome, and it made it
  stricter.
- **1003 and 1013.** Rejected by the policy with no model call at all.

How to say it: the model can add a hold and advise a person. It cannot approve, reject or pay,
and that is enforced in code, so nothing a vendor writes on an invoice can talk it into paying.

A true story worth telling: its first replies suspected four invoices of being duplicates. The
cause was my tool, which listed the invoice under review in the vendor's history. Reading real
output, not just passing tests, is what found it.

What not to claim: that its reasoning is checked. Nothing yet verifies a rationale's claims
against the invoice; one early reply invented a quantity change. That check is the critic, next.

## Watching one invoice move through the pipeline

```bash
python main.py --invoice_path=data/invoices/invoice_1003.txt --offline --trace > /dev/null
```

Prints one line per stage as it finishes: how the file was read, the flags found, the decision
with the rule and reasons, and the branch taken. Good for showing the four stages without
scrolling through JSON. The pipeline diagram is in the main README.

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
