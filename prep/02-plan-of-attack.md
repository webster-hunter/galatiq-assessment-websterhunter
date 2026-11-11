# 02 — Plan of Attack

How to build the submission. Read `01-situation-assessment.md` first; this plan follows from its findings.
You build this yourself: each milestone names the curriculum module (`03-fde-curriculum.md`) that
teaches what it needs.

## 1. Thesis

> **Code decides what is true. The model handles what is unclear. A person owns what is risky.**

- Arithmetic, stock lookups, duplicate checks and policy thresholds are deterministic code. They are
  cheap, testable and identical on every run.
- The LLM does three jobs it is actually good at: extracting structure from free text, repairing damaged
  input, and reasoning about ambiguous cases in language an approver can read.
- Anything with unresolved risk goes to a human queue with the evidence attached.

This is the position to defend in every interview round. It is also what separates "agentic
sophistication" from "an LLM call at every step."

## 2. The core idea for inconsistent data

Four mechanisms. If you only build these well, the submission is already strong.

### 2.1 One canonical invoice, with provenance

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
be read is null plus a finding, not a default.

### 2.2 Findings, not booleans

Validation does not return pass/fail. It returns a list of findings, each with a stable code, a
severity, and evidence. Everything downstream (approval, logs, the UI, tests) reads findings.

| Severity | Meaning | Examples |
|---|---|---|
| `BLOCK` | Cannot be paid as submitted | `QTY_NON_POSITIVE`, `TOTAL_MISMATCH`, `VENDOR_MISSING`, `DUPLICATE_INVOICE`, `ZERO_STOCK_ITEM` |
| `REVIEW` | A person must decide | `STOCK_EXCEEDED`, `UNKNOWN_ITEM`, `CURRENCY_UNSUPPORTED`, `OVER_APPROVAL_THRESHOLD`, `REVISION_OF_EXISTING` |
| `WARN` | Payable, but recorded | `PRICE_VARIANCE`, `OCR_REPAIRED`, `NEAR_THRESHOLD`, `TERMS_DATE_MISMATCH`, `VENDOR_ALIAS` |
| `INFO` | Normalisation notes | `DATE_FORMAT_NORMALISED`, `SKU_NORMALISED` |

Example: `STOCK_EXCEEDED { sku: "WidgetA", requested: 22, available: 15, lines: [1, 4, 7] }`.

### 2.3 Recompute, then reconcile

Never trust a stated aggregate. Recompute line totals, subtotal and total from the parts and compare.
This single check catches 1007 (short $110), 1013 (padded $50) and 1009 (subtotal contradiction), and
it is what *proves* an OCR repair on 1012. It is also your extraction quality gate: if an LLM extraction
does not reconcile, that is a signal to retry extraction before blaming the vendor.

### 2.4 A ledger for cross-file consistency

A SQLite table keyed on `(normalised invoice number, revision)` with a content hash and status. It gives
you duplicate detection, revision handling, payment idempotency and an audit trail from one mechanism.

## 3. Architecture

```
                       ┌──────────────────────────────────────────────────────┐
                       │                 Shared state + ledger                │
                       └──────────────────────────────────────────────────────┘
 file ──► INGEST ──────────► VALIDATE ─────────► APPROVE ───────────► PAY / HOLD / REJECT
          │                  │                   │                    │
          route by format    deterministic       policy rules (code)  mock_payment
          parse or LLM       checks via tools    reviewer agent       idempotency key
          normalise          aggregate by SKU    critic agent         ledger + audit log
          reconcile ◄─┐      ledger lookup       guardrail: rules
            └─retry───┘                          are a floor
          (self-correction)                      (reflection loop)
```

| Stage | Deterministic part | LLM part | Loop |
|---|---|---|---|
| Ingest | Format routing; JSON, XML and both CSV parsers; normalisers; reconciliation | Structured extraction for TXT and PDF text | Validation errors fed back to the extractor, max 2 retries, then hold |
| Validate | All checks, as tools against SQLite | None needed. Optional: suggest nearest SKU for unknown items | — |
| Approve | Policy table maps findings to the minimum outcome | Reviewer writes decision and rationale; critic checks it against findings and policy | Reviewer ↔ critic, max 2 rounds |
| Pay | Idempotent `mock_payment`, ledger write | Optional: human-readable summary | Retry with backoff on simulated failure |

Two loops, both with a hard stop, both with a deterministic check as the judge. That is the answer to
"show me your self-correction."

**Guardrail to state out loud:** the approval agents can escalate (approve → hold → reject) but cannot
de-escalate below what the policy table requires. An invoice with a `BLOCK` finding is never paid,
whatever the model writes.

**Untrusted input:** invoice text goes into prompts as delimited data. Notes fields never reach the
payment step. Test this with 1003.

### Framework

Recommendation: **LangGraph**. It is first in the JD's list, and its primitives map directly onto this
problem: typed shared state, conditional edges for the three outcomes, cycles for the two loops,
checkpointing, and interrupts for the human hold. Keep the business logic in plain Python modules that
do not import the framework, so the graph is a thin wiring layer and everything is unit-testable
without it. That also lets you answer "what if we used CrewAI or a custom router?" with "an afternoon."

### LLM provider

One small interface, three implementations: Grok (primary, via xAI's OpenAI-compatible endpoint),
one alternative frontier model, and an offline provider that replays recorded responses or falls back
to deterministic parsing. Model names live in configuration. The offline provider is what makes the
repo runnable by a reviewer with no key and what makes your demo safe.

## 4. Approval policy (v1)

Written as data so it can be shown to a VP and changed without touching prompts.

| Condition | Minimum outcome |
|---|---|
| Any `BLOCK` finding | Reject, with reasons logged |
| Fraud signals (zero-stock item, pressure language plus wire request, unparseable due date) | Reject |
| Any `REVIEW` finding | Hold for human |
| Total ≥ $10,000 | Hold for human, even if otherwise clean |
| Only `WARN`/`INFO` findings, total < $10,000 | Approve; warnings recorded |
| Total within 2% below the threshold | Add `NEAR_THRESHOLD` warning; reviewer agent must address it in its rationale |

## 5. Expected outcomes (your golden set)

This table is your test suite and your demo script. Encode it as a fixture before writing the pipeline.
Rows marked ⚖ are judgment calls: pick a side, write down why, be ready to argue the other.

| Invoice | Outcome | Key findings |
|---|---|---|
| 1001 | **Pay** $5,000.00 | — |
| 1002 | Hold | `STOCK_EXCEEDED` GadgetX 20/5, over threshold, terms/date mismatch |
| 1003 | **Reject** | Zero-stock item, unparseable due date, pressure language, wire request, $100K |
| 1004 | **Pay** $1,890.00 alone; superseded when R1 is present | — |
| 1004 R1 | ⚖ Hold if the original was already paid (pay the $4,050 difference only after review); pay $5,940.00 if the original was never paid | `REVISION_OF_EXISTING` |
| 1005 | Hold | `STOCK_EXCEEDED` GadgetX 8/5, over threshold |
| 1006 | **Pay** $2,750.00 | — |
| 1007 | **Reject** | `TOTAL_MISMATCH` (stated 15,525.00, computed 15,635.00), plus stock exceeded on two SKUs |
| 1008 | ⚖ Hold (reject is defensible) | Two `UNKNOWN_ITEM`, `NEAR_THRESHOLD` |
| 1009 | **Reject** | Negative quantity, negative total, vendor missing, due date missing, subtotal mismatch |
| 1010 | ⚖ Pay $7,185.00 with warning (hold is defensible) | `PRICE_VARIANCE` WidgetA rush +20%, aggregated WidgetA 12/15 |
| 1011 txt | **Pay** $3,000.00 | — |
| 1011 pdf | Skip | `DUPLICATE_INVOICE` |
| 1012 txt | ⚖ Pay $9,975.00 with warnings (hold is defensible) | `OCR_REPAIRED` ×2, `SKU_NORMALISED`, `NEAR_THRESHOLD`, `VENDOR_ALIAS` |
| 1012 pdf | Skip | `DUPLICATE_INVOICE` |
| 1013 json | **Reject** | `TOTAL_MISMATCH` (+$50.00), aggregated stock exceeded on all three SKUs |
| 1013 pdf | Skip | `DUPLICATE_INVOICE` |
| 1014 | Hold | `CURRENCY_UNSUPPORTED` (EUR), prices below catalogue |
| 1015 | **Pay** $6,500.00 | — |
| 1016 | Hold | `UNKNOWN_ITEM` WidgetC; vendor otherwise known |

Order sensitivity: duplicate and revision outcomes depend on which file is seen first. Process a
directory in a defined order (sorted by name) and say so.

## 6. Milestones

Each one ends with something runnable. If time runs out, you stop at the last finished milestone and
still have a submission.

| # | Milestone | Done when | Curriculum |
|---|---|---|---|
| M0 | **Skeleton and golden set.** Repo layout, `inventory.db` seed script, section 5 as a test fixture, CLI stub accepting `--invoice_path` | `pytest` runs and fails on 20 named cases | Module 1 |
| M1 | **Deterministic ingest.** JSON, XML, both CSV shapes into the canonical model; normalisers; reconciliation | 10 structured files parse with correct findings; 1006 keeps both items | Module 1 |
| M2 | **Validation.** All checks as tools over SQLite, aggregated by SKU; ledger for duplicates and revisions | Every structured-file row in section 5 is green with the LLM switched off | Modules 1, 5 |
| M3 | **LLM extraction with self-correction.** Provider interface, structured output, retry on failed reconciliation, offline provider | All 7 TXT and 3 PDF files extract correctly; 1012 repaired and proven by arithmetic; recorded mode works with no key | Modules 2, 3 |
| M4 | **Approval agents and orchestration.** Policy table, reviewer and critic, guardrail, LangGraph wiring, three outcomes | Full golden set green end to end; a test proves the model cannot approve a `BLOCK` invoice | Modules 3, 4 |
| M5 | **Payment and observability.** Idempotent mock payment, structured JSON logs with a run ID, per-run report, batch mode over a directory | Running the folder twice pays nothing twice; logs for all 20 files committed | Modules 5, 6 |
| M6 | **Presentation layer.** README rewritten outcome-first, architecture diagram, decisions log, demo script, business summary | A stranger can run it in under 2 minutes with no key | Module 9 |
| M7 | **Above and beyond** (pick one or two, not all) | See section 7 | Modules 6, 7 |

**Scope line:** M0–M6 is the submission. Cut M7 before cutting tests or logs.

## 7. Above-and-beyond candidates, by value for effort

1. **Human review queue.** `python main.py review` lists held invoices with findings and accepts
   approve/reject with a reason; the decision is logged and resumes the graph. It shows
   human-in-the-loop, a JD nice-to-have, and makes the hold outcome real.
2. **Eval report.** One command prints extraction accuracy per field and decision accuracy against the
   golden set, per provider. This is the "continuous-improvement loop" from the JD in concrete form.
3. **Extra adversarial fixtures.** Your own invoices: a prompt-injection note, a changed bank detail, a
   split invoice (two files just under $10K from one vendor on one day). "Added to test cases" is named
   in the criteria.
4. **Small web UI** (Streamlit) over the same review queue. Only after 1–3.
5. **Dockerfile** and a one-line run. Cheap, and it touches the JD's infrastructure line.

## 8. What to cut, and say you cut

Real OCR for scanned images. Currency conversion (hold instead). Real email ingestion. Stock that
decrements. Vendor bank-detail verification. Cloud deployment. Authentication. List these in the README
under "Not built, and why" — the JD asks for people who cut scope deliberately, so show the cut list.

## 9. Demo plan (7 minutes)

1. **30 s — the problem in their words.** $2M, 30%, 5 days. One sentence on what you found in the data.
2. **60 s — clean path.** 1001: file to payment in seconds.
3. **90 s — messy path.** 1012: show raw text, the repair, the arithmetic that proves it.
4. **90 s — the trap.** 1013: every line passes, the invoice fails, and $50 was added to the total.
5. **60 s — fraud.** 1003: rejected, with reasons; show that the "URGENT" note changed nothing.
6. **60 s — the folder.** Batch run: outcome table, nothing paid twice, $37,427.80 of duplicates stopped.
7. **60 s — what is next.** Three-way match against PO and goods receipt, vendor master, ERP connector,
   what you would measure in week one.

## 10. Questions you will be asked

Prepare a two-sentence answer to each before submitting.

- Why is validation not an LLM agent?
- What stops the model approving a bad invoice?
- How do you know extraction is right? What is your accuracy, and how did you measure it?
- What happens when Grok is down, slow, or returns malformed JSON?
- Why LangGraph? What would change with CrewAI or no framework?
- 1004 arrived twice. What did your system do and why?
- How does this scale to 10,000 invoices a day? What breaks first?
- What would you do in the first week on site at Acme?
- What did you cut, and what would you build next?
