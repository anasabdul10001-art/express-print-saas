"""Manual order creation, stock decrement, and profit calculation."""


def _make_product(client, headers, **overrides):
    payload = {"sku": "ORD-SKU", "title": "Order Test Product", "purchase_price": 10, "selling_price": 25, "stock_quantity": 5}
    payload.update(overrides)
    return client.post("/products", headers=headers, json=payload).json()


def test_create_order_decrements_stock_and_computes_profit(client, register):
    headers, _ = register()
    product = _make_product(client, headers)

    response = client.post("/orders", headers=headers, json={"items": [{"product_id": product["id"], "quantity": 2}]})
    assert response.status_code == 201
    body = response.json()
    assert float(body["total_profit"]) == 30  # 2 * (25 - 10)
    assert body["recipient_address"] is None  # manual orders never have one
    assert body["status"] == "new"

    updated_product = client.get(f"/products/{product['id']}", headers=headers).json()
    assert updated_product["stock_quantity"] == 3


def test_create_order_rejects_insufficient_stock(client, register):
    headers, _ = register()
    product = _make_product(client, headers, stock_quantity=1)

    response = client.post("/orders", headers=headers, json={"items": [{"product_id": product["id"], "quantity": 5}]})
    assert response.status_code == 400

    # Stock must be untouched - the whole order failed before anything committed.
    unchanged = client.get(f"/products/{product['id']}", headers=headers).json()
    assert unchanged["stock_quantity"] == 1


def test_order_summary_reflects_todays_orders(client, register):
    headers, _ = register()
    product = _make_product(client, headers)
    client.post("/orders", headers=headers, json={"items": [{"product_id": product["id"], "quantity": 1}]})

    summary = client.get("/orders/summary", headers=headers).json()
    assert summary["orders_today"] == 1
    assert float(summary["profit_today"]) == 15  # 25 - 10


def test_orders_are_isolated_per_tenant(client, register):
    headers_a, _ = register()
    headers_b, _ = register()

    product_a = _make_product(client, headers_a)
    client.post("/orders", headers=headers_a, json={"items": [{"product_id": product_a["id"], "quantity": 1}]})

    orders_for_b = client.get("/orders", headers=headers_b).json()
    assert orders_for_b == []

    orders_for_a = client.get("/orders", headers=headers_a).json()
    assert len(orders_for_a) == 1
