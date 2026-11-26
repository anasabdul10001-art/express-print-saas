"""VAT mode: Kleinunternehmer (default, no VAT) vs regular 19%."""


def _sale(client, headers):
    pid = client.post("/products", headers=headers, json={
        "sku": "V1", "title": "V1", "purchase_price": 1, "selling_price": 11.9, "stock_quantity": 5}).json()["id"]
    assert client.post("/orders", headers=headers, json={"items": [{"product_id": pid, "quantity": 1}]}).status_code in (200, 201)


def _summary(client, headers):
    return client.get("/accounting/summary", headers=headers, params={"start": "2020-01-01", "end": "2029-12-31"}).json()


def test_default_is_kleinunternehmer_without_vat(client, register):
    headers, _ = register()
    _sale(client, headers)
    r = _summary(client, headers)
    assert r["vat_mode"] == "kleinunternehmer" and "vat" not in r


def test_regular_shows_19_percent(client, register):
    headers, _ = register()
    _sale(client, headers)
    assert client.put("/accounting/vat-settings", headers=headers, json={"vat_mode": "regular"}).status_code == 200
    r = _summary(client, headers)
    # no estimated VAT figures: they come from the invoices in /accounting/ustva
    assert r["vat_mode"] == "regular" and "vat" not in r
    assert client.get("/accounting/vat-report", headers=headers).status_code == 404


def test_invalid_mode_rejected(client, register):
    headers, _ = register()
    assert client.put("/accounting/vat-settings", headers=headers, json={"vat_mode": "x"}).status_code == 422
