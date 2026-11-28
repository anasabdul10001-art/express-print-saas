"""Buying a letter stamp spends the seller's money: never twice, and never silently."""

import uuid
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config import settings
from app.database import engine
from app.models.order import Order
from app.services import ebay_deletion_service, internetmarke_products, internetmarke_service, retention_service
from tests.test_print_jobs import _make_agent, _make_order, _make_product, _set_recipient_address

PDF = b"%PDF-1.4 stamp"
CONNECT = {"username": "kasse@example.com", "password": "geheim-123", "sender_name": "LamarConnect",
           "sender_street": "Teststr. 1", "sender_zip": "04720", "sender_city": "Döbeln", "accept_terms": True}
# An invented product: real codes and prices come from Deutsche Post's list.
PRODUCT = internetmarke_products.Product(999, "Testbrief", 100, date(2000, 1, 1), date(2999, 1, 1))
BUY = {"product_code": 999, "price_cents": 100, "accept_terms": True}


class Post:
    """Stands in for Deutsche Post."""

    def __init__(self, monkeypatch):
        self.checkouts = []
        self.checkout_error = None
        self.download_error = None
        self.wallet = 10_000
        monkeypatch.setattr(settings, "internetmarke_client_id", "id")
        monkeypatch.setattr(settings, "internetmarke_client_secret", "secret")
        monkeypatch.setattr(internetmarke_products, "PRODUCTS", (PRODUCT,))
        monkeypatch.setattr(internetmarke_service, "login",
                            lambda u, p: internetmarke_service.Session("token", self.wallet))
        monkeypatch.setattr(internetmarke_service, "create_cart", lambda session: "1234567")
        monkeypatch.setattr(internetmarke_service, "checkout", self.checkout)
        monkeypatch.setattr(internetmarke_service, "download_label", self.download)

    def checkout(self, *args):
        self.checkouts.append(args)
        if self.checkout_error:
            raise self.checkout_error
        return internetmarke_service.Checkout("https://internetmarke.deutschepost.de/x.pdf", "A001", None, 9_900)

    def download(self, link):
        if self.download_error:
            raise self.download_error
        return PDF


def _ready(client, register, agent=True):
    headers, _ = register()
    assert client.post("/internetmarke/connect", headers=headers, json=CONNECT).status_code == 200
    assert client.put("/internetmarke/page-format", headers=headers, json={"page_format_id": 1}).status_code == 200
    if agent:
        _make_agent(client, headers)
    order = _make_order(client, headers, _make_product(client, headers))
    _set_recipient_address(order["id"])
    return headers, order["id"]


def _rows(sql):
    with create_engine(settings.database_url).connect() as conn:
        return conn.execute(text(sql)).fetchall()


def _buy(client, headers, order_id, **changes):
    return client.post(f"/internetmarke/orders/{order_id}/buy", headers=headers, json={**BUY, **changes})


def test_buying_pays_once_stores_the_label_and_prints_it(client, register, monkeypatch):
    post = Post(monkeypatch)
    headers, order_id = _ready(client, register)
    response = _buy(client, headers, order_id)
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "BOUGHT" and response.json()["print_job_id"]
    assert _buy(client, headers, order_id).status_code == 409
    assert len(post.checkouts) == 1
    label = client.get(f"/internetmarke/purchases/{response.json()['id']}/label", headers=headers)
    assert label.content == PDF


@pytest.mark.parametrize("changes,expected", [
    ({"accept_terms": False}, 400), ({"price_cents": 99}, 409), ({"product_code": 1}, 400)])
def test_nothing_is_bought_without_terms_the_shown_price_or_a_known_product(client, register, monkeypatch,
                                                                            changes, expected):
    post = Post(monkeypatch)
    headers, order_id = _ready(client, register)
    assert _buy(client, headers, order_id, **changes).status_code == expected
    assert post.checkouts == []


def test_too_little_money_buys_nothing(client, register, monkeypatch):
    post = Post(monkeypatch)
    headers, order_id = _ready(client, register)
    post.wallet = 50
    assert _buy(client, headers, order_id).status_code == 502
    assert post.checkouts == []


def test_a_rejected_purchase_can_be_tried_again(client, register, monkeypatch):
    post = Post(monkeypatch)
    headers, order_id = _ready(client, register)
    post.checkout_error = internetmarke_service.InternetmarkeError("abgelehnt")
    assert _buy(client, headers, order_id).status_code == 502
    post.checkout_error = None
    assert _buy(client, headers, order_id).status_code == 201


def test_an_unclear_purchase_blocks_until_the_seller_confirms(client, register, monkeypatch):
    post = Post(monkeypatch)
    headers, order_id = _ready(client, register)
    post.checkout_error = internetmarke_service.InternetmarkeOutcomeUnknown("Abbruch.")
    response = _buy(client, headers, order_id)
    assert response.status_code == 502 and "1234567" in response.json()["detail"]
    post.checkout_error = None
    assert _buy(client, headers, order_id).status_code == 409
    purchase = client.get("/internetmarke/purchases", headers=headers).json()[0]
    assert purchase["status"] == "UNKNOWN"
    assert client.post(f"/internetmarke/purchases/{purchase['id']}/not-charged", headers=headers).status_code == 200
    assert _buy(client, headers, order_id).status_code == 201


def test_a_failed_download_never_pays_again(client, register, monkeypatch):
    post = Post(monkeypatch)
    headers, order_id = _ready(client, register)
    post.download_error = internetmarke_service.InternetmarkeError("Download kaputt.")
    response = _buy(client, headers, order_id)
    assert response.status_code == 201 and response.json()["status"] == "BOUGHT"
    assert response.json()["has_label"] is False
    assert _buy(client, headers, order_id).status_code == 409
    post.download_error = None
    again = client.post(f"/internetmarke/purchases/{response.json()['id']}/fetch-label", headers=headers)
    assert again.json()["has_label"] is True and len(post.checkouts) == 1


def test_without_a_printer_the_label_is_kept_for_download(client, register, monkeypatch):
    Post(monkeypatch)
    headers, order_id = _ready(client, register, agent=False)
    response = _buy(client, headers, order_id).json()
    assert response["has_label"] is True and response["print_job_id"] is None


def test_credentials_are_encrypted_and_never_returned(client, register, monkeypatch):
    Post(monkeypatch)
    headers, _ = _ready(client, register)
    status = client.get("/internetmarke/status", headers=headers).text
    assert "geheim-123" not in status and "kasse@example.com" not in status
    stored = _rows("SELECT username_encrypted, password_encrypted FROM internetmarke_accounts")[0]
    assert "geheim-123" not in stored[1] and "kasse@example.com" not in stored[0]


def test_another_seller_cannot_see_or_buy_for_the_order(client, register, monkeypatch):
    post = Post(monkeypatch)
    headers, order_id = _ready(client, register)
    purchase = _buy(client, headers, order_id).json()
    other, _ = register()
    assert client.post("/internetmarke/connect", headers=other, json=CONNECT).status_code == 200
    client.put("/internetmarke/page-format", headers=other, json={"page_format_id": 1})
    assert _buy(client, other, order_id).status_code == 404
    assert client.get(f"/internetmarke/purchases/{purchase['id']}/label", headers=other).status_code == 404
    assert len(post.checkouts) == 1


@pytest.mark.parametrize("answer,unknown", [
    (httpx.Response(400, json={"detail": "x"}), False),
    (httpx.Response(500), True),
    (httpx.Response(200, text="not json"), True),
    (httpx.ReadTimeout("slow"), True),
    (httpx.ConnectError("down"), False),
])
def test_only_a_clear_rejection_counts_as_not_charged(monkeypatch, answer, unknown):
    def post(*args, **kwargs):
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(internetmarke_service.InternetmarkeError) as caught:
        internetmarke_service.checkout(internetmarke_service.Session("t", None), "1", 1, 1, 1, {}, {})
    assert isinstance(caught.value, internetmarke_service.InternetmarkeOutcomeUnknown) is unknown


@pytest.mark.parametrize("link", ["http://internetmarke.deutschepost.de/a.pdf", "https://evil.example/a.pdf",
                                  "https://deutschepost.de.evil.example/a.pdf", ""])
def test_the_label_is_only_loaded_from_deutsche_post(monkeypatch, link):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("must not be requested"))
    with pytest.raises(internetmarke_service.InternetmarkeError):
        internetmarke_service.download_label(link)


def test_the_list_is_complete_with_kompaktbrief_first():
    offered = internetmarke_products.available(date(2026, 11, 28))
    assert len(offered) == 51 and sum(p.international for p in offered) == 14
    assert [(p.code, p.price_cents) for p in offered[:2]] == [(11, 110), (21, 180)]
    assert all(p.max_length_mm and p.max_width_mm and p.max_height_mm for p in offered)
    assert internetmarke_products.available(date(2027, 1, 1)) == []


@pytest.mark.parametrize("country,international,expected", [
    ("DE", True, 400), ("AT", False, 400), ("AT", True, 201), ("ZZ", True, 400)])
def test_postage_must_match_the_destination(client, register, monkeypatch, country, international, expected):
    post = Post(monkeypatch)
    product = internetmarke_products.Product(999, "Testbrief", 100, date(2000, 1, 1), date(2999, 1, 1),
                                             international=international)
    monkeypatch.setattr(internetmarke_products, "PRODUCTS", (product,))
    headers, order_id = _ready(client, register)
    with create_engine(settings.database_url).begin() as conn:
        conn.execute(text("UPDATE orders SET recipient_country_code = :c"), {"c": country})
    assert _buy(client, headers, order_id).status_code == expected
    assert len(post.checkouts) == (1 if expected == 201 else 0)
    if expected == 201:
        assert post.checkouts[0][-1]["country"] == "AUT"


@pytest.mark.parametrize("cleanup", ["retention", "ebay_deletion"])
def test_the_buyer_is_removed_from_bought_stamps_but_the_cost_stays(client, register, monkeypatch, cleanup):
    Post(monkeypatch)
    headers, order_id = _ready(client, register)
    assert _buy(client, headers, order_id).status_code == 201
    with Session(engine) as db:
        if cleanup == "retention":
            retention_service.run(db, now=datetime.now(timezone.utc) + timedelta(days=91))
        else:
            ebay_deletion_service._wipe_order(db.get(Order, uuid.UUID(order_id)), db)
            db.commit()
    purchase = client.get("/internetmarke/purchases", headers=headers).json()[0]
    assert purchase["status"] == "BOUGHT" and purchase["price_cents"] == 100
    assert purchase["has_label"] is False and purchase["can_fetch_label"] is False
    assert _rows("SELECT label_pdf, label_link, voucher_id, track_id FROM internetmarke_purchases")[0] == (None,) * 4
    assert _buy(client, headers, order_id).status_code in (400, 409)  # never paid twice
    assert client.post(f"/internetmarke/purchases/{purchase['id']}/fetch-label", headers=headers).status_code == 400

