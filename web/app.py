"""The review queue over HTTP, for the page in web/static.

    GET  /api/queue                 what is waiting, what was decided, and what has been paid
    GET  /api/queue/{id}            one held invoice in full
    POST /api/queue/{id}/decision   {"action": "approve" | "reject", "reviewer": "...", "note": "..."}
    POST /api/queue/{id}/reopen     put back an invoice whose saved run is gone, to be processed again

This adds no rules of its own. Listing reads the ledger and the saved runs; deciding calls
pipeline.release, so what may be approved, what is paid and
what is recorded are decided in one place.

Every request opens the ledger and the saved runs afresh and closes them after. SQLite
connections belong to the thread that opened them, and the server answers each request on
whichever worker thread is free.

There is no login. The server listens on this machine only, and a reviewer is whoever types
their name. Requests addressed to any other host name are refused: without that check, a web
page the reviewer has open could point its own domain at 127.0.0.1 and post decisions here.
"""
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel

from extraction import Replay
from ledger import Ledger
from ledger.ledger import currency_of
from ledger.payment import payment_key
from normalizer.__main__ import _json_default
from normalizer.severity import MEANING
from pipeline import build_graph, cannot_approve, checkpointer, conversion, payable, release
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
    """What approving this invoice would pay, by the rule release() pays by (pipeline.payable)."""
    amount, already_paid = payable(invoice)
    return {"amount": amount, "currency": currency_of(invoice), "already_paid": already_paid}


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


def _decided(entry: dict, payments: dict) -> dict:
    """One row of the decided list, with the payment made for it, if any: its amount, currency and whether it confirmed."""
    payment = None
    if entry["number_key"] is not None:
        payment = payments.get(payment_key(entry["vendor_key"], entry["number_key"], entry["revision"]))
    return {"id": entry["id"], "file": entry["source_file"], "vendor": entry["vendor"], "total": entry["total"],
            "status": entry["status"], "action": entry["action"], "reviewer": entry["reviewer"], "note": entry["note"],
            "payment": payment and entry["action"] == "approve" and {
                "amount": payment["amount"], "currency": payment["currency"], "status": payment["status"]} or None}


def _detail(entry: dict, state: dict, rates, stuck: bool) -> dict:
    """Everything a reviewer needs to decide one invoice."""
    invoice = state["invoice"]
    advice = state.get("assessment")
    flags = [{"code": f["code"], "severity": f["severity"], "meaning": MEANING.get(f["code"], ""),
              "detail": {k: v for k, v in f.items() if k not in ("code", "severity")}} for f in invoice.flags]
    return {
        **_summary(entry, state, rates),
        # Decided, but the run never resumed: the decision as recorded, to be carried out again.
        "stuck": stuck and {"action": entry["action"], "reviewer": entry["reviewer"], "note": entry["note"]},
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


def create_app(ledger_path="ledger.db", checkpoints_path="checkpoints.db", rates_path="rates.db",
               allowed_hosts=("127.0.0.1", "localhost")) -> FastAPI:
    """The review application.

    Args:
        ledger_path: The ledger main.py wrote.
        checkpoints_path: The saved runs main.py wrote. A held invoice can only be decided with these.
        rates_path: The exchange-rate table. Without one, a foreign-currency invoice shows no dollar estimate.
        allowed_hosts: The host names this server answers to. A request for any other is refused.
    """
    app = FastAPI(title="Invoice review", docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    # One decision at a time. release() already refuses a second decision on the same invoice;
    # this also keeps two requests from resuming runs against the same files at once.
    deciding = threading.Lock()

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
            payments = {p["payment_key"]: p for p in ledger.payments()}
            decided = [_decided(e, payments) for e in everything if e["status"] != "WAITING" and e["id"] not in stuck]
            return _plain({"waiting": waiting, "decided": decided[::-1], "paid": ledger.paid_totals(),
                           "payments": len(ledger.payments())})

    @app.get("/api/queue/{queue_id}")
    def one(queue_id: int):
        with session() as (ledger, graph, rates):
            entry = ledger.queue_entry(queue_id)
            if entry is None:
                raise HTTPException(404, f"No queue number {queue_id}.")
            stuck = any(e["id"] == queue_id for e in ledger.decided_not_finished())
            if entry["status"] != "WAITING" and not stuck:
                how = f"{entry['action']} by {entry['reviewer']}" if entry["action"] else entry["status"].lower()
                raise HTTPException(409, f"{entry['source_file']} is no longer waiting: it was already decided ({how}).")
            state = saved(graph, entry)
            if not state:
                raise HTTPException(409, f"The saved run for {entry['source_file']} was not found, so it can't be decided. "
                                         "Put it back to be processed again, then run main.py on the file.")
            return _plain(_detail(entry, state, rates, stuck))

    @app.post("/api/queue/{queue_id}/decision")
    def decide(queue_id: int, decision: Decision):
        with session() as (ledger, graph, rates):
            try:
                with deciding:
                    state = release(graph, ledger, queue_id, decision.action, decision.reviewer, decision.note, rates=rates)
            except ValueError as e:
                raise HTTPException(400, str(e))
            return _plain({"file": state["invoice"].source_file, "vendor": state["invoice"].vendor_name,
                           "result": state["result"], "review": state["review"]})

    @app.post("/api/queue/{queue_id}/reopen")
    def reopen(queue_id: int):
        """For a waiting invoice whose saved run is gone: close the entry as LOST and return the invoice to unprocessed."""
        with session() as (ledger, graph, rates), deciding:
            entry = ledger.queue_entry(queue_id)
            if entry is None or entry["status"] != "WAITING":
                raise HTTPException(400, f"No invoice is waiting under queue number {queue_id}.")
            if saved(graph, entry):
                raise HTTPException(400, f"{entry['source_file']} still has its saved run: decide it instead.")
            ledger.reopen(entry, "its saved run was not found, so it could not be released")
            return {"file": entry["source_file"]}

    @app.get("/")
    def page():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
