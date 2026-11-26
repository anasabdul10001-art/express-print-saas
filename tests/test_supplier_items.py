"""Names on supplier invoices are linked to products once and recognised afterwards."""

import json


def _product(client, headers, sku, title):
    return client.post("/products", headers=headers, json={"sku": sku, "title": title, "stock_quantity": 0}).json()["id"]


def _book(client, headers, links, number, supplier="Tech Handel"):
    ids = list(dict.fromkeys(link["product_id"] for link in links))
    data = {"supplier": supplier, "invoice_number": number, "invoice_date": "2026-11-20",
            "lines": [{"product_id": pid, "quantity": 10, "line_total": "20.00"} for pid in ids],
            "supplier_items": links}
    return client.post("/purchases", headers=headers, data={"data": json.dumps(data)},
                       files={"file": ("r.pdf", b"%PDF-1.4 x", "application/pdf")})


def _match(client, headers, name="", code="", supplier="Tech Handel"):
    r = client.post("/purchases/match", headers=headers,
                    json={"supplier": supplier, "items": [{"name": name, "code": code}]})
    assert r.status_code == 200, r.text
    return r.json()[0]


def _setup(register, client):
    headers, _ = register()
    black = _product(client, headers, "K-S", "USB C Kabel 1m schwarz")
    white = _product(client, headers, "K-W", "USB C Kabel 1m weiß")
    return headers, black, white


def test_a_similar_name_is_only_suggested_until_it_was_booked(register, client):
    headers, black, white = _setup(register, client)
    first = _match(client, headers, "Type-C cable 1m black USB C", "AB-1")
    assert first["match"] is None and first["suggestions"]
    assert _book(client, headers, [{"name": "Type-C cable 1m black", "code": "AB-1", "product_id": black,
                                    "units_per_pack": 5}], "R1").status_code == 201
    again = _match(client, headers, "anders geschrieben", " ab-1 ", supplier="  tech   HANDEL ")
    assert again["match"] == {"product_id": black, "units_per_pack": 5}
    # the same name without the number is recognised too
    assert _match(client, headers, "type-c CABLE 1m  black")["match"]["product_id"] == black
    # nearly the same is not the same, a new number is a new item, another supplier knows nothing
    assert _match(client, headers, "Type-C cable 2m black")["match"] is None
    assert _match(client, headers, "Type-C cable 1m black", "AB-2")["match"] is None
    assert _match(client, headers, "Type-C cable 1m black", "AB-1", supplier="Anderer")["match"] is None


def test_english_supplier_names_still_get_suggestions(register, client):
    headers, black, white = _setup(register, client)
    found = _match(client, headers, "Type-C cable 1m black nylon USB C braided fast charging")
    assert found["match"] is None and {black, white} == {s["product_id"] for s in found["suggestions"]}
    assert _match(client, headers, "Phone holder for car")["suggestions"] == []


def test_later_choice_wins_and_ambiguous_names_are_forgotten(register, client):
    headers, black, white = _setup(register, client)
    _book(client, headers, [{"name": "Cable A", "product_id": black}], "R1")
    _book(client, headers, [{"name": "Cable A", "product_id": white}], "R2")
    assert _match(client, headers, "Cable A")["match"]["product_id"] == white
    _book(client, headers, [{"name": "Cable A", "product_id": white}, {"name": "Cable A", "product_id": black}], "R3")
    assert _match(client, headers, "Cable A")["match"] is None


def test_nothing_is_learned_from_a_refused_booking_or_a_preview(register, client):
    headers, black, white = _setup(register, client)
    data = {"supplier": "Tech Handel", "invoice_date": "2026-11-20",
            "lines": [{"product_id": black, "quantity": 1, "line_total": "1.00"}],
            "supplier_items": [{"name": "Cable B", "product_id": black}]}
    assert client.post("/purchases/preview", headers=headers, json=data).status_code == 200
    stranger = {**data, "supplier_items": [{"name": "Cable B", "product_id": white}]}
    assert client.post("/purchases/preview", headers=headers, json=stranger).status_code == 422
    assert client.get("/purchases/supplier-items", headers=headers).json() == []


def test_links_are_private_and_can_be_deleted(register, client):
    headers, black, _ = _setup(register, client)
    other, _ = register()
    _book(client, headers, [{"name": "Cable C", "product_id": black}], "R1")
    link = client.get("/purchases/supplier-items", headers=headers).json()[0]
    assert link["product"] == "USB C Kabel 1m schwarz"
    assert client.get("/purchases/supplier-items", headers=other).json() == []
    assert _match(client, other, "Cable C") == {"match": None, "suggestions": []}
    assert client.delete(f"/purchases/supplier-items/{link['id']}", headers=other).status_code == 404
    assert client.delete(f"/purchases/supplier-items/{link['id']}", headers=headers).status_code == 204
    assert _match(client, headers, "Cable C")["match"] is None
    assert client.post("/purchases/match", json={"items": [{"name": "x"}]}).status_code == 401


def test_a_removed_product_is_not_chosen_again(register, client):
    headers, black, _ = _setup(register, client)
    _book(client, headers, [{"name": "Cable D", "product_id": black}], "R1")
    assert client.delete(f"/products/{black}", headers=headers).status_code == 204
    found = _match(client, headers, "Cable D")
    assert found["match"] is None and all(s["product_id"] != black for s in found["suggestions"])
