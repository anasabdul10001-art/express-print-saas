"""Product CRUD, stock movements, and package weight/dimension fields."""


def test_create_and_list_product(client, register):
    headers, _ = register()
    response = client.post("/products", headers=headers, json={
        "sku": "SKU-1", "title": "Test Widget", "purchase_price": 5, "selling_price": 10, "stock_quantity": 3,
    })
    assert response.status_code == 201
    body = response.json()
    assert body["sku"] == "SKU-1"
    assert float(body["profit_per_unit"]) == 5
    assert float(body["margin_percent"]) == 50.0

    listed = client.get("/products", headers=headers)
    assert listed.status_code == 200
    assert len(listed.json()) == 1


def test_duplicate_sku_rejected(client, register):
    headers, _ = register()
    payload = {"sku": "DUPE-SKU", "title": "A"}
    assert client.post("/products", headers=headers, json=payload).status_code == 201
    response = client.post("/products", headers=headers, json={**payload, "title": "B"})
    assert response.status_code == 409


def test_negative_weight_rejected(client, register):
    headers, _ = register()
    response = client.post("/products", headers=headers, json={"sku": "SKU-W", "title": "A", "weight_kg": -1})
    assert response.status_code == 422


def test_product_weight_and_dimensions_roundtrip(client, register):
    headers, _ = register()
    created = client.post("/products", headers=headers, json={
        "sku": "SKU-DIM", "title": "Boxed thing", "weight_kg": 0.75, "length_cm": 20, "width_cm": 15, "height_cm": 10,
    }).json()
    assert float(created["weight_kg"]) == 0.75
    assert created["length_cm"] == 20

    updated = client.patch(f"/products/{created['id']}", headers=headers, json={"weight_kg": 1.2}).json()
    assert float(updated["weight_kg"]) == 1.2
    assert updated["length_cm"] == 20  # untouched fields survive a partial PATCH


def test_restock_logs_stock_movement(client, register):
    headers, _ = register()
    product = client.post("/products", headers=headers, json={"sku": "SKU-R", "title": "A", "stock_quantity": 5}).json()

    response = client.post(f"/products/{product['id']}/restock", headers=headers, json={"quantity": 10})
    assert response.status_code == 200
    assert response.json()["stock_quantity"] == 15

    movements = client.get(f"/products/{product['id']}/movements", headers=headers).json()
    assert len(movements) == 1
    assert movements[0]["quantity_change"] == 10
    assert movements[0]["reason"] == "restock"


def test_deactivate_product_is_soft_delete(client, register):
    headers, _ = register()
    product = client.post("/products", headers=headers, json={"sku": "SKU-D", "title": "A"}).json()

    response = client.delete(f"/products/{product['id']}", headers=headers)
    assert response.status_code == 204

    fetched = client.get(f"/products/{product['id']}", headers=headers).json()
    assert fetched["active"] is False


def test_products_are_isolated_per_tenant(client, register):
    headers_a, _ = register()
    headers_b, _ = register()

    product = client.post("/products", headers=headers_a, json={"sku": "SAME-SKU", "title": "Tenant A's product"}).json()

    # Same SKU is fine for a different tenant - uniqueness is per-tenant, not global.
    other = client.post("/products", headers=headers_b, json={"sku": "SAME-SKU", "title": "Tenant B's product"})
    assert other.status_code == 201

    assert client.get(f"/products/{product['id']}", headers=headers_b).status_code == 404
    assert len(client.get("/products", headers=headers_a).json()) == 1
    assert len(client.get("/products", headers=headers_b).json()) == 1
