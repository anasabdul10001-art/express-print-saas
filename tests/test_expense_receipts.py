"""Receipts on expenses."""
import uuid

from app.database import SessionLocal
from app.models.expense import ExpenseReceipt

PDF = b"%PDF-1.4 receipt"
YEAR = {"start": "2026-01-01", "end": "2026-12-31"}


def _expense(client, headers, **extra):
    body = {"description": "Kartons", "amount": 12.5, "expense_date": "2026-06-01", **extra}
    r = client.post("/expenses", headers=headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _upload(client, headers, expense_id, content=PDF, name="Beleg Juni.pdf"):
    return client.post(f"/expenses/{expense_id}/receipts", headers=headers,
                       files={"file": (name, content, "application/pdf")})


def _count():
    with SessionLocal() as db:
        return db.query(ExpenseReceipt).count()


def test_supplier_number_and_receipt_are_stored_and_downloadable(client, register):
    headers, _ = register()
    eid = _expense(client, headers, supplier="Karton GmbH", invoice_number="R-9")
    r = _upload(client, headers, eid)
    assert r.status_code == 201, r.text
    row = client.get("/expenses", headers=headers).json()[0]
    assert (row["supplier"], row["invoice_number"]) == ("Karton GmbH", "R-9")
    assert row["receipts"] == [{"id": r.json()["id"], "file_name": "Beleg Juni.pdf"}]
    got = client.get(f"/expenses/{eid}/receipts/{r.json()['id']}", headers=headers)
    assert got.content == PDF and got.headers["x-content-type-options"] == "nosniff"
    assert got.headers["content-disposition"].startswith("attachment")
    changed = client.patch(f"/expenses/{eid}", headers=headers, json={"supplier": "Neu AG"})
    assert changed.json()["supplier"] == "Neu AG" and len(changed.json()["receipts"]) == 1


def test_only_real_pdf_or_pictures_and_not_more_than_five(client, register):
    headers, _ = register()
    eid = _expense(client, headers)
    assert _upload(client, headers, eid, b"<html>", "x.pdf").status_code == 400
    assert _upload(client, headers, eid, b"%PDF" + b"0" * (8 * 1024 * 1024), "big.pdf").status_code == 400
    for _ in range(5):
        assert _upload(client, headers, eid).status_code == 201
    assert _upload(client, headers, eid).status_code == 400
    assert _count() == 5


def test_another_tenant_cannot_see_add_or_delete(client, register):
    owner, _ = register()
    eid = _expense(client, owner)
    rid = _upload(client, owner, eid).json()["id"]
    other, _ = register()
    assert client.get(f"/expenses/{eid}/receipts/{rid}", headers=other).status_code == 404
    assert client.delete(f"/expenses/{eid}/receipts/{rid}", headers=other).status_code == 404
    assert _upload(client, other, eid).status_code == 404
    assert client.get(f"/expenses/{eid}/receipts/{rid}").status_code in (401, 403, 422)
    assert _count() == 1


def test_deleting_a_receipt_or_the_expense_removes_the_file(client, register):
    headers, _ = register()
    eid = _expense(client, headers)
    first = _upload(client, headers, eid).json()["id"]
    _upload(client, headers, eid)
    assert client.delete(f"/expenses/{eid}/receipts/{first}", headers=headers).status_code == 204
    assert _count() == 1
    assert client.delete(f"/expenses/{eid}", headers=headers).status_code == 204
    assert _count() == 0


def test_the_euer_names_the_receipt_and_warns_when_one_is_missing(client, register):
    headers, _ = register()
    with_receipt = _expense(client, headers, supplier="Karton GmbH", invoice_number="R-9")
    _upload(client, headers, with_receipt)
    _expense(client, headers, description="Strom")
    d = client.get("/accounting/euer", headers=headers, params=YEAR).json()
    assert d["checks"]["expenses_without_receipt"] == 1
    assert any("ohne hochgeladenen Beleg" in w for w in d["warnings"])
    csv = client.get("/accounting/euer/bookings.csv", headers=headers, params=YEAR).text
    assert "(Karton GmbH R-9)" in csv
