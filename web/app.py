"""The review queue over HTTP, for the page in web/static.

    GET  /api/queue                 what is waiting, what was decided, and what has been paid
    GET  /api/queue/{id}            one held invoice in full
    POST /api/queue/{id}/decision   {"action": "approve" | "reject", "reviewer": "...", "note": "..."}

This adds no rules of its own. Listing reads the ledger and the saved runs; deciding calls
pipeline.release, so what may be approved, what is paid and
what is recorded are decided in one place.

Every request opens the ledger and the saved runs afresh and closes them after. SQLite
connections belong to the thread that opened them, and the server answers each request on
whichever worker thread is free.

There is no login. The server listens on this machine only, and a reviewer is whoever types
their name.
"""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from extraction import Replay
from ledger import Ledger
from ledger.ledger import currency_of
from normalizer.__main__ import _json_default
from normalizer.severity import MEANING
from pipeline import build_graph, cannot_approve, checkpointer, conversion, release
from pipeline.graph import _thread
from rates import MockRates

STATIC = Path(__file__).parent / "static"


class Decision(BaseModel):
    action: str
    reviewer: str
    note: str | None = None


def _plain(value):
    """Decimals and dates as strings, so the value can be sent as JSON."""
    return json.loads(json.dumps(value, default=_json_default))


def _pays(invoice) -> dict:
    """What approving this invoice would pay: its total, or for a revision of a paid invoice, the difference."""
    revision = next((f for f in invoice.flags if f["code"] == "REVISION_OF_PAID"), None)
    if revision:
        return {"amount": revision["difference"], "currency": currency_of(invoice), "already_paid": revision["already_paid"]}
    return {"amount": invoice.total, "currency": currency_of(invoice), "already_paid": None}


def _summary(entry: dict, state: dict, rates) -> dict:
    """One row of the list. `state` is the paused run's saved state, or {} if it was not found."""
    invoice = state.get("invoice")
    estimate = conversion(invoice, rates) if invoice is not None else None
    advice = state.get("assessment") or {}
    return {
        "id": entry["id"], "file": entry["source_file"], "vendor": entry["vendor"], "total": entry["total"],
        "currency": currency_of(invoice) if invoice is not None else "USD",
        "usd_estimate": estimate["usd_estimate"] if estimate else None,
        "reasons": [r for r in (entry["reasons"] or "").split(", ") if r],
        "recommendation": advice.get("recommendation") if advice.get("by") == "reviewer_agent" else None,
        "has_saved_run": invoice is not None,
    }


def _detail(entry: dict, state: dict, rates) -> dict:
    """Everything a reviewer needs to decide one invoice."""
    invoice = state["invoice"]
    advice = state.get("assessment")
    flags = [{"code": f["code"], "severity": f["severity"], "meaning": MEANING.get(f["code"], ""),
              "detail": {k: v for k, v in f.items() if k not in ("code", "severity")}} for f in invoice.flags]
    return {
        **_summary(entry, state, rates),
        "status": entry["status"], "invoice_number": invoice.invoice_number, "date": invoice.date, "due_date": invoice.due_date,
        "payment_terms": invoice.payment_terms, "notes": invoice.notes,
        "subtotal": invoice.subtotal, "tax_amount": invoice.tax_amount, "shipping": invoice.shipping,
        "line_items": [{"item": li.item, "quantity": li.quantity, "unit_price": li.unit_price, "note": li.note}
                       for li in invoice.line_items],
        "flags": flags, "pays": _pays(invoice), "cannot_approve": cannot_approve(invoice, rates),
        "conversion": conversion(invoice, rates),
        "advice": advice and {
            "by": advice["by"], "recommendation": advice.get("recommendation"), "rationale": advice["rationale"],
            "open_questions": advice.get("open_questions", []), "note": advice.get("note"),
            "lookups": [{"tool": t["tool"], "arguments": t["arguments"], "result": t["result"]} for t in advice.get("tools_used", [])],
            "verified": (state.get("critique") or {}).get("verdict") == "ACCEPT",
        },
        "trail": state["trail"],
    }


def create_app(ledger_path="ledger.db", checkpoints_path="checkpoints.db", rates_path="rates.db") -> FastAPI:
    """The review application.

    Args:
        ledger_path: The ledger main.py wrote.
        checkpoints_path: The saved runs main.py wrote. A held invoice can only be decided with these.
        rates_path: The exchange-rate table. Without one, a foreign-currency invoice shows no dollar estimate.
    """
    app = FastAPI(title="Invoice review", docs_url="/api/docs", redoc_url=None)

    @contextmanager
    def session():
        """The ledger, the graph over the saved runs, and the rates, for one request."""
        for path, what in ((ledger_path, "ledger"), (checkpoints_path, "saved runs")):
            # Opening a path that doesn't exist would create an empty file there, and every held
            # invoice would then look as if its run were lost.
            if not Path(path).exists():
                raise HTTPException(503, f"No {what} at {path}. Process some invoices with main.py first, "
                                         "and start this server with the same --ledger and --checkpoints.")
        try:
            rates = MockRates(rates_path)
        except sqlite3.Error:
            rates = None
        ledger, saver = Ledger(ledger_path), checkpointer(checkpoints_path)
        try:
            # The nodes left to run on a held invoice (pay or reject) use neither the model nor the inventory.
            yield ledger, build_graph(Replay("data/replays"), None, ledger, saver, rates), rates
        finally:
            ledger.close()
            saver.conn.close()

    def saved(graph, entry) -> dict:
        return graph.get_state(_thread(entry["thread_id"])).values

    @app.get("/api/queue")
    def queue():
        with session() as (ledger, graph, rates):
            stuck = {e["id"] for e in ledger.decided_not_finished()}
            everything = ledger.queue(status=None)
            waiting = [_summary(e, saved(graph, e), rates) | {"stuck": e["id"] in stuck and {"action": e["action"], "reviewer": e["reviewer"]}}
                       for e in everything if e["status"] == "WAITING" or e["id"] in stuck]
            decided = [{"id": e["id"], "file": e["source_file"], "vendor": e["vendor"], "total": e["total"], "status": e["status"],
                        "action": e["action"], "reviewer": e["reviewer"], "note": e["note"], "amount": e["amount"]}
                       for e in everything if e["status"] != "WAITING" and e["id"] not in stuck]
            return _plain({"waiting": waiting, "decided": decided[::-1], "paid": ledger.paid_totals(),
                           "payments": len(ledger.payments())})

    @app.get("/api/queue/{queue_id}")
    def one(queue_id: int):
        with session() as (ledger, graph, rates):
            entry = ledger.queue_entry(queue_id)
            if entry is None:
                raise HTTPException(404, f"No queue number {queue_id}.")
            state = saved(graph, entry)
            if not state:
                raise HTTPException(409, f"{entry['source_file']} has no saved run under these checkpoints, so it can't be shown "
                                         "or decided here. Start the server with the --checkpoints file main.py used.")
            return _plain(_detail(entry, state, rates))

    @app.post("/api/queue/{queue_id}/decision")
    def decide(queue_id: int, decision: Decision):
        with session() as (ledger, graph, rates):
            try:
                state = release(graph, ledger, queue_id, decision.action, decision.reviewer, decision.note, rates=rates)
            except ValueError as e:
                raise HTTPException(400, str(e))
            return _plain({"file": state["invoice"].source_file, "result": state["result"], "review": state["review"]})

    @app.get("/")
    def page():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
