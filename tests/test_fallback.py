"""Files no parser can read: an unknown file type, or a JSON, CSV or XML file in an unknown layout.

The model reads them as text, under the same grounding check as a PDF, and the result is held for
a person. A scripted model stands in here; tests/test_recordings.py covers the real model's replies.
"""
from pathlib import Path

import pytest

import normalizer
from approval import HOLD, REJECT, decide
from extraction import ProviderError, read
from extraction.grounding import as_written, records, ungrounded

ROOT = Path(__file__).resolve().parent.parent
INVOICES = ROOT / "data" / "invoices"
UNFAMILIAR = ROOT / "data" / "unfamiliar"
INVENTORY = {"WidgetA": 15, "WidgetB": 10, "GadgetX": 5, "FakeItem": 0}
TOP = ("invoice_number", "vendor_name", "vendor_address", "date", "due_date", "subtotal", "tax_rate", "tax_amount",
       "shipping", "total", "currency", "payment_terms", "notes")


def transcription(lines=(), other=(), **top):
    """A model reply in the extraction schema: every key present, null unless given."""
    return {**{k: top.get(k) for k in TOP},
            "line_items": [{"item": i, "quantity": q, "unit_price": p, "amount": None, "note": None} for i, q, p in lines],
            "other_fields": [{"label": k, "value": v} for k, v in other]}


class Model:
    """Replies with what it is given, in turn, and records what it was sent."""

    def __init__(self, *replies):
        self.replies, self.sent = list(replies), []

    def complete(self, messages, schema):
        self.sent.append(messages)
        reply = self.replies.pop(0) if self.replies else ProviderError("no recording")
        if isinstance(reply, Exception):
            raise reply
        return reply


T3001 = transcription(invoice_number="INV-3001", vendor_name="Northwind Components", date="2026-02-03", due_date="2026-03-05",
                      subtotal="1750.00", tax_rate="8%", tax_amount="140.00", total="1890.00", payment_terms="Net 30",
                      lines=[("WidgetA", "3", "250.00"), ("WidgetB", "2", "500.00")], other=[("purchaseOrder", "PO-7741")])


def codes(invoice):
    return [f["code"] for f in invoice.flags]


# --- A known type in an unknown layout ------------------------------------------------------------

def test_a_json_file_in_a_suppliers_own_layout_is_read_by_the_model_and_held():
    """The parser finds nothing under its own field names, so the model is given the file as text. Its transcription is grounded like a PDF's, including line items that span several lines of the file, and the invoice is held: a new layout gets one look from a person."""
    model = Model(T3001)
    invoice, method = read(UNFAMILIAR / "invoice_3001.json", model)
    assert method == "llm_fallback" and len(model.sent) == 1 and '"invoiceNo": "INV-3001"' in model.sent[0][1]["content"]
    assert (invoice.invoice_number, invoice.vendor_name, str(invoice.total), len(invoice.line_items)) == (
        "INV-3001", "Northwind Components", "1890.00", 2)
    [flag] = [f for f in invoice.flags if f["code"] == "UNFAMILIAR_FORMAT"]
    assert flag["read_by"] == "model" and {"invoiceNo", "grandTotal", "rows", "supplier"} <= set(flag["unrecognised_fields"])
    assert "NOT_IN_SOURCE" not in codes(invoice)

    checked = normalizer.check(invoice, INVENTORY)
    decision = decide(checked.flags)
    assert (decision.outcome, decision.because) == (HOLD, ("UNFAMILIAR_FORMAT",))     # nothing else is wrong with it


def test_what_the_model_returns_for_a_structured_file_is_checked_against_the_file():
    """A value that isn't in the file is flagged, and so is a line whose values come from two different objects."""
    invented = {**T3001, "total": "1980.00"}
    invoice, _ = read(UNFAMILIAR / "invoice_3001.json", Model(invented, invented))
    assert [(f["field"], f["value"]) for f in invoice.flags if f["code"] == "NOT_IN_SOURCE"] == [("total", "1980.00")]

    crossed = {**T3001, "line_items": transcription(lines=[("WidgetA", "2", "250.00")])["line_items"]}   # WidgetB's quantity
    invoice, _ = read(UNFAMILIAR / "invoice_3001.json", Model(crossed, crossed))
    assert [(f["field"], f.get("reason")) for f in invoice.flags if f["code"] == "NOT_IN_SOURCE"] == [("line_items[0]", "NOT_ON_ONE_LINE")]


def test_without_the_model_an_unfamiliar_layout_is_rejected_with_everything_kept():
    """No model, or a model that finds no invoice either: the parser's empty result stands, flagged, and is rejected for its missing fields. What the file held is still on the invoice."""
    for model, said in ((Model(), "was not available"), (Model(transcription()), "found no invoice in it either")):
        invoice, method = read(UNFAMILIAR / "invoice_3001.json", model)
        [flag] = [f for f in invoice.flags if f["code"] == "UNFAMILIAR_FORMAT"]
        assert method == "parser" and flag["read_by"] == "parser" and said in flag["model"]
        assert invoice.invoice_number is None and "grandTotal" in invoice.unexpected_fields
        assert decide(normalizer.check(invoice, INVENTORY).flags).outcome == REJECT


def test_an_invoice_in_our_layout_with_a_field_missing_is_not_sent_to_the_model():
    """1009 has no vendor but is plainly in our layout: it is the parser's, and the model is never called. So are all the other structured samples."""
    model = Model()
    for path in sorted(INVOICES.iterdir()):
        if path.suffix in (".json", ".csv", ".xml"):
            invoice, method = read(path, model)
            assert method == "parser" and "UNFAMILIAR_FORMAT" not in codes(invoice), path.name
    assert model.sent == []


def test_csv_and_xml_in_unknown_layouts_take_the_same_path():
    """A CSV with other column names keeps only a stray total under the parser; an XML file with another root element is refused by it outright. Both go to the model. If the model isn't there, the XML file's error says so."""
    csv_reply = transcription(invoice_number="INV-3002", vendor_name="Lakeside Tooling Ltd.", date="2026-02-04", due_date="2026-03-06",
                              total="1250.00", lines=[("WidgetA", "2", "250.00"), ("GadgetX", "1", "750.00")])
    invoice, method = read(UNFAMILIAR / "invoice_3002.csv", Model(csv_reply))
    assert method == "llm_fallback" and "NOT_IN_SOURCE" not in codes(invoice) and [str(li.quantity) for li in invoice.line_items] == ["2", "1"]

    xml_reply = transcription(invoice_number="INV-3003", vendor_name="Ridgeline Fasteners", date="2026-02-05", due_date="2026-03-07",
                              total="2000.00", lines=[("WidgetB", "4", "500.00")])
    invoice, method = read(UNFAMILIAR / "invoice_3003.xml", Model(xml_reply))
    assert method == "llm_fallback" and "NOT_IN_SOURCE" not in codes(invoice)
    assert [f for f in invoice.flags if f["code"] == "UNFAMILIAR_FORMAT"][0]["unrecognised_fields"] == []
    with pytest.raises(ValueError, match="root element is <Bill>, not <invoice>.*the model was not available"):
        read(UNFAMILIAR / "invoice_3003.xml", Model())


# --- An unknown file type -------------------------------------------------------------------------

T3004 = transcription(invoice_number="INV-3004", vendor_name="Coastal Bearings Co.", date="2026-02-06", due_date="2026-03-08",
                      total="3000.00", payment_terms="NET30", lines=[("WidgetA", "6", "250.00"), ("GadgetX", "2", "750.00")])


def test_a_text_file_of_an_unknown_type_is_read_by_the_model_and_held():
    """A .dat export has no parser. It is text, so the model reads it; the values are grounded against the file and the invoice is held with the file type on the flag."""
    invoice, method = read(UNFAMILIAR / "invoice_3004.dat", Model(T3004))
    assert method == "llm_fallback" and "NOT_IN_SOURCE" not in codes(invoice)
    assert [f for f in invoice.flags if f["code"] == "UNFAMILIAR_FORMAT"] == [{"code": "UNFAMILIAR_FORMAT", "read_by": "model", "file_type": ".dat"}]
    decision = decide(normalizer.check(invoice, INVENTORY).flags)
    assert (decision.outcome, "UNFAMILIAR_FORMAT" in decision.because) == (HOLD, True)


def test_only_listed_types_are_given_to_the_model_and_only_if_they_are_text(tmp_path):
    """The model is an outside service, and a folder holds more than invoices. A type that isn't on the list is refused unread, whether it is binary (a Word file, an image) or text (notes, a log, source code). A listed type that turns out to be binary is refused too. Nothing of any of them is sent."""
    model = Model(T3004)
    unlisted = {"scan.png": b"\x89PNG\r\n\x1a\n\x00\x00", "invoice.docx": b"PK\x03\x04\x14\x00", "notes.md": b"# Notes\nCall the bank.\n",
                "server.log": b"12:00 started\n", "settings.env": b"API_KEY=secret\n", "README": b"Read me.\n"}
    for name, data in unlisted.items():
        (tmp_path / name).write_bytes(data)
        with pytest.raises(ValueError, match="No reader for .* files .*not a type that is given to the model .*nothing of it was sent"):
            read(tmp_path / name, model)
    (tmp_path / "blob.dat").write_bytes(b"\x00\x01\x02 binary")
    with pytest.raises(ValueError, match="blob.dat is not a text file"):
        read(tmp_path / "blob.dat", model)
    assert model.sent == []
    # The list is the caller's to change: with .md on it, the notes file is sent (and here, found not to be an invoice).
    with pytest.raises(ValueError, match="the model found no invoice in it"):
        read(tmp_path / "notes.md", Model(transcription()), fallback_types={".md"})


def test_an_unknown_type_the_model_cannot_help_with_is_an_error_not_a_guess(tmp_path):
    """Text, but no model to read it, or a model that finds no invoice in it (a README, a log): an error naming the file. Nothing is invented, and an oversized file is not sent at all."""
    notes = tmp_path / "readme.dat"
    notes.write_text("These are the delivery notes for February.\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"No reader for \.dat files \(readme\.dat\).*the model was not available"):
        read(notes, Model())
    with pytest.raises(ValueError, match="the model found no invoice in it"):
        read(notes, Model(transcription()))
    big, model = tmp_path / "dump.dat", Model(T3004)
    big.write_text("x" * 200_001, encoding="utf-8")
    with pytest.raises(ValueError, match="too large to give to the model"):
        read(big, model)
    assert model.sent == []


# --- Grounding against structured text ------------------------------------------------------------

def test_a_csv_is_checked_as_cells_so_a_value_after_a_comma_is_found():
    """In prose "1,250" makes "250" the tail of a number. In a CSV the comma separates cells: "2" in WidgetA,2,250.00 is the value 2, and a quoted "1,250.00" is one value. A comma-separated .dat is read the same way; prose is not."""
    text = 'Part,Units,Each,Total\nWidgetA,2,250.00,"1,250.00"\n'
    reply = transcription(total="1,250.00", lines=[("WidgetA", "2", "250.00")])
    assert ungrounded(reply, text) != []                                   # as raw text, 2 and 250.00 follow commas
    assert ungrounded(reply, as_written(text, ".csv")) == []
    assert ungrounded(reply, as_written(text, ".dat")) == []                # looks like a table: same commas on every line
    prose = "Pay 1,250 now, please.\nThank you, as ever."
    assert as_written(prose, ".dat") == prose and as_written(prose, ".txt") == prose


def test_a_record_is_one_json_object_or_one_xml_element():
    """What counts as a row for a line item in a structured file: any JSON object that holds no array, any XML element under which no tag repeats. A line item may hold smaller objects of its own; a list of line items is never one record."""
    nested = '{"rows": [{"sku": "A",\n "qty": 3, "price": {"amount": 250.00, "currency": "USD"}}, {"sku": "B", "qty": 2}], "n": 1}'
    assert records(nested, ".json") == ['{"amount": 250.00, "currency": "USD"}',
                                        '{"sku": "A", "qty": 3, "price": {"amount": 250.00, "currency": "USD"}}', '{"sku": "B", "qty": 2}']
    assert records('{"note": "a } and a [ in a string", "n": 1}', ".json") == ['{"note": "a } and a [ in a string", "n": 1}']
    one = "<Bill><Ref>R1</Ref><Positions><Position><Article>A</Article>\n<Count>4</Count></Position></Positions></Bill>"
    assert records(one, ".xml") == ["R1 A 4", "A 4", "A 4"]              # one position: the whole bill is one thing
    two = "<Bill><Ref>R1</Ref><Positions><Position sku='A'><Count>4</Count></Position><Position sku='B'><Count>2</Count></Position></Positions></Bill>"
    assert records(two, ".xml") == ["A 4", "B 2"]                        # two: only each position is
    assert records("<Bill><unclosed>", ".xml") == [] and records("a,b", ".csv") == []


def test_a_line_item_with_a_nested_object_is_grounded():
    """A price written as an object inside the line: the item, quantity and amount are still in one record, so a correct transcription passes."""
    text = '{"no": "INV-9", "rows": [\n  {"sku": "WidgetA", "qty": 3,\n   "price": {"amount": 250.00, "currency": "USD"}},\n  {"sku": "WidgetB", "qty": 2,\n   "price": {"amount": 500.00, "currency": "USD"}}]}'
    good = transcription(invoice_number="INV-9", lines=[("WidgetA", "3", "250.00"), ("WidgetB", "2", "500.00")])
    assert ungrounded(good, text, records(text, ".json")) == []
    crossed = transcription(invoice_number="INV-9", lines=[("WidgetA", "2", "250.00")])
    assert [f.get("reason") for f in ungrounded(crossed, text, records(text, ".json"))] == ["NOT_ON_ONE_LINE"]


# --- Fixes from the code review ----------------------------------------------------------------------

def test_a_structured_file_too_large_to_send_keeps_the_parsers_result(tmp_path):
    """The size limit holds for JSON, CSV and XML too: a large export in an unknown layout is not put into a model request. The parser's result stands, saying why the model was not asked."""
    big = tmp_path / "export.json"
    big.write_text('{"invoiceNo": "INV-1", "blob": "' + "x" * 200_000 + '"}', encoding="utf-8")
    model = Model(T3001)
    invoice, method = read(big, model)
    [flag] = [f for f in invoice.flags if f["code"] == "UNFAMILIAR_FORMAT"]
    assert method == "parser" and model.sent == [] and flag["read_by"] == "parser"
    assert "was not asked: at 200,0" in flag["model"] and "too large to give to the model" in flag["model"]


def test_a_file_holding_several_invoices_is_not_passed_off_as_one(tmp_path):
    """The model returns one invoice. A JSON list of several records is refused before anything is sent. A file the model does read that names other invoice numbers carries a flag listing them, so the person holding it knows only one was read."""
    batch = tmp_path / "batch.json"
    batch.write_text('[{"invoiceNo": "INV-1"}, {"invoiceNo": "INV-2"}, {"invoiceNo": "INV-3"}]', encoding="utf-8")
    model = Model(T3004)
    with pytest.raises(ValueError, match="top level is a JSON list.*a list of 3 records.*nothing of it was sent"):
        read(batch, model)
    assert model.sent == []

    dat = tmp_path / "batch.dat"
    dat.write_text((UNFAMILIAR / "invoice_3004.dat").read_text(encoding="utf-8") + "HDR|INV-3005|Coastal Bearings Co.|2026-02-07|2026-03-09|NET30\nLIN|WidgetB|1|500.00\nTOT|500.00\n")
    invoice, method = read(dat, Model(T3004))
    assert method == "llm_fallback"
    assert [f for f in invoice.flags if f["code"] == "POSSIBLY_SEVERAL_INVOICES"] == [
        {"code": "POSSIBLY_SEVERAL_INVOICES", "read": "INV-3004", "also_in_file": ["INV-3005"]}]
    assert "POSSIBLY_SEVERAL_INVOICES" in decide(normalizer.check(invoice, INVENTORY).flags).because
    single, _ = read(UNFAMILIAR / "invoice_3004.dat", Model(T3004))
    assert "POSSIBLY_SEVERAL_INVOICES" not in codes(single)             # one number in the file: nothing to say


def test_text_in_the_usual_windows_encodings_is_read(tmp_path):
    """An export is not always UTF-8. UTF-16 with its byte-order mark, UTF-8 with one, and Windows-1252 are all text a person can read, and are read. A .txt that is really binary says so, without claiming .txt has no reader."""
    from extraction import read_text
    text = (UNFAMILIAR / "invoice_3004.dat").read_text(encoding="utf-8") + "NOTE|Total due \u00a33000\n"
    for name, data in (("utf16.dat", text.encode("utf-16")), ("bom.dat", text.encode("utf-8-sig")), ("win.dat", text.encode("cp1252"))):
        (tmp_path / name).write_bytes(data)
        assert read_text(tmp_path / name) == text, name
        invoice, method = read(tmp_path / name, Model(T3004))
        assert method == "llm_fallback" and "NOT_IN_SOURCE" not in codes(invoice), name
    (tmp_path / "scan.txt").write_bytes(b"\x00\x01\x02\x03")
    with pytest.raises(ValueError, match="^scan.txt is not a text file"):
        read(tmp_path / "scan.txt", Model())


def test_the_extraction_command_and_the_report_read_files_as_the_pipeline_does():
    """One function decides how a file is read. extraction.normalize and the self-correction report go through it, so an unfamiliar format is read by the model there too, not parsed into an empty invoice or skipped."""
    from extraction import Replay, normalize
    from extraction.report import measure
    replay = Replay(ROOT / "data" / "replays")
    results = {Path(r["file"]).name: r for r in normalize([UNFAMILIAR], replay, INVENTORY)}
    assert sorted(results) == ["invoice_3001.json", "invoice_3002.csv", "invoice_3003.xml", "invoice_3004.dat"]
    assert results["invoice_3004.dat"]["invoice"]["invoice_number"] == "INV-3004"
    assert results["invoice_3001.json"]["invoice"]["vendor_name"] == "Northwind Components"
    rows = {r["file"]: r for r in measure([UNFAMILIAR], replay, INVENTORY)}
    assert "error" not in rows["invoice_3003.xml"] and rows["invoice_3004.dat"]["after"]["holding"] == ["UNFAMILIAR_FORMAT"]


# --- The real model on the four samples, from its recorded replies --------------------------------

@pytest.fixture(scope="module")
def unfamiliar(tmp_path_factory):
    """data/unfamiliar run through the whole pipeline offline: file name -> final state."""
    from extraction import Replay
    from ledger import Ledger
    from pipeline import build_graph, run
    graph = build_graph(Replay(ROOT / "data" / "replays"), INVENTORY, Ledger(":memory:", pay=lambda *args: None))
    return {p.name: run(graph, p) for p in sorted(UNFAMILIAR.iterdir())}


def test_the_model_reads_a_dat_file_and_two_unfamiliar_layouts_correctly_and_they_are_held(unfamiliar):
    """Real model output. An unlabelled pipe-separated .dat, a JSON file with a supplier's own field names and an XML file with another root element are each read in one attempt with every value grounded, and held for a person on the unfamiliar format alone."""
    expected = {"invoice_3001.json": ("INV-3001", "Northwind Components", "1890.00", 2),
                "invoice_3003.xml": ("INV-3003", "Ridgeline Fasteners", "2000.00", 1),
                "invoice_3004.dat": ("INV-3004", "Coastal Bearings Co.", "3000.00", 2)}
    for name, (number, vendor, total, lines) in expected.items():
        state = unfamiliar[name]
        invoice = state["invoice"]
        assert state["trail"][0] == {"node": "ingest", "method": "llm_fallback", "model_attempts": 1}, name
        assert (invoice.invoice_number, invoice.vendor_name, str(invoice.total), len(invoice.line_items)) == (number, vendor, total, lines), name
        assert "NOT_IN_SOURCE" not in codes(invoice), name
        assert (state["result"]["status"], state["decision"].because) == ("HELD", ("UNFAMILIAR_FORMAT",)), name
        assert state["assessment"]["by"] == "reviewer_agent" and state["critique"]["verdict"] == "ACCEPT", name
    lines = [(li.item, str(li.quantity), str(li.unit_price)) for li in unfamiliar["invoice_3004.dat"]["invoice"].line_items]
    assert lines == [("WidgetA", "6", "250.00"), ("GadgetX", "2", "750.00")]


def test_a_misreading_by_the_model_is_caught_by_the_arithmetic_and_rejected(unfamiliar):
    """Real model output, and a mistake. The CSV repeats its grand total on every row; the model took it for each line's amount and left the invoice total empty. Every value it returned is in the file, so grounding passes: grounding shows a value wasn't invented, not that it is in the right field. The line arithmetic doesn't reconcile and there is no total, so the invoice is rejected, not paid."""
    state = unfamiliar["invoice_3002.csv"]
    invoice = state["invoice"]
    assert state["trail"][0]["method"] == "llm_fallback" and "NOT_IN_SOURCE" not in codes(invoice)
    assert invoice.total is None and [str(li.amount) for li in invoice.line_items] == ["1250.00", "1250.00"]
    assert (state["result"]["status"], state["decision"].rule) == ("REJECTED", "BLOCKING_FLAG")
    assert set(state["decision"].because) == {"TOTAL_MISSING", "LINE_AMOUNT_MISMATCH"}


def test_a_file_with_windows_line_endings_is_the_same_invoice_and_finds_its_recording(tmp_path):
    """A checkout on Windows can turn every line ending into CRLF. The text read is the same either way, so the recording made from the LF file is found and the invoice comes out identical. Without this, every replay missed on Windows."""
    from extraction import Replay, extract, read_text
    replay = Replay(ROOT / "data" / "replays")
    for name in ("invoice_1001.txt", "invoice_1012.txt"):
        original = (INVOICES / name).read_bytes()
        assert b"\r\n" not in original
        windows = tmp_path / name
        windows.write_bytes(original.replace(b"\n", b"\r\n"))
        assert read_text(windows) == read_text(INVOICES / name) and "\r" not in read_text(windows)
        assert extract(windows, replay) == extract(INVOICES / name, replay)       # no "no recording" error: the hash matched
    mac = tmp_path / "old_mac.txt"
    mac.write_bytes(b"Invoice INV-1\rTotal 5\r")
    assert read_text(mac) == "Invoice INV-1\nTotal 5\n"


def test_a_path_in_a_run_log_reads_the_same_on_every_system():
    """A Path is logged with forward slashes, including one built the Windows way."""
    import json
    from pathlib import PureWindowsPath
    from pipeline.graph import _plain
    assert json.loads(json.dumps({"p": PureWindowsPath("x\\y.txt"), "q": Path("x") / "y.txt"}, default=_plain)) == {"p": "x/y.txt", "q": "x/y.txt"}
