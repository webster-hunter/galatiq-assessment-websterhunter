# Approval: policy, reviewer agent and critic

Part of the reference documentation. Start with the [README](../README.md).

## Approval policy

`approval/policy.py` holds the policy floor: the minimum outcome an invoice's flags require. It is
an ordered table of rules, in code, and the first rule that matches decides. It never calls a
model, so the same flags always give the same decision, and it can be read and changed by the
people who own it without touching a prompt.

| Rule | Condition | Outcome |
|---|---|---|
| `BLOCKING_FLAG` | Any BLOCK flag | REJECT |
| `FRAUD_SIGNALS` | Two or more fraud signals, at least one of them a fact and not wording | REJECT |
| `NEEDS_REVIEW` | Any REVIEW flag | HOLD |
| `FRAUD_WORDING` | Pressure wording and a wire request together, with nothing else | HOLD |
| `NEAR_THRESHOLD_WITH_WARNINGS` | Total within 2% below $10,000, plus any other warning | HOLD |
| `NO_OBJECTION` | None of the above | APPROVE, with any warnings listed |

- **Fraud signals** are four kinds of flag, each weak alone. Two are facts about the invoice: an
  item with no stock at all, and a due date that isn't a date (`yesterday`). Two are wording:
  pressure to pay fast (`PRESSURE_LANGUAGE`) and a request to pay by wire or to a changed account
  (`WIRE_REQUEST`). One signal is weighed as an ordinary flag. Two different kinds reject, but
  only if at least one is a fact: honest invoices ask for bank transfers and mention deadlines,
  so wording alone can hold an invoice for a person and never rejects it. The wording checks are
  narrow keyword matches in code ("late fees apply" is not pressure; "avoid penalties" is), so a
  rejection never depends on a model noticing something.
- **INFO notes are never a reason** for any outcome.
- **It is a floor.** The [reviewer agent](approval.md#reviewer-agent) may make an outcome stricter, never looser.

Each decision records the rule that fired and the flags that caused it:

```json
{"outcome": "REJECT", "rule": "FRAUD_SIGNALS",
 "because": ["STOCK_EXCEEDED", "UNPARSEABLE", "PRESSURE_LANGUAGE", "WIRE_REQUEST"],
 "warnings": [], "stage": "POLICY_FLOOR"}
```

### Decisions on the samples

| Outcome | Invoices | Why |
|---|---|---|
| APPROVE | 1001, 1004, 1004 revised, 1006, 1011 (txt and pdf), 1015 | Nothing to object to |
| APPROVE | 1010 | Warnings only (a repeated item, a Bill To line), on record |
| HOLD | 1002, 1005 | Over stock, and $10,000 or more |
| HOLD | 1016 | A product not in inventory whose name is close to ones that are |
| HOLD | 1014 | Billed in EUR |
| HOLD | 1012 (txt and pdf) | $25 under the threshold, with repaired values and other warnings |
| REJECT | 1003 | Four fraud signals: FakeItem has no stock, due date `yesterday`, "URGENT... penalties", "Wire transfer preferred" |
| REJECT | 1007, 1013 (json and pdf) | The stated total doesn't match the lines |
| REJECT | 1009 | Negative quantity and total, no vendor, wrong subtotal |
| REJECT | 1008 | Products not in inventory, with no similar name in inventory |

The policy decides each invoice on its flags alone. What happens across files (a second copy, a
revision) is the ledger's job, below.

## Reviewer agent

`approval/reviewer.py` is the model's part in approval. The policy has already decided; the agent
adds what code can't: it follows a flag up with a lookup, and writes the reason in plain language.

**It runs a tool-calling loop.** The model is sent the invoice, its flags, the policy floor and
three tools. It may ask for tools; the code runs them and sends back the results; when it
answers, that is its assessment: a recommendation, a short rationale, and questions for a reviewer.

| Tool | Returns |
|---|---|
| `get_stock(item)` | How many of a product are in stock, or that it isn't an inventory product |
| `find_similar_products(name)` | Up to three inventory products with similar names, and their stock. The same matching decides in code whether an unknown product is held or rejected |
| `get_vendor_history(vendor)` | The vendor's other invoices on record, with totals and outcomes |

All three only read. The model names a tool and arguments; the code decides whether and how to
run it, and answers an unknown tool or bad arguments with an error the model can read.

**Its authority is fixed in code, not requested in the prompt:**

| Policy floor | What the agent can do |
|---|---|
| APPROVE with warnings | Add a HOLD. The only way it changes an outcome; even a "reject" from it only holds. Once added, the hold stays: nothing later in the run can remove it, only a person |
| HOLD | Advise the person who decides. Its recommendation is shown in the review queue, and is never looser than the hold: an "approve" from it is recorded as HOLD, with a note |
| REJECT, or a clean APPROVE | Nothing: it isn't called, and the reason is written from the flags |

So a model can make the system stricter, never looser. If the model can't be reached, or answers
unusably, the policy's decision stands and says so. A lookup that fails costs the agent that
fact, not the batch its run.

**Vendor text is always inside a delimited block.** It is not only in the invoice: a flag quotes
it (an unknown product's name), and a lookup echoes it. The invoice goes in `<invoice>`, the flags
in `<flags>`, lookup results in `<lookups>`, and none can be closed from inside. Each flag is sent
with its meaning in a sentence (`normalizer.MEANING`), since a code's name alone invites
misreading. If the instructions fail entirely, the worst outcome is still an extra hold.

### The critic

`approval/critic.py` checks each assessment the agent writes before it stands, in two layers.

**Checks in code**, which give the same verdict every time:

| Check | Catches |
|---|---|
| `UNGROUNDED_NUMBER` | A figure in the rationale or a question that is nowhere in the invoice, the flags or a lookup result: an invented amount, or a sum or percentage the agent worked out |
| `FLAG_CODE_IN_TEXT` | A flag code in text a person reads |
| `FLAG_NOT_ADDRESSED` | A flag the checks raised that the agent's `flags_addressed` list leaves out |

**A second model call**, for what code can't judge: a statement the evidence doesn't support, a
question the evidence already answers, a hold whose only reasons the invoice explains. It must
quote the words it objects to; a complaint about text that isn't in the assessment is dropped.

Any problem sends the assessment back to the agent with the specific complaints. In the graph
that is the `critic -> reviewer` edge, the one edge that points backwards. A counter in the
state ends the loop after two critic passes; an assessment still not accepted goes forward
marked as unverified, and the review screen says so.

The critic changes no outcome. It checks the reason, and cannot undo a hold the agent added: if
the agent withdraws a hold after the critic's objections, the invoice is held all the same, with
a note saying so, for a person to release. A second model's objection is not a check.

Three things keep the critic itself honest:

- **The code checks come first and count on their own.** A model that accepts everything can't
  wave through an invented figure.
- **Figures are grounded on what a lookup found, not on what the agent asked.** Calling
  `get_stock("9999")` does not make "9999 complaints" evidence.
- **What the model critic said is recorded as it said it.** An objection whose quote can't be
  matched to the text doesn't send the assessment back, but it is kept on the record.

### What they did on the samples

From recorded replies (`grok-4`), on the folder run in order. The agent reviewed seven invoices:
the six policy holds, and 1010. On all seven it agreed with the policy's outcome.

- **1010: it agreed the invoice is payable, and said why.** The second WidgetA line's higher
  price carries the vendor's "rush order" note, and it is billed to an Acme division. No hold.
- **1016:** it looked for products like WidgetC (WidgetA and WidgetB), and asks whether to accept
  WidgetC as a new product.
- **1005:** it looked up the vendor's history, found no earlier invoice, and asks whether this
  new vendor has been approved.
- **1004 revised:** it found the original in the vendor's history, paid $1,890, explains that
  this revision adds $4,050, and asks whether the PO amendment authorises it.

The critic accepted six of the seven on the first pass. It sent 1010's back once, on the check
in code: two flag codes had been written into its text. The revision describes them in words and
was accepted. The model critic objected to none.

That is the third recording of the critic, and the first two are the instructive ones. In each,
the model critic sent two assessments back on objections that didn't hold up: that "an expected
Acme division name" contradicted the unexpected-fields flag; that a question about confirming a
PO amendment was answered by the vendor's own note claiming one. Each traced to something the
critic had not been told: what a flag means, who the buyer is, that a vendor's claim is not
confirmation. With those in its instructions it stopped. The checks in code never needed fixing.

Two faults were found earlier by reading recorded replies, and neither was the model's:

- **A tool misled it.** Vendor history listed the invoice under review, which is in the ledger
  by then, and the model took it for an earlier duplicate on four invoices. The tool now returns
  the vendor's other invoices only.
- **It didn't know who it worked for.** Its instructions never named the buyer, so it held 1010
  over "Bill To: Acme Corp Manufacturing Division", and asked why WidgetA had two prices although
  the line said "rush order". The instructions now name Acme Corp as the buyer and say to read a
  line's note before asking about that line.

[`design-and-demo.pdf`](design-and-demo.pdf) shows how the 20 outcomes changed as
each stage was added, including the agent's mistaken hold.

Limits:

- The model critic is the weak part: see above. It found nothing real on these files and, before
  its instructions were corrected, twice found things that weren't. What is dependable is narrow:
  figures, flag codes, and the flag checklist.
- The flag checklist can be satisfied by listing a code without discussing it. It catches a flag
  forgotten, not a flag brushed aside.
- A statement with no figure in it ("the vendor is well established") is checked only by the
  model critic.
- The model's choices vary between recordings: which tools it calls changed with each prompt
  edit. The tests pin the recorded behaviour, so a change shows up as a failure to look at.
- Recordings cover the folder run. An invoice run on its own gets a different history from the
  tools, has no recording, and offline falls back to the policy's wording.
- Whether a 20% rush premium should be paid without a person seeing it is a policy question the
  agent's agreement doesn't settle. There is no price list or purchase order to check it against.
