"""Push ShipSync selling prices to eBay (Trading API ReviseInventoryStatus)."""

import uuid

from app.database import SessionLocal
from app.models.ebay_account import EbayAccount
from app.services import ebay_listing_service

LIST_XML = """<?xml version="1.0" encoding="UTF-8"?>
<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack>
<ActiveList><ItemArray>
<Item><ItemID>111</ItemID><SKU>A</SKU><Title>A</Title><Quantity>1</Quantity>
<SellingStatus><CurrentPrice currencyID="EUR">10.00</CurrentPrice><QuantitySold>0</QuantitySold></SellingStatus></Item>
<Item><ItemID>222</ItemID><SKU>B</SKU><Title>B</Title><Quantity>1</Quantity>
<SellingStatus><CurrentPrice currencyID="EUR">20.00</CurrentPrice><QuantitySold>0</QuantitySold></SellingStatus></Item>
<Item><ItemID>333</ItemID><SKU>C</SKU><Title>C</Title><Quantity>1</Quantity>
<SellingStatus><CurrentPrice currencyID="EUR">5.00</CurrentPrice><QuantitySold>0</QuantitySold></SellingStatus></Item>
</ItemArray><PaginationResult><TotalNumberOfPages>1</TotalNumberOfPages></PaginationResult></ActiveList>
</GetMyeBaySellingResponse>"""


class _Resp:
    status_code = 200

    def __init__(self, text):
        self.text = text


def _setup(register, client, monkeypatch):
    headers, _ = register()
    tid = client.get("/auth/me", headers=headers).json()["tenant_id"]
    with SessionLocal() as db:
        db.add(EbayAccount(id=uuid.uuid4(), tenant_id=tid, environment="PRODUCTION", status="CONNECTED", ebay_user_id="shop"))
        db.commit()
    monkeypatch.setattr(ebay_listing_service.ebay_oauth_service, "get_valid_access_token", lambda a, d: "t")
    for sku, price in (("A", 9), ("B", 20), ("C", 4.5), ("NOEBAY", 3)):
        client.post("/products", headers=headers, json={"sku": sku, "title": sku, "selling_price": price})
    return headers


def test_preview_changes_nothing_and_apply_sends_only_differences(register, client, monkeypatch):
    headers = _setup(register, client, monkeypatch)
    sent = []

    def fake_post(url, headers=None, content=None, **k):
        if headers["X-EBAY-API-CALL-NAME"] == "GetMyeBaySelling":
            return _Resp(LIST_XML)
        sent.append(content)
        return _Resp('<R xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack></R>')

    monkeypatch.setattr(ebay_listing_service.httpx, "post", fake_post)

    prev = client.post("/ebay/push-prices", headers=headers, json={"dry_run": True}).json()
    assert prev["to_change"] == 2 and prev["no_ebay_match"] == 1 and not prev["applied"]
    assert sent == []

    done = client.post("/ebay/push-prices", headers=headers, json={"dry_run": False}).json()
    assert done["updated"] == 2 and done["failed"] == 0
    assert len(sent) == 1
    assert "<ItemID>111</ItemID><StartPrice>9.00</StartPrice>" in sent[0]
    assert "<ItemID>333</ItemID><StartPrice>4.50</StartPrice>" in sent[0]
    assert "222" not in sent[0]


def test_failure_is_attributed_per_item(register, client, monkeypatch):
    headers = _setup(register, client, monkeypatch)

    def fake_post(url, headers=None, content=None, **k):
        if headers["X-EBAY-API-CALL-NAME"] == "GetMyeBaySelling":
            return _Resp(LIST_XML)
        if "<ItemID>111</ItemID>" in content and "<ItemID>333</ItemID>" in content:
            return _Resp('<R xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>PartialFailure</Ack></R>')
        if "111" in content:
            return _Resp('<R xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Failure</Ack><Errors><LongMessage>Preis ungueltig</LongMessage></Errors></R>')
        return _Resp('<R xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack></R>')

    monkeypatch.setattr(ebay_listing_service.httpx, "post", fake_post)
    out = client.post("/ebay/push-prices", headers=headers, json={"dry_run": False}).json()
    assert out["updated"] == 1 and out["failed"] == 1
    bad = [i for i in out["items"] if i["result"] != "ok"][0]
    assert bad["item_id"] == "111" and "Preis" in bad["result"]


def test_selected_products_only_and_no_account(register, client, monkeypatch):
    headers, _ = register()
    assert client.post("/ebay/push-prices", headers=headers, json={}).status_code == 400
