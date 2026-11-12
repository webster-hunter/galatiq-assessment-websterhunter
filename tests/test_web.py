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


@pytest.fixture
def client(paths):
    return TestClient(create_app(paths["ledger"], paths["checkpoints"], paths["rates"]))


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
    assert [(d["file"], d["action"], d["reviewer"], d["amount"]) for d in queue["decided"]] == [
        ("invoice_1004_revised.json", "approve", "Dana Lee", "4050")]
    again = client.post(f"/api/queue/{queue_id}/decision", json={"action": "approve", "reviewer": "Dana Lee"})
    assert again.status_code == 400 and "it is RESOLVED" in again.json()["detail"]
    assert client.get("/api/queue").json()["payments"] == 7


def test_a_foreign_invoice_is_paid_in_its_own_currency(client):
    """1014 is approved with nothing typed but a name, and 4,125.00 EUR is paid."""
    queue_id = by_file(client)["invoice_1014.xml"]["id"]
    done = client.post(f"/api/queue/{queue_id}/decision", json={"action": "approve", "reviewer": "Dana Lee"}).json()
    assert (done["result"]["amount"], done["result"]["currency"]) == ("4125.00", "EUR")
    assert done["result"]["usd_estimate"]["usd_estimate"] == "4480.16" and done["review"]["reviewer"] == "Dana Lee"
    assert client.get("/api/queue").json()["paid"] == {"USD": "26325", "EUR": "4125"}


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
    client = TestClient(create_app(paths["ledger"], paths["checkpoints"], paths["rates"]))
    assert client.get("/api/queue/99").status_code == 404
    assert client.post("/api/queue/99/decision", json={"action": "approve", "reviewer": "Dana"}).status_code == 400
    wrong = TestClient(create_app(paths["ledger"], tmp_path / "typo.db", paths["rates"]))
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
