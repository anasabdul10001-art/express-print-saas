"""Creating listings. eBay is always faked: nothing is ever really listed."""
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.database import SessionLocal
from app.models.ebay_account import EbayAccount
from app.services import ebay_create_listing_service as service
from app.services import ebay_oauth_service

NS = {"e": "urn:ebay:apis:eBLBaseComponents"}


def _reply(ack="Success", item_id="110000000001", extra=""):
    return httpx.Response(200, text=f'<R xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>{ack}</Ack>'
                                    f"<ItemID>{item_id}</ItemID>{extra}</R>")


class Ebay:
    def __init__(self):
        self.calls = []
        self.response = _reply()

    def post(self, url, headers, content, **kwargs):
        self.calls.append((headers["X-EBAY-API-CALL-NAME"], content.decode()))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.fixture
def ebay(monkeypatch):
    fake = Ebay()
    monkeypatch.setattr(ebay_oauth_service, "get_valid_access_token", lambda account, db: "token")
    monkeypatch.setattr(service.httpx, "post", fake.post)
    return fake


def _seller(client, register):
    headers, _ = register()
    tenant_id = client.get("/auth/me", headers=headers).json()["tenant_id"]
    with SessionLocal() as db:
        account = EbayAccount(tenant_id=tenant_id, ebay_user_id="shop", status="CONNECTED", environment="SANDBOX")
        db.add(account)
        db.commit()
        return headers, str(account.id)


def _body(account_id, **extra):
    return {"account_id": account_id, "draft_id": str(uuid.uuid4()), "title": "USB Adapter <neu> & gut",
            "description": "Zeile eins\n<script>x</script>", "category_id": "123", "price": "4.99", "quantity": 3,
            "location": "Döbeln", "postal_code": "04720", "picture_urls": ["https://example.com/a.jpg"],
            "shipping_policy_id": "1", "return_policy_id": "2", "payment_policy_id": "3",
            "specifics": {"Marke": "Markenlos", "": "x"}, **extra}


def test_verify_never_lists(client, register, ebay):
    headers, account_id = _seller(client, register)
    ebay.response = _reply(extra="<Fees><Fee><Name>InsertionFee</Name><Fee>0.35</Fee></Fee></Fees>")
    r = client.post("/ebay/new-listing/verify", headers=headers, json=_body(account_id))
    assert r.status_code == 200 and r.json()["ok"] and "item_id" not in r.json()
    assert r.json()["fees"] == [{"name": "InsertionFee", "amount": "0.35"}]
    assert [c[0] for c in ebay.calls] == ["VerifyAddFixedPriceItem"]
    assert client.get("/products", headers=headers).json() == []


def test_xml_escapes_everything_and_schedules_in_utc(client, register, ebay):
    headers, account_id = _seller(client, register)
    when = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0)
    client.post("/ebay/new-listing/verify", headers=headers,
                json=_body(account_id, schedule_at=when.isoformat()))
    item = ET.fromstring(ebay.calls[0][1].encode()).find("e:Item", NS)
    assert item.findtext("e:Title", namespaces=NS) == "USB Adapter <neu> & gut"
    assert item.findtext("e:Description", namespaces=NS) == "Zeile eins<br>&lt;script&gt;x&lt;/script&gt;"
    assert item.findtext("e:ScheduleTime", namespaces=NS) == when.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    assert len(item.findtext("e:UUID", namespaces=NS)) == 32
    assert [n.findtext("e:Name", namespaces=NS) for n in item.findall("e:ItemSpecifics/e:NameValueList", NS)] == ["Marke"]


def test_publish_needs_confirmation_then_creates_the_product(client, register, ebay):
    headers, account_id = _seller(client, register)
    assert client.post("/ebay/new-listing/publish", headers=headers, json=_body(account_id)).status_code == 400
    assert ebay.calls == []
    r = client.post("/ebay/new-listing/publish", headers=headers, json=_body(account_id, confirm=True))
    assert r.status_code == 200 and r.json()["item_id"] == "110000000001"
    assert [c[0] for c in ebay.calls] == ["AddFixedPriceItem"]
    assert [p["sku"] for p in client.get("/products", headers=headers).json()] == ["110000000001"]


def test_ebay_errors_are_shown_and_nothing_is_saved(client, register, ebay):
    headers, account_id = _seller(client, register)
    ebay.response = _reply("Failure", "", "<Errors><LongMessage>Marke fehlt</LongMessage><SeverityCode>Error</SeverityCode></Errors>")
    r = client.post("/ebay/new-listing/publish", headers=headers, json=_body(account_id, confirm=True))
    assert r.json()["ok"] is False and r.json()["messages"] == [{"severity": "error", "text": "Marke fehlt"}]
    assert client.get("/products", headers=headers).json() == []


def test_timeout_on_publish_is_not_retried(client, register, ebay):
    headers, account_id = _seller(client, register)
    ebay.response = httpx.ReadTimeout("slow")
    r = client.post("/ebay/new-listing/publish", headers=headers, json=_body(account_id, confirm=True))
    assert r.status_code == 502 and "prüfen" in r.json()["detail"] and len(ebay.calls) == 1


@pytest.mark.parametrize("extra", [
    {"title": "x" * 81}, {"picture_urls": ["http://example.com/a.jpg"]}, {"price": "0.50"},
    {"category_id": "abc"}, {"postal_code": "123"}, {"schedule_at": "2030-01-01T10:00:00"},
])
def test_invalid_input_never_reaches_ebay(client, register, ebay, extra):
    headers, account_id = _seller(client, register)
    r = client.post("/ebay/new-listing/publish", headers=headers, json=_body(account_id, confirm=True, **extra))
    assert r.status_code == 422 and ebay.calls == []


def test_other_sellers_cannot_use_the_account(client, register, ebay):
    _, account_id = _seller(client, register)
    other, _ = register()
    r = client.post("/ebay/new-listing/publish", headers=other, json=_body(account_id, confirm=True))
    assert r.status_code == 404 and ebay.calls == []
