# How approvals changed as each stage was built

The same 20 sample files, run through the system at each point in its construction. Every row
below was reproduced by checking out the commit named and running the folder offline
(`python main.py --invoice_path=data/invoices --offline`). It shows what each stage is for by
what it changed.

## The short version

| Stage added | Commit | What happens to the 20 files | What changed, and why |
|---|---|---|---|
| Checks and repairs only | `d6b6d14` | No decisions yet. Worst flag per file: 4 BLOCK, 6 REVIEW, 3 WARN, 5 INFO-only, 2 none | The system can say what is wrong with an invoice, not what to do about it |
| Policy floor | `994d0ec` | **8 approved, 7 held, 5 rejected.** Approved invoices total $35,265.00 | Flags become decisions. Two files move against their worst flag: 1003 and 1012 |
| Ledger and payment | `40d715d` | **6 paid ($26,325.00), 7 held, 4 rejected, 3 skipped as duplicates** | Invoices are compared with each other. $8,940.00 that the policy alone approved is not paid |
| Review queue | `460d953` | Same outcomes. The 7 held invoices now wait for a person, who can release or reject them | A hold stops being a dead end |
| Rate service | `5f324b7` | Same outcomes. 1014 is listed as 4,125.00 EUR (~$4,480.16) and is paid in euros when released | A foreign-currency invoice can be approved like any other |
| Reviewer agent, first recording | `493ac7d` | **5 paid ($19,140.00), 8 held**, 4 rejected, 3 skipped | The model added a hold to 1010. It was wrong to |
| Reviewer agent, told who the buyer is | `a5d6c6a` | **6 paid ($26,325.00), 7 held**, 4 rejected, 3 skipped | 1010 is paid again. The model had been missing context |
| Critic, first recording | `e3d4d80` | Same outcomes. 4 of the agent's 8 assessments are sent back once; all 8 end accepted | The reasons a reviewer reads are checked. No decision changed |
| Critic, after its review | `b6009c7` | Same outcomes. 1 of 8 is sent back, by a check in code; all 8 end accepted | The critic was told what it had been missing, and an agent-added hold can no longer be undone |
| Unknown products rejected | current | **6 paid ($26,325.00), 5 held, 6 rejected**, 3 skipped. The agent now reviews 6 invoices; 1 is sent back, all 6 end accepted | A rule change, not a new stage: 1008 and 1016 move from held to rejected |

## Each change, by invoice

### Policy floor: flags become decisions

Before this stage an invoice had only its flags. The policy maps them to an outcome, and for two
files that outcome is not what the worst single flag suggests.

| Invoice | Worst flag | Policy outcome | Why they differ |
|---|---|---|---|
| 1003 | REVIEW | **Rejected** | No single flag blocks it. Four fraud signals together do: an item with no stock, a due date of "yesterday", "URGENT... penalties", and a request for a wire |
| 1012 (both copies) | WARN | **Held** | Nothing on it is wrong once repaired. It is $25 under the $10,000 limit and carries other warnings; that combination is held |

The rest follow their worst flag: BLOCK is rejected (1007, 1009, 1013), REVIEW is held (1002,
1005, 1008, 1014, 1016), and warnings or notes alone are approved (1001, 1004, 1004 revised,
1006, 1010, 1011, 1015).

### Ledger: the same invoice, twice

The policy judges each file alone. Of the 8 files it approves, two should not be paid as they
stand, and the ledger is what knows it.

| Invoice | Policy alone | With the ledger | Money not paid |
|---|---|---|---|
| 1011 (txt) | Approved, $3,000.00 | Skipped: the PDF copy of the same invoice was already paid | $3,000.00 |
| 1004 revised | Approved, $5,940.00 | Held: the original was already paid $1,890.00, so only $4,050.00 can be owed | $5,940.00 held; $1,890.00 of it would have been a double payment |
| 1012 (txt) | Held | Skipped: the PDF copy is already in the review queue | One review item, not two |
| 1013 (pdf) | Rejected | Skipped: the JSON copy was already rejected | One rejection, not two |

Approved by the policy alone: $35,265.00. Paid with the ledger: $26,325.00. The difference,
$8,940.00, is 1011's second copy ($3,000.00) and 1004's revision ($5,940.00). Of that, $4,890.00
would have been money paid twice ($3,000.00 + $1,890.00); the remaining $4,050.00 is the
revision's real difference, which a person can release from the queue.

### Review queue and rate service: holds can be acted on

These two stages change no automatic outcome. They change what can happen next to the 7 held
invoices.

| Invoice | Before | After |
|---|---|---|
| Any held invoice | Recorded as held; nothing more could be done | A named person approves or rejects it; the run resumes and pays or records the rejection |
| 1004 revised | Held | Released for $4,050.00, the difference, not $5,940.00 |
| 1014 (EUR) | First: could only be rejected. Then: approvable if the reviewer typed a dollar amount | Listed with a dollar estimate from the rate service; approved with nothing to type; paid 4,125.00 EUR |

### Reviewer agent: one outcome changed, then changed back

This is the only stage where a model can move an outcome, and only from approved to held.

| Recording | 1010 | Paid | Why |
|---|---|---|---|
| First (`493ac7d`) | **Held** by the agent | 5 invoices, $19,140.00 | It asked why WidgetA had two prices, although the second line is marked "rush order", and who "Acme Corp Manufacturing Division" on the Bill To line was |
| After the fix (`a5d6c6a`) | **Paid**; the agent agrees with the policy | 6 invoices, $26,325.00 | Its instructions now say it works for Acme Corp, the buyer, and to read a line's note before asking about that line |

The agent's hold was a mistake, and the cause was in what it had been given: nothing told it
which company it was reviewing for. An earlier recording had a second fault of the same kind,
which changed no outcome: a lookup tool listed the invoice under review in the vendor's history,
and the agent suspected four invoices of being duplicates of themselves.

On the other seven invoices it reviews, the agent has recommended a hold in every recording,
matching the policy. It has never recommended paying something the policy held.

### Critic: the reasons are checked, the decisions are not touched

| | First recording | After its code review |
|---|---|---|
| Assessments the agent wrote | 8 | 8 |
| Sent back to the agent once | 4 | 1 |
| ...by a check in code (a flag code in the text) | 2 (1014, 1016) | 1 (1010) |
| ...on the model critic's objection | 2 (1002, 1010) | 0 |
| Accepted by the final pass | 8 | 8 |
| Outcomes changed | 0 | 0 |

The model critic's objections in the first recording did not hold up. On 1010 it said the
sentence "the bill-to field is an expected Acme division name" contradicted the
unexpected-fields flag. It doesn't: that flag means the field has no place in the invoice
structure, not that it is suspicious. On 1002 it said the question "should the order for 20
GadgetX proceed given only 5 in stock?" was already answered by the stock flag, which states the
shortfall but not what to do about it. A second recording produced two more of the same kind.

Each one traced to something the critic had not been told: what a flag means, who the buyer is,
that a vendor's own claim is not confirmation. Those are in its instructions now, each flag is
sent with its meaning, and in the current recording the model critic objects to nothing. The one
assessment sent back is caught by code.

The code review also changed a rule. The critic could previously lead the agent to withdraw a
hold it had added, on the model critic's word alone. Now a hold the agent adds stays, whatever
the critic says and even if the model fails mid-revision; a person releases it.

### Unknown products: from held to rejected

A decision about the rule, made after the pipeline was complete. A product that isn't in
inventory is one Acme doesn't stock, so there is nothing to check the line against and no way to
fulfil it. Holding such an invoice asked a person a question with one answer.

| Invoice | Before | After |
|---|---|---|
| 1008 (SuperGizmo, MegaSprocket) | Held; the agent looked for similar products and found none | **Rejected** by the policy. No model is called |
| 1016 (WidgetC) | Held; the agent found WidgetA and WidgetB and asked whether WidgetC was a new product | **Rejected** by the policy. No model is called |

What it costs: a misspelled name in a structured file ("WidgtA") is now rejected too, where
before a person could have waved it through. Spacing, hyphen and case differences are still
repaired first, so "Widget A" is unaffected. It also shows what self-correction is worth on 1012:
without the name repair its two scanner-damaged product names would now reject the invoice, and
with it the invoice is held on its other grounds.

The figures in the sections above are as they were at each commit. Today the agent reviews six
invoices (the five holds and 1010), and the critic sends one back.

## What stayed the same throughout

- **The four rejected invoices never changed** after the policy was written: 1003, 1007, 1009,
  1013. No later stage, and no model, touched them.
- **No stage ever paid something an earlier stage had held or rejected.** Every change after the
  policy either held more, or let a named person release a hold.
- **Paid invoices: 1001, 1004, 1006, 1011, 1015 in every version** since payment existed. 1010 is
  the only one that moved.

## How to say it

Each stage narrowed what gets paid without a person, or widened what a person can do: the policy
turned findings into decisions, the ledger stopped $4,890.00 being paid twice, the queue gave
holds an owner, and the model was allowed to add caution and explain, never to approve.

## What not to claim

- That the agent improved a decision. On these 20 files its one change was an error, since
  corrected. Its contribution is the written reason and the lookups behind it.
- That the model critic is reliable. Across three recordings it has not found a real problem, and
  it found four that weren't until its instructions were corrected. The checks in code are the
  part to rely on.
- That these counts generalise. Twenty files, built to be awkward.
