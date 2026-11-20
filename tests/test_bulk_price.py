"""Bulk price change for products."""


def _mk(client, headers, sku, price):
    body = {"sku": sku, "title": sku}
    if price is not None:
        body["selling_price"] = price
    return client.post("/products", headers=headers, json=body).json()["id"]


def _prices(client, headers):
    return {p["sku"]: p["selling_price"] for p in client.get("/products", headers=headers).json()}


def test_preview_does_not_change_then_apply_percent(client, register):
    headers, _ = register()
    _mk(client, headers, "A", 10)
    _mk(client, headers, "B", 20)
    _mk(client, headers, "NOPRICE", None)

    prev = client.post("/products/bulk-price", headers=headers,
                       json={"mode": "percent", "value": -10, "dry_run": True}).json()
    assert prev["changed"] == 2 and prev["skipped_no_price"] == 1 and not prev["applied"]
    assert float(_prices(client, headers)["A"]) == 10

    done = client.post("/products/bulk-price", headers=headers,
                       json={"mode": "percent", "value": -10, "dry_run": False}).json()
    assert done["applied"]
    prices = _prices(client, headers)
    assert float(prices["A"]) == 9 and float(prices["B"]) == 18


def test_selected_only_amount_and_round(client, register):
    headers, _ = register()
    a = _mk(client, headers, "A", 10)
    _mk(client, headers, "B", 20)
    client.post("/products/bulk-price", headers=headers,
                json={"product_ids": [a], "mode": "amount", "value": 2.3, "round_to_99": True, "dry_run": False})
    prices = _prices(client, headers)
    assert float(prices["A"]) == 11.99 and float(prices["B"]) == 20


def test_price_never_below_cent_and_zero_rejected(client, register):
    headers, _ = register()
    _mk(client, headers, "A", 1)
    client.post("/products/bulk-price", headers=headers, json={"mode": "amount", "value": -50, "dry_run": False})
    assert float(_prices(client, headers)["A"]) == 0.01
    assert client.post("/products/bulk-price", headers=headers, json={"mode": "amount", "value": 0}).status_code == 400


def test_other_tenant_untouched(client, register):
    h1, _ = register()
    h2, _ = register()
    _mk(client, h2, "OTHER", 10)
    client.post("/products/bulk-price", headers=h1, json={"mode": "percent", "value": 50, "dry_run": False})
    assert float(_prices(client, h2)["OTHER"]) == 10
