"""Auto-import of active eBay listings as products (Trading API GetMyeBaySelling)."""

import uuid

from app.database import SessionLocal
from app.models.ebay_account import EbayAccount
from app.models.product import Product
from app.services import ebay_listing_service

XML = """<?xml version="1.0" encoding="UTF-8"?>
<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <ActiveList>
    <ItemArray>
      <Item><ItemID>111</ItemID><SKU>KAB-1</SKU><Title>USB-C Kabel</Title><Quantity>10</Quantity>
        <SellingStatus><CurrentPrice currencyID="EUR">3.59</CurrentPrice><QuantitySold>4</QuantitySold></SellingStatus>
        <PictureDetails><GalleryURL>https://img/1.jpg</GalleryURL></PictureDetails></Item>
      <Item><ItemID>222</ItemID><Title>Adapter ohne SKU</Title><Quantity>2</Quantity>
        <SellingStatus><CurrentPrice currencyID="EUR">5.00</CurrentPrice><QuantitySold>0</QuantitySold></SellingStatus></Item>
    </ItemArray>
    <PaginationResult><TotalNumberOfPages>1</TotalNumberOfPages></PaginationResult>
  </ActiveList>
</GetMyeBaySellingResponse>"""


class _Resp:
    status_code = 200

    def __init__(self, text):
        self.text = text


def _setup(register, client, monkeypatch, text=XML):
    headers, _ = register()
    tenant_id = client.get("/auth/me", headers=headers).json()["tenant_id"]
    with SessionLocal() as db:
        db.add(EbayAccount(id=uuid.uuid4(), tenant_id=tenant_id, environment="PRODUCTION", status="CONNECTED"))
        db.commit()
    monkeypatch.setattr(ebay_listing_service.ebay_oauth_service, "get_valid_access_token", lambda a, d: "t")
    monkeypatch.setattr(ebay_listing_service.httpx, "post", lambda *a, **k: _Resp(text))
    return headers, tenant_id


def test_import_creates_products_from_listings(register, client, monkeypatch):
    headers, tenant_id = _setup(register, client, monkeypatch)
    out = client.post("/ebay/import-products", headers=headers).json()
    assert out["listings_found"] == 2 and out["products_created"] == 2
    with SessionLocal() as db:
        kabel = db.query(Product).filter_by(tenant_id=tenant_id, sku="KAB-1").one()
        assert kabel.title == "USB-C Kabel" and float(kabel.selling_price) == 3.59
        assert kabel.stock_quantity == 6 and kabel.image_url == "https://img/1.jpg"
        assert db.query(Product).filter_by(tenant_id=tenant_id, sku="222").one()  # item number fallback


def test_import_is_repeatable_and_keeps_existing_products(register, client, monkeypatch):
    headers, tenant_id = _setup(register, client, monkeypatch)
    with SessionLocal() as db:
        db.add(Product(tenant_id=tenant_id, sku="KAB-1", title="Mein Titel", selling_price=9, purchase_price=2, stock_quantity=1))
        db.commit()
    out = client.post("/ebay/import-products", headers=headers).json()
    assert out["products_created"] == 1 and out["already_known"] == 1
    again = client.post("/ebay/import-products", headers=headers).json()
    assert again["products_created"] == 0
    with SessionLocal() as db:
        kabel = db.query(Product).filter_by(tenant_id=tenant_id, sku="KAB-1").one()
        assert kabel.title == "Mein Titel" and float(kabel.purchase_price) == 2


def test_ebay_error_returns_502(register, client, monkeypatch):
    failure = '<R xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Failure</Ack><Errors><LongMessage>Token invalid</LongMessage></Errors></R>'
    headers, _ = _setup(register, client, monkeypatch, failure)
    r = client.post("/ebay/import-products", headers=headers)
    assert r.status_code == 502 and "Token invalid" in r.json()["detail"]
