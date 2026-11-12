# Pipeline

Part of the reference documentation. Start with the [README](../README.md).

`pipeline/graph.py` wires the stages together as a [LangGraph](https://langchain-ai.github.io/langgraph/)
graph. Each stage is a node; the dotted arrows are branches chosen from the invoice's state.

The diagram is in the [README](../README.md#how-it-works).

| Node | What it does | Calls |
|---|---|---|
| `ingest` | Reads the file into an `Invoice`: a parser for JSON, CSV and XML, the model for TXT and PDF | `normalizer`, `extraction` |
| `validate` | Repairs what the invoice proves, runs every check against the inventory, grades each flag | `normalizer.check` |
| `critic` | Checks the agent's assessment, in code and with a second model call. Problems send it back to `reviewer`, two rounds at most | `approval.critique` |
| `register` | Asks the ledger whether this invoice was seen before, and records it if new | `ledger.Ledger` |
| `skip` | The invoice was already handled, as this file or another copy: nothing is decided or paid | |
| `approve` | The policy floor: the minimum outcome the flags require | `approval.decide` |
| `reviewer` | The reviewer agent looks into holds and warned approvals with lookup tools, writes the reason, and may turn an approval into a hold | `approval.assess` |
| `pay` | Pays through the ledger, which pays any one invoice once | `ledger.Ledger.pay` |
| `hold` | Puts the invoice in the review queue and records it as held | `ledger.Ledger.enqueue` |
| `await_review` | Pauses the run until a person decides, then sends it to `pay` or `reject` | LangGraph `interrupt` |
| `reject` | Records the rejection and its reasons, the policy's or the reviewer's | |
| `failed` | The file couldn't be read, so there is nothing to decide | |

- **One state per invoice.** A run starts with the file path. Each node returns only what it
  adds (the invoice, the decision, the result) and LangGraph merges it in.
- **The nodes are thin.** Each calls one function from `normalizer`, `extraction`, `approval` or
  `ledger`, and none of those packages imports LangGraph. The graph only decides what runs next.
- **Nodes that change the world are safe to repeat.** LangGraph may run a node again after a
  crash or a resume, so `register` and `pay` rely on the ledger: recording is an upsert, and a
  payment carries a key the ledger accepts once.
- **Nodes don't change what they are given.** `validate` and `register` add flags to a copy of the
  invoice and return it, so running a node again on the same state gives the same result.
- **Branches read the state, they don't decide.** The outcome is set by the `approve` node; the
  branch after it only looks at that outcome and picks a node.
- **A trail of steps.** Every node appends one entry saying what it did. It comes back in the
  output as `trail`, and `--trace` prints the entries live:

```
{"file": "invoice_1003.txt", "node": "ingest", "method": "llm", "model_attempts": 1}
{"file": "invoice_1003.txt", "node": "validate", "flags": ["UNPARSEABLE", "UNEXPECTED_FORMAT", ...]}
{"file": "invoice_1003.txt", "node": "approve", "outcome": "REJECT", "rule": "FRAUD_SIGNALS", "because": [...]}
{"file": "invoice_1003.txt", "node": "reject", "status": "REJECTED", "reasons": [...]}
```

- **A held invoice is a paused run.** The graph is compiled with a checkpointer, which saves the
  state after every node under a thread ID (in `checkpoints.db`). `await_review` calls LangGraph's
  `interrupt()`, which stops the run there. `review.py`, in another process and possibly days
  later, resumes that thread with a person's decision, and the run continues into `pay` or
  `reject`. See [Review queue](ledger-and-review.md#review-queue).

Why a graph and not a script: a run that stops for a person and is resumed later by another
process is exactly what a script handles badly, and a graph with checkpoints handles by design.
The reviewer and critic loop still to come is an edge pointing backwards, also added in this one
file.
