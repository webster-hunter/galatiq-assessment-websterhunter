# 01 — Situation Assessment

What this take-home is really testing, what is actually in the data, and what the brief leaves unsaid.
Every number below was recomputed from the files in `data/invoices/`, not taken from the README.

## 1. The one-paragraph read

The brief says "build a multi-agent invoice pipeline." The data says something narrower: **can you turn
inconsistent inputs into decisions that are safe to pay on?** Of 16 distinct invoices, only 5 are clean.
The other 11 carry multiple defects each, and several of the worst ones are *not* listed in the README's
scenario table. A system that demos nicely on INV-1001 and trusts the README's list will pay invoices
it should not. The winning submission treats inconsistency as the product, not as an edge case.

## 2. What the evaluator is looking for

Three sources, read together:

| Source | Signal |
|---|---|
| README evaluation criteria | Functionality, code quality, agentic sophistication, shipping mindset, presentation, above/beyond, UI/UX |
| JD | "Imperfect data", "error recovery, observability, continuous-improvement loop", "ruthlessly cut scope", "technical face with senior executives", "not pure prompt engineers" |
| Git history of this repo | See below |

Things the git history reveals that the current README does not:

- **A removed "Deliverables" section** (commit `39d465d`) asked for `demo_script.md` (architecture
  diagram, *run logs for all sample invoices*, edge cases handled) and `presentation.md` (a 5–10 minute
  **live demo** script with business impact). It was removed, not contradicted. Treat it as a description
  of the next interview stage: you will very likely demo this live and defend it.
- **"grader reads GitHub repos"** (commit `5d97e97`): "our review tooling reads GitHub repositories
  directly." Inference, not fact: the first pass is probably tool-assisted and may never execute your
  code. So the repo must be legible *by reading*: a README that leads with outcomes, committed run
  output for every sample invoice, and tests whose names state the behaviour.
- "Above/Beyond" and "UI/UX" were added later (Feb 2026), which suggests earlier submissions were
  functionally fine but generic. Differentiation matters.

## 3. Data audit

### 3.1 Inventory

20 files, 16 distinct invoice numbers, 5 formats, at least 9 distinct layouts.

| Format | Files | Layout notes |
|---|---|---|
| TXT | 1001, 1002, 1003, 1008, 1010, 1011, 1012 | Five layout families: labelled key/value, abbreviated labels, email wrapper, column tables, spaced-out OCR style. Vendor appears as `Vendor:`, `Vndr:`, or `FROM:`; 1010 and 1012 also name the buyer (`Bill To`, `TO:`), which a careless extractor returns as the vendor |
| JSON | 1004, 1004_revised, 1005, 1009, 1013, 1016 | Same schema; `amount` per line present only in 1013 and 1016; `revision`/`notes` optional |
| CSV | 1006, 1007, 1015 | **Two different shapes**: 1006 is a `field,value` list with repeated `item` keys; 1007/1015 are one row per line item with footer rows for totals |
| PDF | 1011, 1012, 1013 | Text-based (no OCR needed). Each duplicates another file |
| XML | 1014 | **Not mentioned in the README**, which lists only PDF, CSV, JSON, TXT |

### 3.2 Per-invoice findings

Stock reference: WidgetA 15, WidgetB 10, GadgetX 5, FakeItem 0.
"In README" = the README's scenario table tells you about it.

| Invoice | Stated total | Findings | In README? |
|---|---|---|---|
| 1001 (txt) | $5,000.00 | Clean. Due date is 17 days out against "Net 15"; within tolerance, see note below | Listed as clean |
| 1002 (txt) | $15,000.00 | Abbreviated/misspelled labels ("INVOCE", "Vndr", "Amt"); invoice number is bare `1002`; mixed date formats in one file; GadgetX 20 > 5 in stock; over $10K; due date equals invoice date although terms say Net 30 | Stock only |
| 1003 (txt) | $100,000.00 | Vendor "Fraudster LLC"; item FakeItem (0 stock); due date is the string `yesterday`; pressure language ("URGENT", "avoid penalties"); "wire transfer preferred"; terms "Immediate"; over $10K | Yes |
| 1004 (json) | $1,890.00 | Clean on its own. Superseded by the revision below | Listed as clean |
| 1004_revised (json) | $5,940.00 | **Same invoice number**, `revision: R1`, adds GadgetX ×5 (exactly equal to stock). Paying both = double payment of $1,890 | **No** |
| 1005 (json) | $15,225.00 | GadgetX 8 > 5 in stock; WidgetB 10 = stock exactly; over $10K | **No** |
| 1006 (csv) | $2,750.00 | Clean. Key/value CSV with repeated keys — a naive `dict()` load silently keeps only the last item | Listed as clean |
| 1007 (csv) | $15,525.00 | US dates (`01/28/2026`); WidgetA 20 > 15; WidgetB 15 > 10; **total is wrong**: 14,750 + 885 tax = 15,635, stated 15,525 (short by $110); over $10K | **No** |
| 1008 (txt) | $9,900.00 | Wrapped in an email (From/To/Subject); both items unknown; **$100 under the $10K threshold**; 10-day due date; sender domain `noproduct.biz` does not match the vendor name; no terms, subtotal or tax | Unknown items only |
| 1009 (json) | −$250.00 | Vendor name empty; address null; due date null; quantity −5; negative total; stated subtotal (1,000) disagrees with line items (−250); terms empty | Negative qty only |
| 1010 (txt) | $7,185.00 | Arithmetic is correct. Same SKU twice: "WidgetA (rush order)" at $300 vs $250 (a 20% premium); a Shipping line no other invoice has; tax given as amount with no rate; long-form dates | **No** |
| 1011 (txt + pdf) | $3,000.00 | Clean. Same invoice in two files; the PDF omits subtotal, tax and payment terms | **No** (duplicate) |
| 1012 (txt + pdf) | $9,975.00 | OCR-style damage: letter O for zero in `2O26` and `$3,500.O0`; "Widget A"/"Gadget X" with spaces; `INV 1012` without hyphen; "Distributers", "Payble"; vendor "(formerly FastShip Ltd.)"; **$25 under the $10K threshold**. Arithmetic reconciles once repaired; all quantities within stock. Same invoice in two files | **No** |
| 1013 (json + pdf) | $22,562.80 | **Each line passes a stock check; the invoice does not.** Aggregated: WidgetA 22 > 15, WidgetB 18 > 10, GadgetX 9 > 5. **Total padded by $50**: 21,040 + 1,472.80 = 22,512.80. The generator script adds the $50 on purpose (`grand_total + 50`). Discounted prices on two lines; over $10K. Same invoice in two files | **No** |
| 1014 (xml) | 4,125.00 **EUR** | Only non-USD invoice. Unit prices are 225 and 475 where every other invoice has 250 and 500, but those are EUR figures, so they cannot be compared without a rate; arithmetic correct; stock fine | **No** |
| 1015 (csv) | $6,500.00 | Clean. Same shape as 1007 but ISO dates | — |
| 1016 (json) | $3,233.00 | WidgetC not in inventory. Vendor "Widgets Inc." matches 1001, a vendor with a clean history | Yes |

Notes on the table:

- **Payment terms against dates.** A strict "Net N means exactly N days" check fires on 8 of the 11
  invoices that state terms (17 against Net 15, 31 against Net 30, 59 against Net 60), including four
  clean ones. Only 1002 is a real contradiction (0 days against Net 30). Use a tolerance.
- **Every due date is in the past** relative to today. Anything that reasons about "overdue" or
  resolves `yesterday` needs an injected clock, or every invoice looks late.
- **Lesser oddities, worth noticing but not blocking on:** addresses on 1004 and 1005 are famous
  fictional or public ones (742 Evergreen Terrace; 1600 Pennsylvania Ave), and 1004 is listed by the
  README as clean; 1006's vendor shares the buyer's name ("Acme"); tax rates range from 0% to 10%
  across vendors; 1012 carries the only PO reference (`PO-20260115`), and 1004 R1 cites a "PO
  amendment" with no number and the same date as the original.

### 3.3 The traps, ranked by how likely they are to catch a submission

1. **Aggregate stock (1013).** Per-line validation passes at the exact boundary on all three SKUs. Only
   summing by SKU first catches it. This is the most deliberately constructed trap in the set.
2. **Totals that do not add up (1007, 1013).** Both look plausible and both are over $10K. A system that
   extracts "the total" and trusts it never notices.
3. **Duplicates and revisions (1004, 1011, 1012, 1013).** Run the pipeline over the whole folder and a
   naive system pays INV-1004 twice and three other invoices twice each. Processing in file-name order
   does not help: `invoice_1011.pdf` sorts before `invoice_1011.txt`. The duplicate exposure is
   1,890 (original 1004) + 3,000 (1011) + 9,975 (1012) + 22,562.80 (1013) = **$37,427.80**, in a
   20-file sample. That is the exposure for a system with no other checks. $22,562.80 of it is 1013,
   which should be rejected anyway; on invoices that would otherwise be paid, duplicate detection alone
   saves $4,890 (1004 and 1011).
4. **Threshold skirting (1008 at $9,900, 1012 at $9,975).** The README plants a $10K rule and two
   invoices sit just below it. Whether that is a fraud signal is a policy decision, but noticing is not
   optional.
5. **Repeated-key CSV (1006).** Silently drops WidgetA and under-validates. No error is raised.
6. **Currency (1014).** `4125.00` compared against a USD threshold without conversion is a silent bug.
7. **Injection surface (1003, 1008, 1012 notes).** Invoice text is untrusted input that you are about to
   paste into an LLM prompt. "URGENT — pay immediately" is a mild version of what a real attacker writes.

### 3.4 Classes of inconsistency

This taxonomy is the backbone of the design: each class needs a different mechanism.

| Class | Examples | Right mechanism |
|---|---|---|
| **Structural** — same data, different container | 5 formats, 2 CSV shapes, email wrapper, XML | Deterministic parsers where structure exists; LLM extraction only for free text |
| **Lexical** — same value, different spelling | `INV 1012` / `1002` / `INV-1001`; "Widget A"; four date formats; `$250` vs `250.00` | Normalisers with the raw value preserved |
| **Corruption** — value damaged in transit | `2O26`, `3,500.O0`, "Vndr", "INVOCE" | Repair, then *prove* the repair with an independent check (7 × $500 = $3,500 confirms the reading) |
| **Semantic** — value present, meaning unclear | `yesterday`, EUR, "rush order" SKU, shipping, tax with no rate | Explicit policy; route to a person when policy is silent |
| **Internal contradiction** — file disagrees with itself | 1007 and 1013 totals; 1009 subtotal; Net 30 with same-day due date | Recompute everything; never trust a stated aggregate |
| **Cross-record** — file disagrees with other files | Revision of 1004; three format duplicates | A ledger keyed on normalised invoice number + revision |
| **Reference mismatch** — file disagrees with systems of record | Unknown SKUs, quantity over stock, off-catalogue prices | Tool calls against the database, aggregated per SKU |
| **Adversarial** — crafted to get paid | 1003 pressure language; threshold skirting; padded totals | Risk signals feeding approval; never let invoice text act as instructions |

## 4. What the brief gets wrong or leaves open

An FDE is expected to notice these and state an assumption rather than wait for an answer.

| Issue | Why it matters | Recommended assumption |
|---|---|---|
| Run command references `data/invoices/invoice1.txt`, which does not exist | A reviewer copy-pasting the README command gets an error unless you handle it well | Support the documented flag exactly; give a helpful error listing available files; also accept a directory |
| "No internet for external APIs" alongside "use Grok via the xAI API" | Most likely means "mock the business systems", but it is ambiguous, and a reviewer without a key cannot run an LLM-only system | Provider abstraction with an offline mode (recorded responses) so the full pipeline runs with no key. State which mode produced the committed logs |
| The Grok snippet (`from xai import Grok`, model `grok-3`, URL `grok.x.ai`) | Does not match the published SDK or API host as far as I can tell; xAI exposes an OpenAI-compatible endpoint | Verify against docs.x.ai before coding; make model name configuration, not code |
| "Validate against inventory stock" for a *vendor* invoice | In real accounts payable you match an invoice to a purchase order and a goods receipt, not to stock on hand | Treat `stock` as a stand-in for "quantity we can confirm receiving." Say so, and name three-way match as the production version |
| Does stock decrement across invoices? | README says 1001, 1004 and 1006 all pass, but together they need 18 WidgetA against 15 | Stock checks are per invoice and do not decrement. State it explicitly |
| What "requires additional scrutiny" over $10K means | It is the only approval rule given | Define a small written policy (section 5 of the plan, "Approval policy") and make it data, not prompt text |
| No vendor master, no price list, no PO data | Fraud and price checks need a reference | Add small `vendors` and `catalog` tables as a stated extension; the README invites this |
| Only approve or reject is described | Half the sample is neither clearly payable nor clearly fraudulent | Add a third outcome: **hold for human review**. This is the most important design decision in the project |

## 5. Business framing

What the client said: $2M/year, 30% error rate, 5-day delays. What the sample shows:

- 5 of 16 invoices (31%) are clean and can be paid without a person touching them.
- 1 more (1010) is payable with a recorded warning. That takes the touchless rate to 6 of 16 (38%).
- 6 should be held for a person and 4 rejected. The value there is not removing the person; it
  is handing them a pre-analysed exception with evidence instead of a raw PDF.
- Four duplicate files are caught; naive duplicate exposure in this small sample is $37,427.80.

The honest pitch is therefore not "we automate invoices." It is: *clean invoices go from 5 days to
seconds, every exception arrives pre-diagnosed, and nothing gets paid twice.* The asymmetry to hold onto:
a wrongly held invoice costs minutes; a wrongly paid one costs the invoice amount. The system should be
tuned so that false approvals are effectively zero, and judged on touchless rate second.

Do not extrapolate the sample percentages to the $2M figure in your presentation. The sample is built to
be hard; present the percentages as what the prototype did on the test set and say what you would
measure in week one on real volume.

## 6. Risks to your submission

| Risk | Mitigation |
|---|---|
| Over-building agents, under-building correctness | Deterministic core first; agents added where judgment is needed. But do not under-build them either: the model must visibly call tools and make decisions (plan section 4) |
| LLM non-determinism during a live demo | Offline/recorded mode; low temperature; committed logs as backup |
| LLM approves something the rules forbid | Rules set a floor. The model may be stricter than policy, never looser |
| Reviewer cannot run it | One-command setup, no key required, logs committed |
| Scope creep into UI | CLI with clear output first; review-queue UI only after the pipeline is green on all 20 files |
| Optimising for a repo-reading grader and neglecting that it runs | The inference in section 2 is a reason to commit logs, not a reason to skip end-to-end testing |
| Being unable to defend a choice | Keep a short decisions log (what, why, what you cut) as you go |
