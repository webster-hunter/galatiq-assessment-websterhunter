"""The review queue over HTTP, for the page in web/static.

    GET  /api/queue                 what is waiting, what was decided, what was resolved without a person, and what has been paid
                                    ?q=text narrows all three lists to rows that mention it; ?outcome= to one kind of outcome
    GET  /api/runs/{run}            one finished run in full: what the application or the agent did, and why
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
import dataclasses
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel

from extraction import Replay
from ledger import Ledger
from ledger.ledger import currency_of
from normalizer.__main__ import _json_default
from normalizer.severity import MEANING
from pipeline import build_graph, cannot_approve, checkpointer, conversion, payable, release
from pipeline.graph import _thread
from rates import MockRates

STATIC = Path(__file__).parent / "static"
LIST_LIMIT = 200  # rows sent per list of finished invoices, unless the request asks for another number


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


def _decided(entry: dict) -> dict:
    """One row of the decided list, from Ledger.find_decided: the entry, the payment made for it and whether it confirmed.

    `run` is the name of the finished run to open for the full record, or None if none was logged
    (an entry closed as lost or superseded, or a decision made before runs were logged).
    """
    paid = entry["action"] == "approve" and entry["payment_status"] is not None
    return {"id": entry["id"], "run": entry["thread_id"] if entry["logged"] else None,
            "file": entry["source_file"], "vendor": entry["vendor"], "total": entry["total"],
            "status": entry["status"], "action": entry["action"], "reviewer": entry["reviewer"], "note": entry["note"],
            "payment": {"amount": entry["paid_amount"], "currency": entry["paid_currency"], "status": entry["payment_status"]} if paid else None}


def _evidence(invoice: dict, assessment: dict | None, critique: dict | None, trail: list) -> dict:
    """What was found on an invoice and what was said about it. `invoice` is the invoice as plain JSON."""
    flags = [{"code": f["code"], "severity": f["severity"], "meaning": MEANING.get(f["code"], ""),
              "detail": {k: v for k, v in f.items() if k not in ("code", "severity")}} for f in invoice["flags"]]
    return {
        "invoice_number": invoice["invoice_number"], "date": invoice["date"], "due_date": invoice["due_date"],
        "payment_terms": invoice["payment_terms"], "notes": invoice["notes"],
        "line_items": [{k: li[k] for k in ("item", "quantity", "unit_price", "note")} for li in invoice["line_items"]],
        "flags": flags,
        "advice": assessment and {
            "by": assessment["by"], "recommendation": assessment.get("recommendation"), "rationale": assessment["rationale"],
            "open_questions": assessment.get("open_questions", []), "note": assessment.get("note"),
            "lookups": [{"tool": t["tool"], "arguments": t["arguments"], "result": t["result"]} for t in assessment.get("tools_used", [])],
            "verified": (critique or {}).get("verdict") == "ACCEPT",
        },
        "trail": trail,
    }


def _detail(entry: dict, state: dict, rates, stuck: bool) -> dict:
    """Everything a reviewer needs to decide one held invoice."""
    invoice = state["invoice"]
    return {
        **_summary(entry, state, rates),
        # Decided, but the run never resumed: the decision as recorded, to be carried out again.
        "stuck": stuck and {"action": entry["action"], "reviewer": entry["reviewer"], "note": entry["note"]},
        "status": entry["status"], "pays": _pays(invoice), "cannot_approve": cannot_approve(invoice, rates),
        "conversion": conversion(invoice, rates), "resolved": None,
        **_evidence(_plain(dataclasses.asdict(invoice)), state.get("assessment"), state.get("critique"), state["trail"]),
    }


# The kinds of outcome the lists can be narrowed to. "other" is everything that is none of the
# first four: a file that couldn't be read, a payment that didn't confirm, an entry closed as lost.
OUTCOMES = ("waiting", "paid", "rejected", "skipped", "other")


WHO = {"policy": "The policy", "policy+agent": "The policy, with the reviewer agent's reason", "ledger": "The ledger",
       "person": "A reviewer", "none": "Nobody: the file could not be read"}


def _run_summary(run: dict) -> dict:
    """One row of the list of finished runs: what happened to a file, who resolved it and why. Read from the run's columns alone."""
    return {"run": run["thread_id"], "file": run["source_file"], "vendor": run["vendor"], "total": run["total"],
            "currency": run["currency"] or "USD", "status": run["status"], "by": run["resolved_by"],
            "who": WHO.get(run["resolved_by"], run["resolved_by"]), "reviewer": run["reviewer"],
            "reasons": [r for r in (run["reasons"] or "").split(", ") if r], "note": run["note"]}


def _run_detail(run: dict) -> dict:
    """One finished run in full, in the shape of a held invoice's detail, with how it ended in place of the decision form."""
    summary = json.loads(run["summary"])
    invoice, result = summary.get("invoice"), summary["result"]
    row = _run_summary(run)
    estimate = result.get("usd_estimate") or next(
        (f for f in (invoice or {}).get("flags", []) if f["code"] == "CURRENCY_CONVERTED"), None)
    base = {"id": None, **row, "usd_estimate": estimate and estimate["usd_estimate"], "recommendation": None,
            "has_saved_run": True, "stuck": False, "pays": None, "cannot_approve": None, "conversion": None,
            "resolved": {"status": run["status"], "by": run["resolved_by"], "who": row["who"], "reviewer": run["reviewer"],
                         "note": row["note"], "rule": (summary.get("decision") or {}).get("rule"),
                         "amount": result.get("amount"), "currency": result.get("currency"), "error": result.get("error")}}
    if invoice is None:   # the file could not be read: there is no invoice to show
        return base | {"invoice_number": None, "date": None, "due_date": None, "payment_terms": None, "notes": None,
                       "line_items": [], "flags": [], "advice": None, "trail": summary.get("trail", [])}
    return base | _evidence(invoice, summary.get("assessment"), summary.get("critique"), summary.get("trail", []))


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
    def queue(q: str = "", outcome: str = "", limit: int = Query(LIST_LIMIT, ge=1, le=1000)):
        """The three lists, optionally narrowed.

        Args:
            q: Words to look for. A row is kept if every word appears in its file name, vendor,
                total, reasons, note or reviewer. Empty: no narrowing.
            outcome: One of OUTCOMES, to keep only rows of that kind. Empty: all kinds.
            limit: The most rows to send for each of the two lists of finished invoices, newest
                first. Every waiting invoice is always sent: those are the ones needing action.
        """
        if outcome and outcome not in OUTCOMES:
            raise HTTPException(400, f"outcome must be one of: {', '.join(OUTCOMES)}")
        with session() as (ledger, graph, rates):
            # The narrowing is done by the ledger, in SQL (Ledger.find_*): only matching rows are
            # read, and for a waiting invoice only then is its saved run loaded.
            held, held_in_all = ledger.find_waiting(q) if outcome in ("", "waiting") else ([], ledger.find_waiting()[1])
            waiting = [_summary(e, saved(graph, e), rates) | {"stuck": bool(e["stuck"]) and {"action": e["action"], "reviewer": e["reviewer"]}}
                       for e in held]
            decided, decided_in_all = ledger.find_decided(q, outcome, limit)
            automatic, automatic_in_all = ledger.find_runs(q, outcome, limit)
            return _plain({"waiting": waiting, "decided": [_decided(e) for e in decided],
                           "automatic": [_run_summary(r) for r in automatic],
                           # How many there are in all, so the screen can say "2 of 14" when a list
                           # is narrowed or cut short.
                           "of": {"waiting": held_in_all, "decided": decided_in_all, "automatic": automatic_in_all},
                           "paid": ledger.paid_totals(), "payments": len(ledger.payments())})

    @app.get("/api/runs/{run}")
    def finished(run: str):
        with session() as (ledger, graph, rates):
            record = ledger.run_record(run)
            if record is None:
                raise HTTPException(404, "No finished run under that name.")
            return _plain(_run_detail(record))

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
