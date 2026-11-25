"""Supplier invoices and the weighted average unit cost."""
import json
from decimal import Decimal

from app.services import purchase_service

PDF = b"%PDF-1.4 test invoice"


def _product(client, headers, sku, stock, cost=None, shipping=None):
    body = {"sku": sku, "title": f"Artikel {sku}", "selling_price": 5, "stock_quantity": stock}
    if cost is not None:
        body["purchase_price"] = cost
    if shipping is not None:
        body["supplier_shipping_cost"] = shipping
    return client.post("/products", headers=headers, json=body).json()["id"]


def _data(lines, **extra):
    return {"supplier": "Lieferant", "invoice_number": "A-1", "invoice_date": "2026-11-20",
            "lines": [{"product_id": p, "quantity": q, "line_total": t} for p, q, t in lines], **extra}


def _book(client, headers, data, content=PDF, name="Rechnung März.pdf"):
    return client.post("/purchases", headers=headers, data={"data": json.dumps(data)},
                       files={"file": (name, content, "application/pdf")})


def _get(client, headers, product_id):
    return next(p for p in client.get("/products", headers=headers).json() if p["id"] == product_id)


def _cost(client, headers, product_id):
    p = _get(client, headers, product_id)
    return (Decimal(str(p["purchase_price"])), Decimal(str(p["supplier_shipping_cost"])), p["stock_quantity"])


def test_the_example_ten_at_1_00_plus_thirty_at_0_60(client, register):
    headers, _ = register()
    pid = _product(client, headers, "A", 10, cost=1.0)
    r = _book(client, headers, _data([(pid, 30, "18.00")]))
    assert r.status_code == 201, r.text
    assert _cost(client, headers, pid) == (Decimal("0.70"), Decimal("0"), 40)


def test_extra_costs_are_split_per_piece_over_all_lines(client, register):
    headers, _ = register()
    a = _product(client, headers, "A", 10, cost=1.0, shipping=0.10)
    b = _product(client, headers, "B", 0)
    # 8.00 shipping over 40 pieces = 0.20 each
    r = _book(client, headers, _data([(a, 10, "10.00"), (b, 30, "15.00")], extra_costs="8.00", expected_total="33.00"))
    assert r.status_code == 201, r.text
    assert r.json()["total"] == "33.00"
    assert _cost(client, headers, a) == (Decimal("1.00"), Decimal("0.15"), 20)
    assert _cost(client, headers, b) == (Decimal("0.50"), Decimal("0.20"), 30)


def test_stock_already_counted_is_not_added_twice(client, register):
    headers, _ = register()
    pid = _product(client, headers, "A", 40, cost=1.0)   # the 30 new pieces are already in the 40
    assert _book(client, headers, _data([(pid, 30, "18.00")], add_to_stock=False)).status_code == 201
    assert _cost(client, headers, pid) == (Decimal("0.70"), Decimal("0"), 40)


def test_preview_changes_nothing(client, register):
    headers, _ = register()
    pid = _product(client, headers, "A", 10, cost=1.0)
    r = client.post("/purchases/preview", headers=headers, json=_data([(pid, 30, "18.00")]))
    assert r.status_code == 200 and r.json()["lines"][0]["new_purchase_price"] == "0.70"
    assert _get(client, headers, pid)["stock_quantity"] == 10
    assert client.get("/purchases", headers=headers).json() == []


def test_wrong_total_duplicate_invoice_and_bad_file_are_refused(client, register):
    headers, _ = register()
    pid = _product(client, headers, "A", 10, cost=1.0)
    r = _book(client, headers, _data([(pid, 30, "18.00")], expected_total="19.00"))
    assert r.status_code == 422 and "18.00" in r.json()["detail"]
    assert _book(client, headers, _data([(pid, 30, "18.00")]), content=b"<html>", name="x.pdf").status_code == 400
    assert _book(client, headers, _data([(pid, 1, "1.00"), (pid, 1, "1.00")])).status_code == 422
    assert _get(client, headers, pid)["stock_quantity"] == 10
    assert _book(client, headers, _data([(pid, 30, "18.00")])).status_code == 201
    assert _book(client, headers, _data([(pid, 30, "18.00")])).status_code == 409
    assert _get(client, headers, pid)["stock_quantity"] == 40


def test_file_can_be_downloaded_only_by_its_owner(client, register):
    headers, _ = register()
    other, _ = register()
    pid = _product(client, headers, "A", 1, cost=1.0)
    purchase = _book(client, headers, _data([(pid, 1, "1.00")])).json()
    got = client.get(f"/purchases/{purchase['id']}/file", headers=headers)
    assert got.content == PDF and "M%C3%A4rz" in got.headers["content-disposition"]
    assert client.get(f"/purchases/{purchase['id']}/file", headers=other).status_code == 404
    assert client.post(f"/purchases/{purchase['id']}/cancel", headers=other).status_code == 404
    assert _book(client, other, _data([(pid, 1, "1.00")])).status_code == 404


def test_cancel_restores_and_respects_newer_bookings(client, register):
    headers, _ = register()
    pid = _product(client, headers, "A", 10, cost=1.0)
    first = _book(client, headers, _data([(pid, 30, "18.00")])).json()
    second = _book(client, headers, _data([(pid, 10, "20.00")], invoice_number="A-2")).json()
    assert client.post(f"/purchases/{first['id']}/cancel", headers=headers).status_code == 409
    assert client.post(f"/purchases/{second['id']}/cancel", headers=headers).status_code == 200
    assert client.post(f"/purchases/{first['id']}/cancel", headers=headers).status_code == 200
    p = _get(client, headers, pid)
    assert Decimal(str(p["purchase_price"])) == Decimal("1.00") and p["stock_quantity"] == 10
    assert p["supplier_shipping_cost"] is None
    assert client.post(f"/purchases/{first['id']}/cancel", headers=headers).status_code == 409


def test_average_function_edges():
    avg = purchase_service.average
    assert avg(0, Decimal("5"), 10, Decimal("1")) == Decimal("1.00")
    assert avg(10, None, 10, Decimal("1.005")) == Decimal("1.01")
    assert purchase_service.old_pieces(5, 30, add_to_stock=False) == 0
