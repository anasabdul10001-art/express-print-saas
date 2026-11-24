"""Pricing calculator: unit cost + eBay fees + VAT mode + target -> price."""
from decimal import Decimal

from app.services import pricing

COMMON = dict(label_cost=Decimal("1.10"), fee_percent=Decimal("11"), fee_fixed=Decimal("0.35"), vat_rate=Decimal("19"))


def test_break_even_kleinunternehmer():
    # goods 2.00 + label 1.10 = 3.10; P*(1-0.11) = 3.10 + 0.35 -> P = 3.45/0.89 = 3.8764 -> 3.88
    p = pricing.solve_price(goods_cost=Decimal("2"), vat_mode="kleinunternehmer", **COMMON)
    assert p == Decimal("3.88")
    b = pricing.breakdown(p, goods_cost=Decimal("2"), vat_mode="kleinunternehmer", **COMMON)
    assert Decimal(b["profit"]) >= 0 and Decimal(b["sales_vat"]) == 0


def test_break_even_regular_vat():
    # f=1.19: P*(1/1.19 - 0.11) = 3.10/1.19 + 0.35 -> P = (2.6050+0.35)/(0.84034-0.11) = 4.0463 -> 4.05
    p = pricing.solve_price(goods_cost=Decimal("2"), vat_mode="regular", **COMMON)
    assert p == Decimal("4.05")
    b = pricing.breakdown(p, goods_cost=Decimal("2"), vat_mode="regular", **COMMON)
    assert Decimal(b["profit"]) >= 0
    # price 4.05 gross: VAT 0.65, net 3.40
    assert b["sales_vat"] == "0.65" and b["net_revenue"] == "3.40"


def _raw_profit(price, f):
    return price / f - Decimal("3.10") / f - price * Decimal("0.11") - Decimal("0.35")


def test_target_profit_is_reached_and_minimal():
    for mode, f in (("kleinunternehmer", Decimal("1")), ("regular", Decimal("1.19"))):
        p = pricing.solve_price(goods_cost=Decimal("2"), vat_mode=mode, target_profit=Decimal("1"), **COMMON)
        assert _raw_profit(p, f) >= Decimal("1")
        assert _raw_profit(p - Decimal("0.01"), f) < Decimal("1")


def test_target_margin_of_net_revenue():
    p = pricing.solve_price(goods_cost=Decimal("2"), vat_mode="regular", target_margin_percent=Decimal("20"), **COMMON)
    b = pricing.breakdown(p, goods_cost=Decimal("2"), vat_mode="regular", **COMMON)
    assert Decimal(b["margin_percent"]) >= Decimal("19.9")


def test_impossible_margin():
    try:
        pricing.solve_price(goods_cost=Decimal("2"), vat_mode="kleinunternehmer", target_margin_percent=Decimal("90"), **COMMON)
    except pricing.PricingError:
        return
    raise AssertionError("expected PricingError")


def test_round_up_endings():
    r = pricing.round_up_to_ending
    assert r(Decimal("3.88"), "x.99") == Decimal("3.99")
    assert r(Decimal("3.99"), "x.99") == Decimal("3.99")
    assert r(Decimal("4.00"), "x.99") == Decimal("4.99")
    assert r(Decimal("3.60"), "x.49") == Decimal("3.99")
    assert r(Decimal("3.30"), "x.49") == Decimal("3.49")
    assert r(Decimal("3.91"), "x.90") == Decimal("4.90")
    assert r(Decimal("0.20"), "x.99") == Decimal("0.99")
    assert r(Decimal("3.88"), "none") == Decimal("3.88")


def test_endpoint_uses_tenant_vat_mode(client, register):
    headers, _ = register()
    body = {"purchase_price": 1, "supplier_shipping_cost": 0.5, "packaging_cost": 0.5}
    r = client.post("/pricing/calculate", headers=headers, json=body).json()
    assert r["vat_mode"] == "kleinunternehmer" and r["goods_cost"] == "2.00"
    assert r["break_even_price"] == "3.88"
    client.put("/accounting/vat-settings", headers=headers, json={"vat_mode": "regular"})
    r = client.post("/pricing/calculate", headers=headers, json={**body, "check_price": 4.05}).json()
    assert r["vat_mode"] == "regular" and r["break_even_price"] == "4.05"
    assert r["at_check_price"]["sales_vat"] == "0.65"
    # explicit override beats the saved setting
    r = client.post("/pricing/calculate", headers=headers, json={**body, "vat_mode": "kleinunternehmer"}).json()
    assert r["break_even_price"] == "3.88"


def test_endpoint_validation(client, register):
    headers, _ = register()
    both = {"purchase_price": 1, "target_profit": 1, "target_margin_percent": 10}
    assert client.post("/pricing/calculate", headers=headers, json=both).status_code == 400
    assert client.post("/pricing/calculate", headers=headers, json={"purchase_price": -1}).status_code == 422
    assert client.post("/pricing/calculate", headers=headers, json={"purchase_price": 1, "target_margin_percent": 95}).status_code == 400  # fees + margin leave no price
    assert client.post("/pricing/calculate", headers=headers, json={"purchase_price": 1, "target_margin_percent": 100}).status_code == 422
    assert client.post("/pricing/calculate", json={"purchase_price": 1}).status_code in (401, 403)


def test_products_list_and_isolation(client, register):
    h1, _ = register()
    h2, _ = register()
    client.post("/products", headers=h1, json={"sku": "P1", "title": "P1", "purchase_price": 1, "packaging_cost": 1, "selling_price": 5, "stock_quantity": 1})
    client.post("/products", headers=h1, json={"sku": "P2", "title": "P2 ohne Kosten", "stock_quantity": 1})
    r = client.post("/pricing/products", headers=h1, json={"target_margin_percent": 10}).json()
    assert [p["sku"] for p in r["products"]] == ["P1"]  # product without a purchase price is skipped
    row = r["products"][0]
    assert row["goods_cost"] == "2.00" and row["at_current"]["price"] == "5.00"
    assert client.post("/pricing/products", headers=h2, json={}).json()["products"] == []
