"""The ZIP with all bookings and their documents."""
import csv
import io
import json
import uuid
import zipfile
from datetime import date

import pytest

from app.core.rate_limit import limiter
from app.database import SessionLocal
from app.models.sales_invoice import SalesInvoice
from app.services import export_service

YEAR = {"start": "2026-01-01", "end": "2026-12-31"}


@pytest.fixture(autouse=True)
def _reset_limit():
    limiter.reset()


def _zip(client, headers, **period):
    r = client.get("/accounting/euer/export.zip", headers=headers, params=period or YEAR)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    return zipfile.ZipFile(io.BytesIO(r.content))


def _text(archive, name):
    return archive.read(name).decode("utf-8-sig")


def _fill(client, headers):
    pid = client.post("/products", headers=headers, json={"sku": "A", "title": "A", "selling_price": 5,
                                                          "stock_quantity": 0}).json()["id"]
    data = {"supplier": "Shenzhen", "invoice_number": "S-1", "invoice_date": "2026-03-01", "paid_date": "2026-03-02",
            "lines": [{"product_id": pid, "quantity": 10, "line_total": "20.00"}]}
    r = client.post("/purchases", headers=headers, data={"data": json.dumps(data)},
                    files={"file": ("../../böse datei.pdf", b"%PDF-1.4 purchase", "application/pdf")})
    assert r.status_code == 201, r.text
    paid = client.post("/expenses", headers=headers, json={"description": "Kartons", "amount": 12.5,
                                                           "expense_date": "2026-06-01"}).json()["id"]
    for content in (b"%PDF-1.4 one", b"%PDF-1.4 two"):
        client.post(f"/expenses/{paid}/receipts", headers=headers,
                    files={"file": ("beleg.pdf", content, "application/pdf")})
    client.post("/expenses", headers=headers, json={"description": "Strom", "amount": 30, "expense_date": "2026-07-01"})


def test_the_zip_holds_every_booking_with_its_documents(client, register):
    headers, _ = register()
    _fill(client, headers)
    tenant_id = uuid.UUID(client.get("/auth/me", headers=headers).json()["tenant_id"])
    with SessionLocal() as db:
        db.add(SalesInvoice(tenant_id=tenant_id, number="RE-2026/0001", issue_date=date(2026, 4, 1), gross_amount=5,
                            data="{}", pdf=b"%PDF-1.4 sale", xml=b"<xml/>"))
        db.add(SalesInvoice(tenant_id=tenant_id, number="RE-2025/0009", issue_date=date(2025, 4, 1), gross_amount=5,
                            data="{}", pdf=b"%PDF-1.4 old"))
        db.commit()
    archive = _zip(client, headers)
    names = archive.namelist()
    assert {"euer-buchungen.csv", "euer-uebersicht.csv", "LIESMICH.txt",
            "ausgangsrechnungen/RE-2026_0001.pdf", "ausgangsrechnungen/RE-2026_0001.xml"} <= set(names)
    assert not any(".." in n or n.startswith("/") or "\\" in n for n in names)
    assert not any("0009" in n for n in names)

    rows = list(csv.reader(io.StringIO(_text(archive, "euer-buchungen.csv")), delimiter=";"))
    files = {row[4]: row[5].split(", ") if row[5] else [] for row in rows[1:]}
    purchase = files["Shenzhen S-1"]
    assert len(purchase) == 1 and archive.read(purchase[0]) == b"%PDF-1.4 purchase"
    receipts = files["Sonstiges: Kartons"]
    assert sorted(archive.read(f) for f in receipts) == [b"%PDF-1.4 one", b"%PDF-1.4 two"]
    assert files["Sonstiges: Strom"] == []
    notes = _text(archive, "LIESMICH.txt")
    assert "Strom" in notes.split("ohne hochgeladenen Beleg (")[1] and "Kartons" not in notes.split("liegen nicht bei):")[1]
    assert "Überschuss / Verlust;" in _text(archive, "euer-uebersicht.csv")


def test_another_tenant_gets_nothing_of_it_and_login_is_needed(client, register):
    owner, _ = register()
    _fill(client, owner)
    other, _ = register()
    archive = _zip(client, other)
    assert sorted(archive.namelist()) == ["LIESMICH.txt", "euer-buchungen.csv", "euer-uebersicht.csv"]
    assert client.get("/accounting/euer/export.zip", params=YEAR).status_code in (401, 403, 422)


def test_an_oversized_export_is_refused_with_a_message(client, register, monkeypatch):
    headers, _ = register()
    _fill(client, headers)
    monkeypatch.setattr(export_service, "MAX_DOCUMENT_BYTES", 20)
    r = client.get("/accounting/euer/export.zip", headers=headers, params=YEAR)
    assert r.status_code == 400 and "kürzeren" in r.json()["detail"]
