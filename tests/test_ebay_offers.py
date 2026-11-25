"""Offers to interested buyers. eBay is always faked: nothing is ever really sent."""
import httpx
import pytest

from app.database import SessionLocal
from app.models.ebay_account import EbayAccount
from app.services import ebay_oauth_service, ebay_offers_service

LISTING = "920123322554"


class Ebay:
    def __init__(self):
        self.sent = []
        self.get_response = httpx.Response(200, json={"eligibleItems": [{"listingId": LISTING}]})
        self.post_response = httpx.Response(200, json={"offers": [{"offerId": "1"}, {"offerId": "2"}]})

    def get(self, url, **kwargs):
        assert url.endswith("/sell/negotiation/v1/find_eligible_items")
        assert kwargs["headers"]["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_DE"
        return self.get_response

    def post(self, url, **kwargs):
        assert url.endswith("/sell/negotiation/v1/send_offer_to_interested_buyers")
        self.sent.append(kwargs["json"])
        if isinstance(self.post_response, Exception):
            raise self.post_response
        return self.post_response


@pytest.fixture
def ebay(monkeypatch):
    fake = Ebay()
    monkeypatch.setattr(ebay_oauth_service, "get_valid_access_token", lambda account, db: "token")
    monkeypatch.setattr(ebay_offers_service.httpx, "get", fake.get)
    monkeypatch.setattr(ebay_offers_service.httpx, "post", fake.post)
    return fake


def _seller(client, register, price=10, cost=1):
    headers, _ = register()
    tenant_id = client.get("/auth/me", headers=headers).json()["tenant_id"]
    with SessionLocal() as db:
        account = EbayAccount(tenant_id=tenant_id, ebay_user_id="shop", status="CONNECTED", environment="SANDBOX")
        db.add(account)
        db.commit()
        account_id = str(account.id)
    body = {"sku": LISTING, "title": "Adapter", "selling_price": price, "stock_quantity": 5}
    if cost is not None:
        body["purchase_price"] = cost
    client.post("/products", headers=headers, json=body)
    return headers, account_id


def _send(client, headers, account_id, **extra):
    body = {"account_id": account_id, "listing_id": LISTING, "discount_percent": 10, "confirm": True, **extra}
    return client.post("/ebay/offers/send", headers=headers, json=body)


def test_eligible_listings_with_price_check(client, register, ebay):
    headers, account_id = _seller(client, register)
    data = client.get("/ebay/offers/eligible", headers=headers).json()
    item = data["items"][0]
    assert item["listing_id"] == LISTING and item["account_id"] == account_id and item["title"] == "Adapter"
    assert item["price"] == "10.00" and float(item["break_even"]) > 2
    assert ebay.sent == []


def test_nothing_eligible_and_ebay_error(client, register, ebay):
    headers, _ = _seller(client, register)
    ebay.get_response = httpx.Response(204)
    assert client.get("/ebay/offers/eligible", headers=headers).json()["items"] == []
    ebay.get_response = httpx.Response(403, json={"errors": [{"message": "Insufficient permissions"}]})
    r = client.get("/ebay/offers/eligible", headers=headers)
    assert r.status_code == 502 and "Insufficient permissions" in r.json()["detail"]


def test_send_builds_exactly_one_offer(client, register, ebay):
    headers, account_id = _seller(client, register)
    r = _send(client, headers, account_id, message="  Danke!  ")
    assert r.status_code == 200 and r.json() == {"sent_to": 2}
    assert ebay.sent == [{"allowCounterOffer": False, "message": "Danke!", "offeredItems": [
        {"listingId": LISTING, "discountPercentage": "10", "quantity": 1}]}]


@pytest.mark.parametrize("extra,status", [
    ({"confirm": False}, 400), ({"discount_percent": 4}, 422), ({"discount_percent": 51}, 422),
    ({"listing_id": "abc"}, 422), ({"message": "x" * 2001}, 422),
])
def test_nothing_is_sent_without_valid_confirmed_input(client, register, ebay, extra, status):
    headers, account_id = _seller(client, register)
    assert _send(client, headers, account_id, **extra).status_code == status
    assert ebay.sent == []


def test_loss_needs_a_second_confirmation(client, register, ebay):
    headers, account_id = _seller(client, register, price=3, cost=1.5)
    r = _send(client, headers, account_id, discount_percent=20)
    assert r.status_code == 409 and "Gewinnschwelle" in r.json()["detail"] and ebay.sent == []
    assert _send(client, headers, account_id, discount_percent=20, accept_loss=True).status_code == 200
    assert len(ebay.sent) == 1


def test_other_sellers_cannot_use_the_account(client, register, ebay):
    _, account_id = _seller(client, register)
    other, _ = register()
    assert _send(client, other, account_id).status_code == 404
    assert ebay.sent == []


def test_timeout_is_reported_and_not_retried(client, register, ebay):
    headers, account_id = _seller(client, register)
    ebay.post_response = httpx.ReadTimeout("slow")
    r = _send(client, headers, account_id)
    assert r.status_code == 502 and "prüfen" in r.json()["detail"]
    assert len(ebay.sent) == 1
