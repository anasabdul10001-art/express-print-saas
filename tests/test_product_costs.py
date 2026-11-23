"""Per-product costs: purchase + supplier shipping + packaging feed profit."""
from app.database import SessionLocal
from app.models.order import OrderItem


def test_total_cost_drives_profit(client, register):
    headers, _ = register()
    body = {"sku": "C1", "title": "C1", "purchase_price": 1.0, "supplier_shipping_cost": 0.3,
            "packaging_cost": 0.2, "selling_price": 2.99}
    p = client.post("/products", headers=headers, json=body).json()
    assert float(p["unit_cost"]) == 1.5
    assert float(p["profit_per_unit"]) == 1.49


def test_cost_without_purchase_price_is_unknown(client, register):
    headers, _ = register()
    p = client.post("/products", headers=headers, json={"sku": "C2", "title": "C2", "packaging_cost": 0.2, "selling_price": 3}).json()
    assert p["unit_cost"] is None and p["profit_per_unit"] is None


def test_negative_cost_rejected(client, register):
    headers, _ = register()
    r = client.post("/products", headers=headers, json={"sku": "C3", "title": "C3", "packaging_cost": -1})
    assert r.status_code == 422


def test_order_snapshots_total_cost_and_old_orders_get_filled(client, register):
    headers, _ = register()
    pid = client.post("/products", headers=headers, json={"sku": "C4", "title": "C4", "selling_price": 3, "stock_quantity": 10}).json()["id"]
    assert client.post("/orders", headers=headers, json={"items": [{"product_id": pid, "quantity": 2}]}).status_code in (200, 201)
    assert client.get("/orders", headers=headers).json()[0]["total_profit"] is None  # cost unknown yet

    client.patch(f"/products/{pid}", headers=headers,
                 json={"purchase_price": 1, "supplier_shipping_cost": 0.5, "packaging_cost": 0.5})
    after = client.get("/orders", headers=headers).json()[0]
    assert float(after["total_profit"]) == 2.0  # (3 - 2) * 2

    # a later cost change must not rewrite the snapshot of that past order
    client.patch(f"/products/{pid}", headers=headers, json={"purchase_price": 2})
    assert float(client.get("/orders", headers=headers).json()[0]["total_profit"]) == 2.0
    client.post("/orders", headers=headers, json={"items": [{"product_id": pid, "quantity": 1}]})
    profits = sorted(float(o["total_profit"]) for o in client.get("/orders", headers=headers).json())
    assert profits == [0.0, 2.0]  # new order: 3 - (2+0.5+0.5); old order unchanged
