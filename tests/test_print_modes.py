"""The integrated print mode. DHL is faked: no label is ever really bought."""
import io
from types import SimpleNamespace

import pytest
from fpdf import FPDF
from pypdf import PdfReader

from app.database import SessionLocal
from app.models.dhl_account import DhlAccount
from app.models.order import Order
from app.models.print_agent import PrintAgent
from app.models.print_job import PrintJob
from app.services import dhl_service, picking_slip_service

ADDRESS = dict(recipient_name="Erika Müller", recipient_street1="Weg 2", recipient_zip="01067",
               recipient_city="Dresden", recipient_country_code="DE")


def _label() -> bytes:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.cell(0, 10, "SHIPPING LABEL")
    return bytes(pdf.output())


def _tenant(client, register, mode):
    headers, _ = register()
    tenant_id = client.get("/auth/me", headers=headers).json()["tenant_id"]
    r = client.patch("/tenants/me/print-mode", headers=headers, json={"print_mode": mode})
    assert r.status_code == 200, r.text
    return headers, tenant_id


def _order(client, headers, sku="P1", **fields):
    pid = client.post("/products", headers=headers, json={
        "sku": sku, "title": "Kabel ümlaut", "selling_price": 5, "stock_quantity": 9, "shelf_location": "A3",
        "image_url": "https://evil.example/x.png"}).json()["id"]
    order_id = client.post("/orders", headers=headers, json={
        "external_order_ref": f"R-{sku}", "items": [{"product_id": pid, "quantity": 2}]}).json()["id"]
    with SessionLocal() as db:
        order = db.get(Order, order_id)
        for name, value in fields.items():
            setattr(order, name, value)
        db.commit()
    return order_id


def _with_dhl_and_agent(tenant_id):
    with SessionLocal() as db:
        db.add(DhlAccount(tenant_id=tenant_id, status="CONNECTED", billing_number="1", environment="PRODUCTION",
                          sender_name="S", sender_street="Str 1", sender_zip="04720", sender_city="D",
                          sender_country="DE"))
        db.add(PrintAgent(tenant_id=tenant_id, name="a", api_key_hash=f"h{tenant_id}", status="online"))
        db.commit()


@pytest.fixture
def dhl(monkeypatch):
    calls = []

    def buy(account, order, *package):
        calls.append(order.external_order_ref)
        return SimpleNamespace(label_pdf=_label(), tracking_number=f"T{len(calls)}")

    monkeypatch.setattr(dhl_service, "create_shipment_label", buy)
    return calls


def _text(content: bytes) -> list[str]:
    return [page.extract_text() for page in PdfReader(io.BytesIO(content)).pages]


def test_integrated_download_adds_one_slip_and_keeps_the_stored_label(client, register, dhl):
    headers, tenant_id = _tenant(client, register, "INTEGRATED")
    _with_dhl_and_agent(tenant_id)
    order_id = _order(client, headers, **ADDRESS)
    job = client.post(f"/print-jobs/order/{order_id}", headers=headers)
    assert job.status_code == 201, job.text
    for _ in range(2):
        pages = _text(client.get(f"/print-jobs/{job.json()['id']}/label").content)
        assert len(pages) == 2 and "SHIPPING LABEL" in pages[0]
        assert "Packzettel R-P1" in pages[1] and "A3" in pages[1] and "2 x" in pages[1]
    with SessionLocal() as db:
        assert len(_text(db.get(PrintJob, job.json()["id"]).label_pdf_data)) == 1


def test_pictures_only_come_from_marketplace_hosts(monkeypatch):
    seen = []
    monkeypatch.setattr(picking_slip_service.httpx, "get", lambda url, **k: seen.append(url))
    for url in ("https://evil.example/a.png", "http://i.ebayimg.com/a.png", "https://127.0.0.1/a", "file:///etc/passwd",
                "https://i.ebayimg.com.evil.example/a", None):
        assert picking_slip_service.fetch_picture(url) is None
    assert seen == []


def test_instant_mode_never_buys_by_itself(client, register, dhl):
    # Instant buying is postponed: choosing the mode must not spend any money.
    headers, tenant_id = _tenant(client, register, "INSTANT")
    _with_dhl_and_agent(tenant_id)
    _order(client, headers, **ADDRESS)
    assert dhl == []

