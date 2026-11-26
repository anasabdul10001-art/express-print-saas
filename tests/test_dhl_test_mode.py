"""A DHL test connection never touches orders; the test label is stamped and tied to nothing."""

import base64
import io
from types import SimpleNamespace

from fpdf import FPDF
from pypdf import PdfReader
from sqlalchemy import create_engine, text

from app.config import settings
from app.services import dhl_service, dhl_test_service
from tests.test_print_jobs import (DHL_CONNECT_PAYLOAD, _make_agent, _make_order, _make_product,
                                   _set_recipient_address)


def _pdf() -> bytes:
    pdf = FPDF(unit="pt", format=(288, 432))
    pdf.add_page()
    pdf.set_font("Helvetica", "", 12)
    pdf.text(20, 40, "DHL LABEL")
    return bytes(pdf.output())


def _connected(client, register, production=False):
    headers, _ = register()
    assert client.post("/dhl/connect", headers=headers, json=DHL_CONNECT_PAYLOAD).status_code == 200
    tenant = client.get("/auth/me", headers=headers).json()["tenant_id"]
    if production:
        with create_engine(settings.database_url).begin() as conn:
            conn.execute(text("UPDATE dhl_accounts SET environment='PRODUCTION' WHERE tenant_id=:t"), {"t": tenant})
    return headers


def _label_service(monkeypatch, content=None, error=None):
    calls = []

    def create(account, order, *package):
        calls.append(order)
        if error:
            raise dhl_service.DhlShipmentError(error)
        return SimpleNamespace(label_pdf=_pdf() if content is None else content, shipment_number="1",
                               tracking_number="1")

    monkeypatch.setattr(dhl_service, "create_shipment_label", create)
    return calls


def _rows(sql):
    with create_engine(settings.database_url).connect() as conn:
        return conn.execute(text(sql)).fetchall()


def test_a_test_connection_is_never_used_for_an_order(client, register, monkeypatch):
    calls = _label_service(monkeypatch)
    headers = _connected(client, register)
    _make_agent(client, headers)
    order = _make_order(client, headers, _make_product(client, headers))
    _set_recipient_address(order["id"])
    r = client.post(f"/print-jobs/order/{order['id']}", headers=headers)
    assert r.status_code == 400 and "Testmodus" in r.json()["detail"] and calls == []
    after = client.get("/orders", headers=headers).json()[0]
    assert after["tracking_number"] is None and after["status"] != "ready_to_print"


def test_the_uploaded_label_still_prints_while_the_test_connection_exists(client, register, monkeypatch):
    calls = _label_service(monkeypatch)
    headers = _connected(client, register)
    _make_agent(client, headers)
    order = _make_order(client, headers, _make_product(client, headers))
    _set_recipient_address(order["id"])
    assert client.post(f"/print-jobs/order/{order['id']}/upload-label", headers=headers,
                       files={"file": ("ebay.pdf", _pdf(), "application/pdf")}).status_code == 201
    assert client.post(f"/print-jobs/order/{order['id']}", headers=headers).status_code == 201
    assert calls == []


def test_the_test_label_uses_a_made_up_address_is_stamped_and_changes_nothing(client, register, monkeypatch):
    calls = _label_service(monkeypatch)
    headers = _connected(client, register)
    order = _make_order(client, headers, _make_product(client, headers))
    _set_recipient_address(order["id"])
    before = client.get("/orders", headers=headers).json()
    r = client.post("/dhl/test-label", headers=headers)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    page = PdfReader(io.BytesIO(r.content)).pages[0].extract_text()
    assert dhl_test_service.STAMP in page and "DHL LABEL" in page
    assert [c.recipient_name for c in calls] == ["Max Mustermann"]
    assert str(calls[0].id) != order["id"]
    assert client.get("/orders", headers=headers).json() == before
    assert _rows("SELECT id FROM print_jobs") == []


def test_no_test_label_without_a_sandbox_connection(client, register, monkeypatch):
    calls = _label_service(monkeypatch)
    plain, _ = register()
    assert client.post("/dhl/test-label", headers=plain).status_code == 400
    production = _connected(client, register, production=True)
    r = client.post("/dhl/test-label", headers=production)
    assert r.status_code == 400 and "kostenpflichtige" in r.json()["detail"] and calls == []
    assert client.post("/dhl/test-label/print", headers=production,
                       files={"file": ("t.pdf", _pdf(), "application/pdf")}).status_code == 400
    assert client.post("/dhl/test-label").status_code == 401


def test_dhl_errors_and_unreadable_labels_are_reported_not_handed_out(client, register, monkeypatch):
    headers = _connected(client, register)
    _label_service(monkeypatch, error="DHL hat die Anfrage abgelehnt (Status 400): billing number")
    r = client.post("/dhl/test-label", headers=headers)
    assert r.status_code == 502 and "billing number" in r.json()["detail"]
    _label_service(monkeypatch, content=b"not a pdf")
    r = client.post("/dhl/test-label", headers=headers)
    assert r.status_code == 502 and "keine lesbare PDF" in r.json()["detail"]


def test_printing_the_test_label_queues_a_stamped_job_without_an_order(client, register):
    headers = _connected(client, register)
    url = "/dhl/test-label/print"
    pdf = {"file": ("t.pdf", _pdf(), "application/pdf")}
    assert client.post(url, headers=headers, files=pdf).status_code == 400  # no printer yet
    _make_agent(client, headers)
    assert client.post(url, headers=headers, files={"file": ("t.txt", b"hello", "text/plain")}).status_code == 400
    assert client.post(url, headers=headers, files=pdf).status_code == 201
    (order_id, data), = _rows("SELECT order_id, label_pdf_data FROM print_jobs")
    assert order_id is None
    assert dhl_test_service.STAMP in PdfReader(io.BytesIO(bytes(data))).pages[0].extract_text()


def test_the_made_up_order_works_with_the_real_request_builder(client, register, monkeypatch):
    headers = _connected(client, register)
    sent = {}

    def post(url, json, auth, headers, timeout):
        sent.update(url=url, body=json)
        return SimpleNamespace(status_code=200, json=lambda: {
            "items": [{"shipmentNo": "00340", "label": {"b64": base64.b64encode(_pdf()).decode()}}]})

    monkeypatch.setattr(dhl_service.httpx, "post", post)
    assert client.post("/dhl/test-label", headers=headers).status_code == 200
    shipment = sent["body"]["shipments"][0]
    assert sent["url"].startswith("https://api-sandbox.dhl.com/")
    assert shipment["billingNumber"] == "33333333330101" and len(shipment["refNo"]) == 32
    assert shipment["consignee"]["addressHouse"] == "20" and shipment["consignee"]["country"] == "DEU"
