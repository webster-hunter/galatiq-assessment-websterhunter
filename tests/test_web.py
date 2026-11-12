"""The review screen's API: listing, showing and deciding held invoices over HTTP, on the sample folder."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
from web import create_app

ROOT = Path(__file__).resolve().parent.parent
INVOICES = ROOT / "data" / "invoices"


@pytest.fixture
def paths(tmp_path, capsys):
    """The sample folder processed offline into its own ledger, saved runs and rates."""
    where = {"ledger": tmp_path / "l.db", "checkpoints": tmp_path / "cp.db", "rates": tmp_path / "rates.db"}
    main.main([f"--invoice_path={INVOICES}", "--offline", *[x for k, v in where.items() for x in (f"--{k}", str(v))]])
    capsys.readouterr()
    return where


def browser(app, **more):
    """A client that addresses the server as a browser on this machine does."""
    return TestClient(app, base_url="http://127.0.0.1:8000", **more)


@pytest.fixture
def client(paths):
    return browser(create_app(paths["ledger"], paths["checkpoints"], paths["rates"]))


def by_file(client):
    return {s["file"]: s for s in client.get("/api/queue").json()["waiting"]}


def test_the_list_shows_the_six_held_invoices_and_what_has_been_paid(client):
    """Each waiting invoice with its vendor, total, reasons and the agent's recommendation; a foreign total carries its dollar estimate."""
    queue = client.get("/api/queue").json()
    assert [s["file"][8:] for s in queue["waiting"]] == ["1002.txt", "1004_revised.json", "1005.json", "1012.pdf", "1014.xml", "1016.json"]
    assert (queue["payments"], queue["paid"], queue["decided"]) == (6, {"USD": "26325"}, [])
    euro = queue["waiting"][4]
    assert (euro["total"], euro["currency"], euro["usd_estimate"]) == ("4125", "EUR", "4480.16")
    assert queue["waiting"][5]["reasons"] == ["UNKNOWN_PRODUCT"] and queue["waiting"][5]["recommendation"] == "HOLD"


def test_one_invoice_is_shown_with_its_flags_explained_the_advice_and_what_approving_pays(client):
    """The detail a reviewer decides on: each flag with its meaning, the agent's reason and lookups, the lines, and the amount approval would pay. A revision pays the difference."""
    d = client.get(f"/api/queue/{by_file(client)['invoice_1016.json']['id']}").json()
    assert [(f["code"], f["severity"], f["meaning"]) for f in d["flags"]] == [
        ("UNKNOWN_PRODUCT", "REVIEW", "a line's product name is not in inventory")]
    assert d["advice"]["by"] == "reviewer_agent" and d["advice"]["verified"] is True
    assert [l["tool"] for l in d["advice"]["lookups"]] == ["find_similar_products", "get_vendor_history"]
    assert [li["item"] for li in d["line_items"]] == ["WidgetA", "WidgetB", "WidgetC"]
    assert d["pays"] == {"amount": "3233.00", "currency": "USD", "already_paid": None} and d["cannot_approve"] is None

    revision = client.get(f"/api/queue/{by_file(client)['invoice_1004_revised.json']['id']}").json()
    assert revision["pays"] == {"amount": "4050.00", "currency": "USD", "already_paid": "1890"}


def test_approving_pays_once_and_moves_the_invoice_to_decided(client):
    """Approve the revision: the difference is paid, the entry leaves the waiting list, and the decision is listed with the reviewer's name. Deciding it again is refused."""
    queue_id = by_file(client)["invoice_1004_revised.json"]["id"]
    done = client.post(f"/api/queue/{queue_id}/decision", json={"action": "approve", "reviewer": "Dana Lee"})
    assert done.status_code == 200 and done.json()["result"]["status"] == "PAID" and done.json()["result"]["amount"] == "4050.00"
    queue = client.get("/api/queue").json()
    assert len(queue["waiting"]) == 5 and (queue["payments"], queue["paid"]) == (7, {"USD": "30375"})
    assert [(d["file"], d["action"], d["reviewer"], d["payment"]) for d in queue["decided"]] == [
        ("invoice_1004_revised.json", "approve", "Dana Lee", {"amount": "4050", "currency": "USD", "status": "PAID"})]
    again = client.post(f"/api/queue/{queue_id}/decision", json={"action": "approve", "reviewer": "Dana Lee"})
    assert again.status_code == 400 and "it is RESOLVED" in again.json()["detail"]
    assert client.get("/api/queue").json()["payments"] == 7


def test_a_foreign_invoice_is_paid_in_its_own_currency(client):
    """1014 is approved with nothing typed but a name, and 4,125.00 EUR is paid."""
    queue_id = by_file(client)["invoice_1014.xml"]["id"]
    done = client.post(f"/api/queue/{queue_id}/decision", json={"action": "approve", "reviewer": "Dana Lee"}).json()
    assert (done["result"]["amount"], done["result"]["currency"]) == ("4125.00", "EUR")
    assert done["result"]["usd_estimate"]["usd_estimate"] == "4480.16" and done["review"]["reviewer"] == "Dana Lee"
    queue = client.get("/api/queue").json()
    assert queue["paid"] == {"USD": "26325", "EUR": "4125"}
    assert queue["decided"][0]["payment"] == {"amount": "4125", "currency": "EUR", "status": "PAID"}    # in euros, not dollars


def test_rejecting_needs_a_note_and_pays_nothing(client):
    """The rules are release()'s: no note or no name is refused with its message and nothing changes; with both, the rejection is recorded."""
    queue_id = by_file(client)["invoice_1016.json"]["id"]
    for body, message in (({"action": "reject", "reviewer": "Dana Lee"}, "needs a note"),
                          ({"action": "reject", "reviewer": " ", "note": "no"}, "reviewer name"),
                          ({"action": "pay", "reviewer": "Dana Lee"}, "approve or reject")):
        refused = client.post(f"/api/queue/{queue_id}/decision", json=body)
        assert refused.status_code == 400 and message in refused.json()["detail"]
    assert len(client.get("/api/queue").json()["waiting"]) == 6
    done = client.post(f"/api/queue/{queue_id}/decision", json={"action": "reject", "reviewer": "Dana Lee", "note": "We don't buy WidgetC"})
    assert done.json()["result"] == {"status": "REJECTED", "rejected_by": "Dana Lee", "reasons": ["We don't buy WidgetC"]}
    queue = client.get("/api/queue").json()
    assert queue["payments"] == 6 and queue["decided"][0]["note"] == "We don't buy WidgetC"


def test_unknown_numbers_and_missing_files_are_reported_not_created(paths, tmp_path):
    """An unknown queue number is a 404. A ledger or saved-runs path that doesn't exist is a 503 with what to do, and no empty file is left behind."""
    client = browser(create_app(paths["ledger"], paths["checkpoints"], paths["rates"]))
    assert client.get("/api/queue/99").status_code == 404
    assert client.post("/api/queue/99/decision", json={"action": "approve", "reviewer": "Dana"}).status_code == 400
    wrong = browser(create_app(paths["ledger"], tmp_path / "typo.db", paths["rates"]))
    answer = wrong.get("/api/queue")
    assert answer.status_code == 503 and "No saved runs at" in answer.json()["detail"] and not (tmp_path / "typo.db").exists()
    assert wrong.post("/api/queue/1/decision", json={"action": "approve", "reviewer": "Dana"}).status_code == 503
    # Nothing was lost by asking in the wrong place: with the right file the invoice is still there to decide.
    assert client.post("/api/queue/1/decision", json={"action": "reject", "reviewer": "Dana", "note": "Over stock"}).status_code == 200


def test_the_page_is_served_and_never_writes_vendor_text_as_html(client):
    """The page and its script load. The script builds the page with textContent only: vendor text can't become markup."""
    assert "Invoice review" in client.get("/").text
    script = client.get("/static/app.js").text
    assert "/api/queue" in script and "innerHTML" not in script and "insertAdjacentHTML" not in script
    source = (ROOT / "web" / "frontend" / "src" / "app.ts").read_text()
    assert "innerHTML" not in source


# --- Fixes from the code review ----------------------------------------------------------------------

def test_requests_for_another_host_name_are_refused(paths):
    """The server answers to 127.0.0.1 and localhost only. A page that points its own domain at this machine is refused before any route runs."""
    app = create_app(paths["ledger"], paths["checkpoints"], paths["rates"])
    assert TestClient(app, base_url="http://localhost:8000").get("/api/queue").status_code == 200
    rebound = TestClient(app, base_url="http://attacker.example:8000")
    assert rebound.get("/api/queue").status_code == 400
    assert rebound.post("/api/queue/1/decision", json={"action": "approve", "reviewer": "x"}).status_code == 400
    assert len(browser(app).get("/api/queue").json()["waiting"]) == 6


def test_a_decision_recorded_but_not_carried_out_can_be_finished_from_the_screen(paths, monkeypatch):
    """The server dies between recording a decision and resuming the run. The invoice stays in the list, marked; its detail carries the recorded decision; only that decision can be repeated, and repeating it pays."""
    import pipeline.graph as g
    client = browser(create_app(paths["ledger"], paths["checkpoints"], paths["rates"]), raise_server_exceptions=False)
    queue_id = by_file(client)["invoice_1016.json"]["id"]
    real = g._stream
    monkeypatch.setattr(g, "_stream", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("process died")))
    assert client.post(f"/api/queue/{queue_id}/decision", json={"action": "approve", "reviewer": "Dana Lee", "note": "OK'd by purchasing"}).status_code == 500
    monkeypatch.setattr(g, "_stream", real)

    listed = by_file(client)["invoice_1016.json"]
    assert listed["stuck"] == {"action": "approve", "reviewer": "Dana Lee"}
    assert client.get(f"/api/queue/{queue_id}").json()["stuck"] == {"action": "approve", "reviewer": "Dana Lee", "note": "OK'd by purchasing"}
    other = client.post(f"/api/queue/{queue_id}/decision", json={"action": "reject", "reviewer": "Someone Else", "note": "no"})
    assert other.status_code == 400 and "already decided: approve by Dana Lee" in other.json()["detail"]
    done = client.post(f"/api/queue/{queue_id}/decision", json={"action": "approve", "reviewer": "Someone Else"}).json()
    assert (done["result"]["status"], done["result"]["approved_by"]) == ("PAID", "Dana Lee")
    assert "invoice_1016.json" not in by_file(client)


def test_an_invoice_already_decided_says_so_instead_of_blaming_the_files(client):
    """Opening an invoice someone else decided a moment ago reports the decision, not a missing saved run."""
    queue_id = by_file(client)["invoice_1016.json"]["id"]
    client.post(f"/api/queue/{queue_id}/decision", json={"action": "reject", "reviewer": "Dana Lee", "note": "Not ordered"})
    stale = client.get(f"/api/queue/{queue_id}")
    assert stale.status_code == 409 and "already decided (reject by Dana Lee)" in stale.json()["detail"]


def test_a_waiting_invoice_whose_saved_run_is_gone_can_be_put_back(paths, tmp_path):
    """The saved runs were replaced. The list marks each invoice as having no saved run; it can't be shown or decided, but it can be put back, which closes the entry and returns the invoice to unprocessed. One with its run intact can't be put back."""
    import sqlite3
    from ledger import Ledger
    sqlite3.connect(tmp_path / "other.db").close()      # an existing file with no runs in it
    lost = browser(create_app(paths["ledger"], tmp_path / "other.db", paths["rates"]))
    entry = by_file(lost)["invoice_1005.json"]
    assert entry["has_saved_run"] is False
    assert lost.get(f"/api/queue/{entry['id']}").status_code == 409
    assert lost.post(f"/api/queue/{entry['id']}/reopen").json() == {"file": "invoice_1005.json"}
    assert "invoice_1005.json" not in by_file(lost)
    ledger = Ledger(paths["ledger"])
    assert ledger.queue_entry(entry["id"])["status"] == "LOST"
    assert [i["status"] for i in ledger.invoices() if i["source_file"] == "invoice_1005.json"] == ["RECEIVED"]
    assert lost.post(f"/api/queue/{entry['id']}/reopen").status_code == 400            # already put back
    good = browser(create_app(paths["ledger"], paths["checkpoints"], paths["rates"]))
    assert good.post(f"/api/queue/{by_file(good)['invoice_1016.json']['id']}/reopen").status_code == 400


def test_a_payment_that_does_not_confirm_is_reported_as_such(paths, monkeypatch):
    """The payment function fails mid-call. The answer says PAYMENT_UNCONFIRMED with the error, and the decided list carries the payment as PENDING, so the screen can say to check with the bank instead of 'paid'."""
    import web.app as web_app
    from ledger import Ledger

    def timeout(*args):
        raise TimeoutError("bank did not answer")

    monkeypatch.setattr(web_app, "Ledger", lambda path: Ledger(path, pay=timeout))
    client = browser(create_app(paths["ledger"], paths["checkpoints"], paths["rates"]))
    queue_id = by_file(client)["invoice_1016.json"]["id"]
    done = client.post(f"/api/queue/{queue_id}/decision", json={"action": "approve", "reviewer": "Dana Lee"}).json()
    assert done["vendor"] == "Widgets Inc." and done["result"]["status"] == "PAYMENT_UNCONFIRMED"
    assert "TimeoutError: bank did not answer" in done["result"]["error"]
    queue = client.get("/api/queue").json()
    assert queue["decided"][0]["payment"] == {"amount": "3233", "currency": "USD", "status": "PENDING"}
    assert (queue["payments"], queue["paid"]) == (7, {"USD": "26325"})          # on record, and not counted as paid


def test_the_screen_shows_the_amount_release_would_pay():
    """One rule, in pipeline.payable: the total, or for a revision of a paid invoice the difference. A revision with nothing more owed can't be approved, and the screen is told so before anyone clicks."""
    from decimal import Decimal
    import normalizer
    from pipeline import cannot_approve, payable
    invoice = normalizer.load(INVOICES / "invoice_1004_revised.json", {"WidgetA": 15, "WidgetB": 10, "GadgetX": 5})
    assert payable(invoice) == (Decimal("5940.00"), None)
    invoice.flags.append({"code": "REVISION_OF_PAID", "severity": "REVIEW", "already_paid": Decimal("1890"), "difference": Decimal("4050.00")})
    assert payable(invoice) == (Decimal("4050.00"), Decimal("1890")) and cannot_approve(invoice) is None
    invoice.flags[-1] |= {"already_paid": Decimal("5940"), "difference": Decimal("0.00")}
    assert cannot_approve(invoice) == "it revises an invoice already paid 5940, and nothing more is owed"


def test_the_compiled_script_matches_its_source(tmp_path):
    """web/static/app.js is committed so the screen runs without Node. Where the TypeScript compiler is installed, check it is what app.ts compiles to."""
    import shutil
    import subprocess
    if shutil.which("tsc") is None:
        pytest.skip("the TypeScript compiler is not installed")
    frontend = ROOT / "web" / "frontend"
    built = subprocess.run(["tsc", "-p", str(frontend), "--outDir", str(tmp_path)], capture_output=True, text=True)
    assert built.returncode == 0, built.stdout
    assert (tmp_path / "app.js").read_text() == (ROOT / "web" / "static" / "app.js").read_text(), \
        "web/static/app.js is out of date: run `npm run build` in web/frontend"



# --- Invoices resolved without a person --------------------------------------------------------------

def test_what_the_application_and_the_agent_resolved_is_listed_with_who_and_why(client):
    """Every file that reached an outcome with no person: paid or rejected by the policy (with the agent's reason where it wrote one), or skipped by the ledger as a copy. Held invoices are not in it."""
    automatic = {a["file"][8:]: a for a in client.get("/api/queue").json()["automatic"]}
    assert {f: (a["status"], a["by"]) for f, a in automatic.items()} == {
        "1001.txt": ("PAID", "policy"), "1004.json": ("PAID", "policy"), "1006.csv": ("PAID", "policy"),
        "1011.pdf": ("PAID", "policy"), "1015.csv": ("PAID", "policy"), "1010.txt": ("PAID", "policy+agent"),
        "1003.txt": ("REJECTED", "policy"), "1007.csv": ("REJECTED", "policy"), "1008.txt": ("REJECTED", "policy"),
        "1009.json": ("REJECTED", "policy"), "1013.json": ("REJECTED", "policy"),
        "1011.txt": ("SKIPPED_DUPLICATE", "ledger"), "1012.txt": ("SKIPPED_DUPLICATE", "ledger"), "1013.pdf": ("SKIPPED_DUPLICATE", "ledger")}
    assert automatic["1008.txt"]["reasons"] == ["PRODUCT_NOT_STOCKED"] and automatic["1001.txt"]["reasons"] == []
    assert automatic["1010.txt"]["reasons"] == ["UNEXPECTED_FIELDS", "DUPLICATE_ITEM"]         # paid with these warnings
    assert automatic["1011.txt"]["note"] == "A copy of invoice_1011.pdf, which was paid"


def test_a_resolved_invoice_is_shown_with_its_evidence_and_how_it_ended(client):
    """Opened, a finished run has what a held invoice has (flags explained, the reason, the lines) and, in place of a decision, how it ended: 1010 paid on the policy's rule with the agent's reason; 1003 rejected on four fraud signals."""
    runs = {a["file"]: a["run"] for a in client.get("/api/queue").json()["automatic"]}
    paid = client.get(f"/api/runs/{runs['invoice_1010.txt']}").json()
    assert paid["resolved"] == {"status": "PAID", "by": "policy+agent", "who": "The policy, with the reviewer agent's reason",
                                "reviewer": None, "note": None, "rule": "NO_OBJECTION", "amount": "7185.00", "currency": "USD", "error": None}
    assert paid["advice"]["by"] == "reviewer_agent" and "rush order" in paid["advice"]["rationale"] and paid["advice"]["verified"] is True
    assert (paid["id"], paid["pays"], len(paid["line_items"])) == (None, None, 4)

    rejected = client.get(f"/api/runs/{runs['invoice_1003.txt']}").json()
    assert (rejected["resolved"]["status"], rejected["resolved"]["rule"], rejected["resolved"]["amount"]) == ("REJECTED", "FRAUD_SIGNALS", None)
    assert rejected["advice"]["by"] == "policy" and "WIRE_REQUEST" in [f["code"] for f in rejected["flags"]]
    assert client.get("/api/runs/nothing").status_code == 404


def test_a_persons_decision_is_not_listed_as_automatic_and_a_rerun_adds_nothing(client, paths, capsys):
    """An invoice a reviewer released is logged as theirs, so it stays out of the automatic list. Running the folder again logs no new runs: a file already processed keeps its first record."""
    from ledger import Ledger
    before = client.get("/api/queue").json()["automatic"]
    queue_id = by_file(client)["invoice_1016.json"]["id"]
    client.post(f"/api/queue/{queue_id}/decision", json={"action": "approve", "reviewer": "Dana Lee"})
    main.main([f"--invoice_path={INVOICES}", "--offline", *[x for k, v in paths.items() for x in (f"--{k}", str(v))]])
    capsys.readouterr()
    assert client.get("/api/queue").json()["automatic"] == before
    [mine] = [r for r in Ledger(paths["ledger"]).runs() if r["resolved_by"] == "person"]
    assert (mine["source_file"], mine["status"], mine["reviewer"]) == ("invoice_1016.json", "PAID", "Dana Lee")


def test_a_file_that_could_not_be_read_is_listed_too(paths, tmp_path, capsys):
    """An unreadable file has no invoice, but it is still an outcome someone should see."""
    bad = tmp_path / "broken.json"
    bad.write_text("{not json")
    main.main([f"--invoice_path={bad}", "--offline", *[x for k, v in paths.items() for x in (f"--{k}", str(v))]])
    capsys.readouterr()
    client = browser(create_app(paths["ledger"], paths["checkpoints"], paths["rates"]))
    newest = client.get("/api/queue").json()["automatic"][0]
    assert (newest["file"], newest["status"], newest["by"], newest["vendor"]) == ("broken.json", "ERROR", "none", None)
    shown = client.get(f"/api/runs/{newest['run']}").json()
    assert shown["resolved"]["error"] and shown["flags"] == [] and shown["line_items"] == []
