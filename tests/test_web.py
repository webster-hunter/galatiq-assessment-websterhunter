"""The review screen's API: listing, showing and deciding held invoices over HTTP, on the sample folder."""
import json
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
    # Every id the script looks up exists once in the page: a repeated id silently answers with the wrong element.
    import re
    page = client.get("/").text
    ids = re.findall(r'id="([^"]+)"', page)
    assert len(ids) == len(set(ids)), sorted(i for i in set(ids) if ids.count(i) > 1)
    assert set(re.findall(r'\$\("([a-z-]+)"\)', source)) <= set(ids)


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


# --- Search and filters ------------------------------------------------------------------------------

def files(rows):
    return sorted(r["file"][8:] for r in rows)


def test_search_narrows_all_three_lists_and_says_how_many_there_are(client):
    """A vendor name finds its invoices wherever they are: one waiting, one resolved. Every word must match, case is ignored, and the counts before narrowing come back too."""
    found = client.get("/api/queue", params={"q": "widgets inc"}).json()
    assert (files(found["waiting"]), files(found["automatic"]), found["decided"]) == (["1016.json"], ["1001.txt"], [])
    assert found["of"] == {"waiting": 6, "decided": 0, "automatic": 14}
    assert files(client.get("/api/queue", params={"q": "WIDGETS gadgets"}).json()["waiting"]) == []      # no row has both words
    everything = client.get("/api/queue").json()
    assert (len(everything["waiting"]), len(everything["automatic"])) == (6, 14) and everything["of"]["automatic"] == 14


def test_search_matches_file_names_reasons_and_notes(client):
    """A reason is found as its flag code or in words; a file name and the note on a skipped copy are searched too."""
    by_code = client.get("/api/queue", params={"q": "STOCK_EXCEEDED"}).json()
    in_words = client.get("/api/queue", params={"q": "stock exceeded"}).json()
    assert files(by_code["waiting"]) == files(in_words["waiting"]) == ["1002.txt", "1005.json"]
    assert files(in_words["automatic"]) == ["1003.txt"]
    assert files(client.get("/api/queue", params={"q": "1013"}).json()["automatic"]) == ["1013.json", "1013.pdf"]
    assert files(client.get("/api/queue", params={"q": "copy of invoice_1011.pdf"}).json()["automatic"]) == ["1011.txt"]


def test_the_outcome_filter_keeps_one_kind_and_combines_with_search(client):
    """Each outcome keeps only its rows; "waiting" empties the resolved lists and the others empty the waiting list. With a search, both must hold."""
    def shown(**params):
        q = client.get("/api/queue", params=params).json()
        return len(q["waiting"]), len(q["decided"]), files(q["automatic"])

    assert shown(outcome="waiting") == (6, 0, [])
    assert shown(outcome="paid") == (0, 0, ["1001.txt", "1004.json", "1006.csv", "1010.txt", "1011.pdf", "1015.csv"])
    assert shown(outcome="rejected") == (0, 0, ["1003.txt", "1007.csv", "1008.txt", "1009.json", "1013.json"])
    assert shown(outcome="skipped") == (0, 0, ["1011.txt", "1012.txt", "1013.pdf"])
    assert shown(outcome="other") == (0, 0, [])
    # Words match separately: 1009 has "total negative" and "subtotal mismatch", so it is found too.
    assert shown(outcome="rejected", q="total mismatch") == (0, 0, ["1007.csv", "1009.json", "1013.json"])
    assert shown(outcome="rejected", q="mismatch atlas") == (0, 0, ["1013.json"])
    assert client.get("/api/queue", params={"outcome": "maybe"}).status_code == 400


def test_a_reviewers_decisions_are_filtered_by_what_they_led_to(client):
    """An approval that paid is "paid"; a rejection is "rejected"; and a reviewer's name finds their decisions."""
    ids = {f: s["id"] for f, s in by_file(client).items()}
    client.post(f"/api/queue/{ids['invoice_1016.json']}/decision", json={"action": "approve", "reviewer": "Dana Lee"})
    client.post(f"/api/queue/{ids['invoice_1002.txt']}/decision", json={"action": "reject", "reviewer": "Sam Ortiz", "note": "Over stock"})
    assert files(client.get("/api/queue", params={"outcome": "paid"}).json()["decided"]) == ["1016.json"]
    assert files(client.get("/api/queue", params={"outcome": "rejected"}).json()["decided"]) == ["1002.txt"]
    assert files(client.get("/api/queue", params={"q": "sam ortiz"}).json()["decided"]) == ["1002.txt"]
    assert client.get("/api/queue", params={"q": "over stock"}).json()["of"] == {"waiting": 4, "decided": 2, "automatic": 14}


def test_an_unconfirmed_payment_is_filed_under_other_not_paid(paths, monkeypatch):
    """An approval whose payment did not confirm must not turn up when someone filters for what was paid."""
    import web.app as web_app
    from ledger import Ledger

    def timeout(*args):
        raise TimeoutError("bank did not answer")

    monkeypatch.setattr(web_app, "Ledger", lambda path: Ledger(path, pay=timeout))
    client = browser(create_app(paths["ledger"], paths["checkpoints"], paths["rates"]))
    client.post(f"/api/queue/{by_file(client)['invoice_1016.json']['id']}/decision", json={"action": "approve", "reviewer": "Dana Lee"})
    assert client.get("/api/queue", params={"outcome": "paid"}).json()["decided"] == []
    assert files(client.get("/api/queue", params={"outcome": "other"}).json()["decided"]) == ["1016.json"]


# --- Fixes from the second code review ---------------------------------------------------------------

def test_an_invoice_a_reviewer_decided_can_be_opened_with_every_step_it_took(client):
    """A decided row names its finished run. Opened, it says who resolved it and with what note, and its steps run to the end: the wait for review and the payment are there, not only what came before."""
    queue_id = by_file(client)["invoice_1016.json"]["id"]
    client.post(f"/api/queue/{queue_id}/decision", json={"action": "approve", "reviewer": "Dana Lee", "note": "New product, agreed"})
    [row] = client.get("/api/queue").json()["decided"]
    shown = client.get(f"/api/runs/{row['run']}").json()
    assert (shown["resolved"]["status"], shown["resolved"]["by"], shown["resolved"]["reviewer"], shown["resolved"]["note"]) == (
        "PAID", "person", "Dana Lee", "New product, agreed")
    assert [s["node"] for s in shown["trail"]][-3:] == ["hold", "await_review", "pay"]
    assert shown["trail"][-1]["approved_by"] == "Dana Lee" and shown["flags"][0]["code"] == "UNKNOWN_PRODUCT"


def test_a_run_resolved_by_the_policy_ends_with_its_final_step(client):
    """The logged steps include the one that ended the run."""
    runs = {a["file"]: a["run"] for a in client.get("/api/queue").json()["automatic"]}
    assert client.get(f"/api/runs/{runs['invoice_1003.txt']}").json()["trail"][-1]["node"] == "reject"
    assert client.get(f"/api/runs/{runs['invoice_1001.txt']}").json()["trail"][-1]["node"] == "pay"
    assert client.get(f"/api/runs/{runs['invoice_1011.txt']}").json()["trail"][-1]["node"] == "skip"


def test_an_amount_is_found_however_it_is_typed(client):
    """The screen shows $15,000.00; the ledger stores 15000. Searching either form, with or without the sign, comma or cents, finds the invoice."""
    for typed in ("15000", "15,000", "$15,000.00", "15,000.00"):
        assert files(client.get("/api/queue", params={"q": typed}).json()["waiting"]) == ["1002.txt"], typed
    assert files(client.get("/api/queue", params={"q": "4,125"}).json()["waiting"]) == ["1014.xml"]
    assert files(client.get("/api/queue", params={"q": "$22,562.80"}).json()["automatic"]) == ["1013.json", "1013.pdf"]


def test_the_lists_of_finished_invoices_are_limited_and_read_without_their_evidence(client, paths):
    """A limit cuts the finished lists, newest first, and the count still says how many there are. Listing reads the runs' columns only: no evidence is loaded to build or search the list."""
    from ledger import Ledger
    cut = client.get("/api/queue", params={"limit": 3}).json()
    assert (len(cut["automatic"]), cut["of"]["automatic"], len(cut["waiting"])) == (3, 14, 6)      # waiting is never cut
    assert files(cut["automatic"]) == ["1013.json", "1013.pdf", "1015.csv"]
    assert client.get("/api/queue", params={"limit": 0}).status_code == 422
    rows = Ledger(paths["ledger"]).runs()
    assert "summary" not in rows[0] and {"reasons", "note", "source_path"} <= set(rows[0])
    by_name = {r["source_file"]: r for r in rows}
    assert by_name["invoice_1003.txt"]["reasons"] == "STOCK_EXCEEDED, UNPARSEABLE, PRESSURE_LANGUAGE, WIRE_REQUEST"
    assert by_name["invoice_1011.txt"]["note"] == "A copy of invoice_1011.pdf, which was paid"


# --- Search in SQL, and files in a format nothing reads ----------------------------------------------

def test_the_search_is_done_by_the_ledger_in_sql(paths):
    """Ledger.find_* narrow in the database: every word must be in the row's search text, the outcome is a condition, the limit applies after narrowing, and the count before narrowing comes back alongside."""
    from ledger import Ledger
    ledger = Ledger(paths["ledger"])
    rows, in_all = ledger.find_runs("total mismatch", "rejected", limit=2)
    assert (sorted(r["source_file"] for r in rows), in_all) == (["invoice_1009.json", "invoice_1013.json"], 14)   # newest two of three
    assert [r["source_file"] for r in ledger.find_runs("$22,562.80")[0]] == ["invoice_1013.pdf", "invoice_1013.json"]
    assert ledger.find_runs("anything", "waiting") == ([], 14)
    waiting, in_all = ledger.find_waiting("gadgets")
    assert ([w["source_file"] for w in waiting], in_all, waiting[0]["stuck"]) == (["invoice_1002.txt"], 6, 0)
    assert ledger.find_decided() == ([], 0)
    assert "search" not in ledger.find_runs()[0][0]          # the search text is for the database, not the caller


def test_a_search_word_is_taken_literally(client):
    """LIKE's wildcards mean nothing special when typed: "%" and "_" match only themselves, and a quote does no harm."""
    assert client.get("/api/queue", params={"q": "%"}).json()["automatic"] == []
    assert files(client.get("/api/queue", params={"q": "total_mismatch"}).json()["automatic"]) == ["1007.csv", "1009.json", "1013.json"]
    assert client.get("/api/queue", params={"q": "inv_ice"}).json()["waiting"] == []        # "_" is not "any character"
    assert client.get("/api/queue", params={"q": "'; DROP TABLE runs; --"}).status_code == 200
    assert len(client.get("/api/queue").json()["automatic"]) == 14


def test_rows_from_before_the_search_column_are_given_their_search_text(paths):
    """A ledger written before search moved into SQL has no search text. Opening it fills it in, so its rows are found like new ones."""
    import sqlite3
    from ledger import Ledger
    db = sqlite3.connect(paths["ledger"])
    db.execute("ALTER TABLE runs DROP COLUMN search")              # as a ledger from before the column was
    db.execute("ALTER TABLE review_queue DROP COLUMN search")
    db.commit()
    db.close()
    ledger = Ledger(paths["ledger"])
    assert [r["source_file"] for r in ledger.find_runs("stock exceeded")[0]] == ["invoice_1003.txt"]
    assert [w["source_file"] for w in ledger.find_waiting("15,225")[0]] == ["invoice_1005.json"]


def test_a_file_in_a_format_nothing_reads_is_reported_not_passed_over(paths, tmp_path, capsys):
    """A folder holds a spreadsheet and a Word file among the invoices. Each ends as an ERROR naming the formats that can be read, and shows on the screen; a hidden file and a subfolder are left alone. No model is asked to read them."""
    inbox = tmp_path / "inbox"
    (inbox / "archive").mkdir(parents=True)
    (inbox / "invoice_1004.json").write_text((INVOICES / "invoice_1004.json").read_text())
    (inbox / "invoice_2001.docx").write_bytes(b"PK\\x03\\x04 not really a document")
    (inbox / "invoice_2002.xlsx").write_bytes(b"PK\\x03\\x04")
    (inbox / ".DS_Store").write_bytes(b"\\x00")
    where = [x for k, v in paths.items() for x in (f"--{k}", str(v))]
    main.main([f"--invoice_path={inbox}", "--offline", *where])
    out = capsys.readouterr()
    assert "3 files:" in out.err
    errors = {Path(r["file"]).name: r["error"] for r in json.loads(out.out) if "error" in r}
    assert set(errors) == {"invoice_2001.docx", "invoice_2002.xlsx"}
    assert "No reader for .docx files (invoice_2001.docx). Supported: .csv, .json, .pdf, .txt, .xml" in errors["invoice_2001.docx"]
    client = browser(create_app(paths["ledger"], paths["checkpoints"], paths["rates"]))
    other = client.get("/api/queue", params={"outcome": "other"}).json()["automatic"]
    assert sorted(a["file"] for a in other) == ["invoice_2001.docx", "invoice_2002.xlsx"] and other[0]["status"] == "ERROR"


def test_an_entry_closed_as_lost_is_found_under_other(paths, tmp_path):
    """An entry with no action recorded (closed as lost, or superseded) is neither paid nor rejected. It belongs to "other", and must not drop out of every filter."""
    import sqlite3
    sqlite3.connect(tmp_path / "other.db").close()
    lost = browser(create_app(paths["ledger"], tmp_path / "other.db", paths["rates"]))
    lost.post(f"/api/queue/{by_file(lost)['invoice_1005.json']['id']}/reopen")
    client = browser(create_app(paths["ledger"], paths["checkpoints"], paths["rates"]))
    client.post(f"/api/queue/{by_file(client)['invoice_1016.json']['id']}/decision", json={"action": "approve", "reviewer": "Dana Lee"})
    shown = {o: [(d["file"][8:], d["status"]) for d in client.get("/api/queue", params={"outcome": o}).json()["decided"]]
             for o in ("other", "paid", "rejected")}
    assert shown == {"other": [("1005.json", "LOST")], "paid": [("1016.json", "RESOLVED")], "rejected": []}


def test_opening_the_ledger_does_not_scan_it_and_counting_does_not_read_rows(paths):
    """Search text is filled in once, when a ledger gains the column. After that, opening the ledger runs no such scan, and the count of waiting invoices is a count."""
    from ledger import Ledger
    calls = []
    original = Ledger._fill_search
    Ledger._fill_search = lambda self: calls.append(1) or original(self)
    try:
        ledger = Ledger(paths["ledger"])
    finally:
        Ledger._fill_search = original
    assert calls == [] and ledger.count_waiting() == 6 == ledger.find_waiting()[1]

