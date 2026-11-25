"""Invoices a seller issues to their buyers."""
import io
import xml.etree.ElementTree as ET
from decimal import Decimal

import pypdf

from app.database import SessionLocal
from app.models.order import Order
from app.services import einvoice_service
from app.services import sales_invoice_service as service

PROFILE = {"legal_name": "Muster Händler", "address": "Hauptstr. 1\n04720 Döbeln", "tax_number": "123/456/789"}


def _order(client, headers, sku="I1", price=11.9, quantity=1, **fields):
    pid = client.post("/products", headers=headers, json={
        "sku": sku, "title": "Kabel für Zubehör", "purchase_price": 1, "selling_price": price,
        "stock_quantity": 99}).json()["id"]
    order_id = client.post("/orders", headers=headers, json={
        "items": [{"product_id": pid, "quantity": quantity}]}).json()["id"]
    if fields:
        with SessionLocal() as db:
            order = db.get(Order, order_id)
            for name, value in fields.items():
                setattr(order, name, value)
            db.commit()
    return order_id


def _seller(client, register, **profile):
    headers, _ = register()
    assert client.put("/sales-invoices/profile", headers=headers, json={**PROFILE, **profile}).status_code == 200
    return headers


def _issue(client, headers, order_id):
    return client.post("/sales-invoices", headers=headers, json={"order_id": order_id, "delivery_date": "2026-11-24"})


def _pdf_text(client, headers, invoice_id):
    response = client.get(f"/sales-invoices/{invoice_id}/pdf", headers=headers)
    assert response.status_code == 200 and response.headers["content-type"] == "application/pdf"
    return "\n".join(page.extract_text() for page in pypdf.PdfReader(io.BytesIO(response.content)).pages)


def test_seller_details_are_required(client, register):
    headers, _ = register()
    order_id = _order(client, headers)
    r = _issue(client, headers, order_id)
    assert r.status_code == 400 and "Rechnungsdaten" in r.json()["detail"]
    client.put("/sales-invoices/profile", headers=headers, json={**PROFILE, "tax_number": "  "})
    assert _issue(client, headers, order_id).status_code == 400


def test_kleinunternehmer_invoice_pdf_with_shipping_difference(client, register):
    headers = _seller(client, register)
    order_id = _order(client, headers, price=3, quantity=2, ebay_buyer_total=Decimal("7.10"),
                      recipient_name="Erika Müller", recipient_street1="Weg 2", recipient_zip="01067",
                      recipient_city="Dresden", recipient_country_code="DE", external_order_ref="12-345")
    r = _issue(client, headers, order_id)
    assert r.status_code == 201, r.text
    assert r.json()["number"].startswith("RE-") and r.json()["number"].endswith("-0001")
    assert r.json()["gross_amount"] == "7.10"
    text = _pdf_text(client, headers, r.json()["id"])
    for expected in ("Muster Händler", "Erika Müller", "01067 Dresden", "Steuernummer: 123/456/789",
                     "12-345", "Lieferdatum: 24.11.2026", "Versandkosten", "1,10 €", "7,10 €", "§ 19 UStG"):
        assert expected in text, expected
    assert "Nettobetrag" not in text


def test_regular_invoice_shows_vat(client, register):
    headers = _seller(client, register)
    client.put("/accounting/vat-settings", headers=headers, json={"vat_mode": "regular"})
    r = _issue(client, headers, _order(client, headers))
    text = _pdf_text(client, headers, r.json()["id"])
    for expected in ("Nettobetrag", "10,00 €", "enthaltene USt. 19 %", "1,90 €", "11,90 €"):
        assert expected in text, expected
    assert "§ 19" not in text


def test_discount_line_is_negative(client, register):
    headers = _seller(client, register)
    r = _issue(client, headers, _order(client, headers, price=10, ebay_buyer_total=Decimal("9.00")))
    assert "Rabatt" in _pdf_text(client, headers, r.json()["id"])


def test_one_invoice_per_order_then_storno_and_new_number(client, register):
    headers = _seller(client, register, number_prefix="LC")
    order_id = _order(client, headers)
    first = _issue(client, headers, order_id).json()
    assert _issue(client, headers, order_id).status_code == 409

    storno = client.post(f"/sales-invoices/{first['id']}/cancel", headers=headers)
    assert storno.status_code == 201
    storno = storno.json()
    assert storno["kind"] == "storno" and storno["gross_amount"] == "-11.90" and storno["number"].endswith("-0002")
    text = _pdf_text(client, headers, storno["id"])
    assert "Stornorechnung" in text and first["number"] in text and "-11,90 €" in text

    assert client.post(f"/sales-invoices/{first['id']}/cancel", headers=headers).status_code == 409
    assert client.post(f"/sales-invoices/{storno['id']}/cancel", headers=headers).status_code == 400
    second = _issue(client, headers, order_id).json()
    assert second["number"].startswith("LC-") and second["number"].endswith("-0003")
    listed = {i["number"]: i["cancelled"] for i in client.get("/sales-invoices", headers=headers).json()}
    assert listed == {first["number"]: True, storno["number"]: False, second["number"]: False}


def test_invoice_does_not_change_afterwards(client, register):
    headers = _seller(client, register)
    order_id = _order(client, headers)
    invoice = _issue(client, headers, order_id).json()
    before = _pdf_text(client, headers, invoice["id"])
    client.put("/sales-invoices/profile", headers=headers, json={**PROFILE, "legal_name": "Neuer Name"})
    with SessionLocal() as db:
        db.get(Order, order_id).ebay_buyer_total = Decimal("99")
        db.commit()
    assert _pdf_text(client, headers, invoice["id"]) == before


def test_rejected_orders_do_not_use_up_a_number(client, register):
    headers = _seller(client, register)
    big = _order(client, headers, sku="BIG", price=300)
    abroad = _order(client, headers, sku="AT", recipient_country_code="AT")
    canceled = _order(client, headers, sku="C", status="canceled")
    for order_id, word in ((big, "250"), (abroad, "innerhalb Deutschlands"), (canceled, "stornierte")):
        r = _issue(client, headers, order_id)
        assert r.status_code == 400 and word in r.json()["detail"], r.text
    assert _issue(client, headers, _order(client, headers, sku="OK")).json()["number"].endswith("-0001")


def test_other_sellers_cannot_see_or_use_invoices(client, register):
    owner = _seller(client, register)
    order_id = _order(client, owner)
    invoice = _issue(client, owner, order_id).json()
    other = _seller(client, register)
    assert client.get("/sales-invoices", headers=other).json() == []
    assert client.get(f"/sales-invoices/{invoice['id']}/pdf", headers=other).status_code == 404
    assert client.post(f"/sales-invoices/{invoice['id']}/cancel", headers=other).status_code == 404
    assert _issue(client, other, order_id).status_code == 404


def test_delivery_date_is_required(client, register):
    headers = _seller(client, register)
    r = client.post("/sales-invoices", headers=headers, json={"order_id": _order(client, headers)})
    assert r.status_code == 422


def test_incomplete_buyer_address_does_not_count(client, register):
    headers = _seller(client, register)
    order_id = _order(client, headers, price=300, recipient_name="A B", recipient_street1="Weg 1",
                      recipient_city="Dresden", recipient_country_code="DE")
    r = _issue(client, headers, order_id)
    assert r.status_code == 400 and "Anschrift" in r.json()["detail"]


def test_vat_split_always_adds_up():
    for cents in range(1, 2000):
        gross = Decimal(cents) / 100
        net, vat = service.split_vat(gross)
        assert net + vat == gross


def test_pdf_text_keeps_german_and_refuses_unprintable_scripts():
    assert service.pdf_text("Müßig € §") == "Müßig € §"
    assert service.pdf_text("Łukasz Ćwik") == "Lukasz Cwik"
    try:
        service.pdf_text("محمد")
    except service.InvoiceError:
        pass
    else:
        raise AssertionError("unprintable text must be refused")


BUYER = dict(recipient_name="Erika Müller", recipient_street1="Weg 2", recipient_zip="01067",
             recipient_city="Dresden", recipient_country_code="DE")
CII = {"ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"}


def _xml(client, headers, invoice_id):
    response = client.get(f"/sales-invoices/{invoice_id}/xml", headers=headers)
    assert response.status_code == 200, response.text
    return ET.fromstring(response.content)


def _values(root, path):
    return [e.text for e in root.findall(path, CII)]


def test_einvoice_totals_add_up_for_regular_vat(client, register):
    headers = _seller(client, register, vat_id="DE123456789")
    client.put("/accounting/vat-settings", headers=headers, json={"vat_mode": "regular"})
    # Three lines whose rounded net amounts don't add up by themselves.
    order_id = _order(client, headers, price=0.99, quantity=3, ebay_buyer_total=Decimal("4.07"), **BUYER)
    invoice = _issue(client, headers, order_id).json()
    assert invoice["has_xml"] is True
    root = _xml(client, headers, invoice["id"])
    total = ".//ram:SpecifiedTradeSettlementHeaderMonetarySummation/ram:"
    line_nets = _values(root, ".//ram:SpecifiedTradeSettlementLineMonetarySummation/ram:LineTotalAmount")
    net = Decimal(_values(root, total + "TaxBasisTotalAmount")[0])
    vat = Decimal(_values(root, total + "TaxTotalAmount")[0])
    assert sum(map(Decimal, line_nets)) == net and net + vat == Decimal("4.07")
    assert _values(root, ".//ram:SellerTradeParty/ram:PostalTradeAddress/ram:PostcodeCode") == ["04720"]


def test_einvoice_storno_references_original_and_has_no_negative_price(client, register):
    headers = _seller(client, register)
    invoice = _issue(client, headers, _order(client, headers, **BUYER)).json()
    storno = client.post(f"/sales-invoices/{invoice['id']}/cancel", headers=headers).json()
    root = _xml(client, headers, storno["id"])
    assert _values(root, ".//ram:InvoiceReferencedDocument/ram:IssuerAssignedID") == [invoice["number"]]
    assert all(Decimal(v) >= 0 for v in _values(root, ".//ram:NetPriceProductTradePrice/ram:ChargeAmount"))
    assert _values(root, ".//ram:BilledQuantity") == ["-1"]


def test_no_einvoice_without_complete_addresses(client, register):
    headers = _seller(client, register)
    invoice = _issue(client, headers, _order(client, headers)).json()
    assert invoice["has_xml"] is False
    assert client.get(f"/sales-invoices/{invoice['id']}/xml", headers=headers).status_code == 404


def test_seller_address_parsing():
    parsed = einvoice_service.parse_address("Hauptstr. 1\nHinterhaus\nD-04720 Döbeln\nDeutschland")
    assert parsed == {"street1": "Hauptstr. 1", "street2": "Hinterhaus", "zip": "04720", "city": "Döbeln"}
    assert einvoice_service.parse_address("Hauptstr. 1, 04720 Döbeln") is None


def test_einvoice_seller_without_vat_id_is_identified_by_tax_number(client, register):
    # EN 16931 rule BR-CO-26, found with the Mustang validator.
    headers = _seller(client, register)
    invoice = _issue(client, headers, _order(client, headers, **BUYER)).json()
    root = _xml(client, headers, invoice["id"])
    assert _values(root, ".//ram:SellerTradeParty/ram:ID") == ["123/456/789"]
