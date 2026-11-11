# 02 — Plan of Attack

How to build the submission. Read `01-situation-assessment.md` first; this plan follows from its findings.
You build this yourself: each milestone names the curriculum module (`03-fde-curriculum.md`) that
teaches what it needs.

## 1. Thesis

> **Code decides what is true. The model handles what is unclear. A person owns what is risky.**

- Arithmetic, stock lookups, duplicate checks and policy thresholds are deterministic code. They are
  cheap, testable and identical on every run.
- The LLM does three jobs it is actually good at: extracting structure from free text, repairing damaged
  input, and investigating ambiguous cases with tools and explaining its decision in language an
  approver can read.
- Anything with unresolved risk goes to a human queue with the evidence attached.

This is the position to defend in every interview round. It is also what separates "agentic
sophistication" from "an LLM call at every step."

## 2. Time budget

No deadline is stated in the brief. Until you know yours, plan for **four focused build days** and cut
from the bottom of the milestone list, never from tests or logs.

| Day | Milestones | State at end of day |
|---|---|---|
| 1 | M0–M2 | Deterministic pipeline correct on the 10 structured files, no LLM |
| 2 | M3 | All 20 files ingest; self-correction loop works; runs offline |
| 3 | M4 | Agents, orchestration, three outcomes. **Minimum shippable submission** |
| 4 | M5–M6 | Review queue, output polish, README, logs, demo script |

If you have two days, ship M0–M4 with a plain README. If you have a week, add M7.

## 3. The core idea for inconsistent data

Four mechanisms. If you only build these well, the submission is already strong.

### 3.1 One canonical invoice, with provenance

Every format converges on a single typed model. Each field keeps what was read *and* what it was read as.

```
Invoice
  invoice_number      "INV-1012"      raw: "INV 1012"       source: txt line 6
  revision            null
  vendor_name         "QuickShip Distributers"
  invoice_date        2026-01-26      raw: "26-Jan-2O26"    repaired: O→0
  due_date            2026-02-25
  currency            "USD"           assumed: true
  line_items[]        sku, raw_description, quantity, unit_price, line_total
  subtotal, tax, shipping, total      (stated values, as Decimal)
  source_file, extraction_method      "llm" | "json" | "csv-rows" | "csv-kv" | "xml"
```

Rules: money is `Decimal`, never float. Dates are `date` or null, never a guess. A field that could not
be read is null plus a finding, not a default. "Today" is injected, not read from the system clock:
every due date in the sample is already in the past.

### 3.2 Findings, not booleans

Validation does not return pass/fail. It returns a list of findings, each with a stable code, a
severity, and evidence. Everything downstream (approval, logs, the UI, tests) reads findings.

| Severity | Meaning | Codes |
|---|---|---|
| `BLOCK` | Cannot be paid as submitted | `QTY_NON_POSITIVE`, `TOTAL_NEGATIVE`, `TOTAL_MISMATCH`, `SUBTOTAL_MISMATCH`, `VENDOR_MISSING` |
| `REVIEW` | A person must decide | `STOCK_EXCEEDED`, `ZERO_STOCK_ITEM`, `UNKNOWN_ITEM`, `CURRENCY_UNSUPPORTED`, `OVER_APPROVAL_THRESHOLD`, `REVISION_OF_PAID`, `DUE_DATE_MISSING`, `DUE_DATE_UNPARSEABLE`, `EXTRACTION_UNRESOLVED` |
| `WARN` | Payable, but recorded | `PRICE_VARIANCE`, `OCR_REPAIRED`, `NEAR_THRESHOLD`, `TERMS_DATE_MISMATCH`, `VENDOR_ALIAS`, `PRESSURE_LANGUAGE`, `WIRE_REQUEST` |
| `INFO` | Normalisation notes | `DATE_FORMAT_NORMALISED`, `SKU_NORMALISED`, `SUPERSEDES_UNPAID`, `EXTRA_CHARGE` (shipping) |

Example: `STOCK_EXCEEDED { sku: "WidgetA", requested: 22, available: 15, lines: [1, 4, 7] }`.

Notes on specific codes:

- `PRESSURE_LANGUAGE` and `WIRE_REQUEST` are keyword checks in code. A reject path must not depend on
  the model noticing something.
- `TERMS_DATE_MISMATCH` needs a tolerance of about three days. A strict check fires on 8 of the 11
  invoices that state terms, including four clean ones (31 days against Net 30 is a calendar month).
  With the tolerance, only 1002 fires (0 days against Net 30).
- `PRICE_VARIANCE` is skipped when the currency is unsupported. Comparing 225 EUR to 250 USD is the
  bug, not the finding.
- Duplicates are a routing outcome (section 3.4), not a finding on the invoice.

### 3.3 Recompute, then reconcile

Never trust a stated aggregate. Recompute line totals, subtotal and total from the parts and compare.
This single check catches 1007 (short $110), 1013 (padded $50) and 1009 (subtotal contradiction), and
it is what *proves* an OCR repair on 1012.

For LLM-extracted files a failed reconciliation has two possible causes, and the loop must tell them
apart:

1. **Grounding check.** Is every extracted number present in the source text (after OCR repair)?
2. If a number is **not grounded**, the extraction is wrong: send the specific mismatch back and retry,
   at most twice. Still failing: `EXTRACTION_UNRESOLVED`, hold.
3. If every number **is grounded** and the invoice still does not reconcile, the vendor is wrong: emit
   `TOTAL_MISMATCH` and stop. Do not retry — a retry here only invites the model to "fix" the total.

### 3.4 A ledger for cross-file consistency

A SQLite table keyed on `(normalised invoice number, revision)` with status and a hash of the
**canonical fields** (number, vendor, lines, total), not of the file bytes: `invoice_1011.pdf` omits
lines the TXT has and would hash differently.

- **Same file processed again:** replay the recorded result; pay nothing. Re-running a command must
  never turn a clean invoice into a "duplicate".
- **Different file, same number and revision:** outcome `SKIPPED_DUPLICATE`.
- **Higher revision, original unpaid:** original becomes `SUPERSEDED`; the revision is judged on its
  merits with `SUPERSEDES_UNPAID`.
- **Higher revision, original already paid:** `REVISION_OF_PAID`, hold; a person decides on the
  difference.
- **Batch mode** does a pre-pass: group files by normalised invoice number, keep the highest revision,
  and among equal copies prefer structured formats over TXT over PDF. Note that plain name order would
  process `invoice_1011.pdf` before `invoice_1011.txt`.
- The database file is gitignored, tests use a temporary one, and `--fresh` resets it.

## 4. Architecture

```
                       ┌──────────────────────────────────────────────────────┐
                       │                 Shared state + ledger                │
                       └──────────────────────────────────────────────────────┘
 file ──► INGEST ──────────► VALIDATE ─────────► APPROVE ───────────► PAY / HOLD / REJECT
          agent              agent               reviewer + critic    agent
          │                  │                   │                    │
          route by format    deterministic       policy floor (code)  mock_payment
          parse or LLM       checks as tools     reviewer calls tools idempotency key
          normalise          aggregate by SKU    critic checks draft  ledger + audit log
          ground + reconcile ledger lookup       outcome ≥ floor
            └─retry─┘                              └─revise─┘
```

| Stage | Deterministic part | LLM part | Loop and its judge |
|---|---|---|---|
| Ingest | Format routing; JSON, XML and both CSV parsers; normalisers | Structured extraction for TXT and PDF text | Retry on ungrounded values; judge = grounding + reconciliation; max 2 |
| Validate | The full check suite always runs, in code | For `UNKNOWN_ITEM` only: the agent calls `find_similar_sku` and proposes a match with a reason | — |
| Approve | Policy table sets the minimum outcome (the floor) | Reviewer agent investigates with tools, decides at or above the floor, writes the rationale. Critic challenges it | Reviewer ↔ critic, max 2 rounds; judge = outcome ≥ floor, every `REVIEW`/`BLOCK` finding addressed in the rationale, every cited fact traceable to a finding or tool result |
| Pay | Idempotent `mock_payment`, ledger write | — | Retry with backoff on simulated failure |

**Where the model actually calls tools.** The reviewer agent gets `get_stock`, `get_vendor_history`
(ledger), `get_catalog_price` and `find_similar_sku`, and chooses which to call. That is real function
calling with a purpose: "Widgets Inc. has one prior clean invoice; WidgetC resembles WidgetA/B;
recommend hold for item setup, not reject."

**Where the model actually decides.** The policy floor covers the clear cases. The judgment rows in the
golden set (1008, 1010) are the reviewer's to decide above the floor, and its tests assert a range
("hold or reject"), not a single value. Clean invoices under the threshold with no findings skip the
reviewer entirely: no reason to spend tokens on them.

**Guardrail to state out loud:** the approval agents can escalate (approve → hold → reject) but cannot
go below the floor. An invoice with a `BLOCK` finding is never paid, whatever the model writes. One
test proves it with a stubbed model that always says "approve".

**Untrusted input:** invoice text goes into prompts as delimited data. Notes fields never reach the
payment step. Test this with 1003.

### Framework

Recommendation: **LangGraph**. It is first in the JD's list, and its primitives map directly onto this
problem: typed shared state, conditional edges for the outcomes, cycles for the two loops,
checkpointing, and interrupts for the human hold. Keep the business logic in plain Python modules that
do not import the framework, so the graph is a thin wiring layer and everything is unit-testable
without it. That also lets you answer "what if we used CrewAI or a custom router?" with "an afternoon."

### LLM provider

One small interface, two implementations: an OpenAI-compatible client (Grok through xAI's endpoint;
any other compatible model by changing configuration) and an offline provider that replays recorded
responses. The offline provider is what lets a reviewer run the repo with no key and what makes your
demo safe. It replays real model output; it is not a second, model-free implementation.

## 5. Approval policy (v1)

Written as data so it can be shown to a VP and changed without touching prompts. First matching row
sets the floor.

| Condition | Floor |
|---|---|
| Any `BLOCK` finding | Reject |
| Two or more fraud signals (`ZERO_STOCK_ITEM`, `DUE_DATE_UNPARSEABLE`, `PRESSURE_LANGUAGE`, `WIRE_REQUEST`) | Reject |
| Any `REVIEW` finding | Hold |
| Total ≥ $10,000 (`OVER_APPROVAL_THRESHOLD`) | Hold, even if otherwise clean |
| `NEAR_THRESHOLD` (within 2% below $10K) plus any other `WARN` | Hold |
| Only `WARN`/`INFO` findings | Approve; warnings recorded |

A single fraud signal is not fraud: an unreadable date alone is a hold. Signals stack.

## 6. Expected outcomes (your golden set)

This table is your test suite and your demo script. Encode it as a fixture before writing the pipeline.
It assumes **batch mode over the folder with a fresh ledger**. ⚖ marks the reviewer's judgment calls:
the test accepts the stated range.

| Invoice | Outcome | Key findings |
|---|---|---|
| 1001 | **Pay** $5,000.00 | — |
| 1002 | Hold | `STOCK_EXCEEDED` GadgetX 20/5, `OVER_APPROVAL_THRESHOLD`, `TERMS_DATE_MISMATCH` |
| 1003 | **Reject** | Four fraud signals; also over threshold |
| 1004 | Superseded (not paid) | — |
| 1004 R1 | **Pay** $5,940.00 | `SUPERSEDES_UNPAID`; GadgetX 5/5 |
| 1005 | Hold | `STOCK_EXCEEDED` GadgetX 8/5, `OVER_APPROVAL_THRESHOLD` |
| 1006 | **Pay** $2,750.00 | — |
| 1007 | **Reject** | `TOTAL_MISMATCH` (stated 15,525.00, computed 15,635.00); stock exceeded on two SKUs |
| 1008 | ⚖ Hold or reject | Two `UNKNOWN_ITEM`, `NEAR_THRESHOLD` |
| 1009 | **Reject** | `QTY_NON_POSITIVE`, `TOTAL_NEGATIVE`, `VENDOR_MISSING`, `SUBTOTAL_MISMATCH`, `DUE_DATE_MISSING` |
| 1010 | ⚖ Pay $7,185.00 or hold | `PRICE_VARIANCE` WidgetA rush +20%; aggregated WidgetA 12/15; `EXTRA_CHARGE` |
| 1011 txt | **Pay** $3,000.00 | — |
| 1011 pdf | Skipped duplicate | — |
| 1012 txt | Hold | `NEAR_THRESHOLD` plus `OCR_REPAIRED` ×2 and `VENDOR_ALIAS` |
| 1012 pdf | Skipped duplicate | — |
| 1013 json | **Reject** | `TOTAL_MISMATCH` (+$50.00); aggregated stock exceeded on all three SKUs |
| 1013 pdf | Skipped duplicate | — |
| 1014 | Hold | `CURRENCY_UNSUPPORTED` (EUR) |
| 1015 | **Pay** $6,500.00 | — |
| 1016 | Hold | `UNKNOWN_ITEM` WidgetC |

Totals for 16 distinct invoices: 6 paid ($30,375.00 if 1010 is paid), 6 held, 4 rejected.

Add separate single-file tests for the cases batch mode hides:

- 1004 alone pays $1,890.00. Then 1004 R1 gives `REVISION_OF_PAID`, hold.
- The same file twice: second run replays, pays nothing.
- 1011 PDF after 1011 TXT: skipped duplicate. And the reverse order.
- 1013 PDF alone: rejected through the LLM path, with the grounding check stopping the retry loop.

## 7. Milestones

Each one ends with something runnable. If time runs out, you stop at the last finished milestone and
still have a submission.

| # | Milestone | Done when | Curriculum |
|---|---|---|---|
| M0 | **Skeleton and golden set.** Repo layout, pinned dependencies, lint, `inventory.db` seed script, section 6 as a test fixture, CLI stub accepting `--invoice_path` | `pytest` runs and fails on 20 named cases | 1, 7 (labs 1–2) |
| M1 | **Deterministic ingest.** JSON, XML, both CSV shapes into the canonical model; normalisers; reconciliation | 10 structured files parse with correct findings; 1006 keeps both items | 1 |
| M2 | **Validation and ledger.** All checks as tools over SQLite, aggregated by SKU; small `catalog` table for price checks; ledger rules from 3.4 | Every structured-file row in section 6 is green with the LLM switched off | 1, 5 (labs 1, 2, 4) |
| M3 | **LLM extraction with self-correction.** Provider interface, structured output, grounding check, retry loop, offline replay | All 7 TXT and 3 PDF files extract correctly; 1012 repaired and proven by arithmetic; 1013 PDF stops without retrying; runs with no key | 2, 3 |
| M4 | **Approval agents and orchestration.** Policy floor, reviewer with tools, critic, LangGraph wiring, outcomes. *Minimum shippable* | Full golden set green end to end; the "always approve" stub cannot pay a `BLOCK` invoice | 3, 4 |
| M5 | **Payment, review queue, output.** Idempotent mock payment; `review` command that lists held invoices and resumes with a human decision; a readable decision card per invoice and a batch summary table; JSON logs with a run ID | Running the folder twice pays nothing twice; a held invoice can be released from the CLI; output for all 20 files committed | 5, 6 |
| M6 | **Presentation layer.** README rewritten outcome-first, architecture diagram, decisions log, "not built, and why", demo script | A stranger can run it in under 2 minutes with no key | 9 |
| M7 | **Above and beyond** (pick one or two) | See section 8 | 6, 7 |

**UI/UX is a scored criterion, so M5's output is not optional polish.** A person should be able to read
one invoice's result in five seconds: outcome, amount, the two or three findings that drove it, the
reviewer's one-paragraph reason. The review command matters because hold is the central design
decision and is otherwise a dead end.

Keep four named agents visible in the code (`ingestion`, `validation`, `approval` reviewer and critic,
`payment`). The brief names them, and a reviewer scanning the repo will look for them.

## 8. Above-and-beyond candidates, by value for effort

1. **Eval report.** One command prints extraction accuracy per field and decision accuracy against the
   golden set. This is the JD's "continuous-improvement loop" in concrete form.
2. **Extra adversarial fixtures.** Your own invoices: a prompt-injection note, a changed bank detail, a
   split invoice (two files just under $10K from one vendor on one day). "Added to test cases" is named
   in the criteria.
3. **Small web UI** (Streamlit) over the review queue.
4. **Dockerfile** and a one-line run. Cheap, and it touches the JD's infrastructure line.

## 9. What to cut, and say you cut

Real OCR for scanned images. Currency conversion (hold instead). Real email ingestion. Stock that
decrements. Vendor master and bank-detail verification. A second live model provider. Cloud
deployment. Authentication. List these in the README under "Not built, and why" — the JD asks for
people who cut scope deliberately, so show the cut list.

## 10. Demo plan (7 minutes)

1. **30 s — the problem in their words.** $2M, 30%, 5 days. One sentence on what you found in the data.
2. **60 s — clean path.** 1001: file to payment in seconds.
3. **90 s — messy path.** 1012: raw text, the repair, the arithmetic that proves it, and why it is
   still held ($25 under the limit, renamed vendor). Release it from the review queue.
4. **90 s — the trap.** 1013: every line passes, the invoice fails, and $50 was added to the total.
5. **60 s — fraud.** 1003: rejected, with reasons; show that the "URGENT" note changed nothing.
6. **60 s — the folder.** Batch run: outcome table, four duplicate files caught, nothing paid twice.
7. **60 s — what is next.** Three-way match against PO and goods receipt, vendor master, ERP connector,
   what you would measure in week one.

## 11. Questions you will be asked

Prepare a two-sentence answer to each before submitting.

- Why is validation mostly code and not an LLM?
- What stops the model approving a bad invoice?
- How do you know extraction is right? What is your accuracy, and how did you measure it?
- How does your loop tell a bad extraction from a bad invoice?
- What happens when Grok is down, slow, or returns malformed JSON?
- Why LangGraph? What would change with CrewAI or no framework?
- 1004 arrived twice. What did your system do and why?
- How does this scale to 10,000 invoices a day? What breaks first?
- What would you do in the first week on site at Acme?
- What did you cut, and what would you build next?
