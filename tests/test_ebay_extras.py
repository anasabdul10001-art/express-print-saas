"""eBay extras (policies, traffic, cases, messages) and AI reply suggestions."""

import re
import uuid
import xml.etree.ElementTree as ET

from app.database import SessionLocal
from app.models.ebay_account import EbayAccount
from app.services import ai_reply_service, ebay_extras_service


class _Resp:
    def __init__(self, data=None, status=200, text=""):
        self._d, self.status_code, self.text = data, status, text or ""

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code != 200:
            import httpx
            raise httpx.HTTPError("boom")


def _connect(register, client, monkeypatch):
    headers, _ = register()
    tid = client.get("/auth/me", headers=headers).json()["tenant_id"]
    with SessionLocal() as db:
        db.add(EbayAccount(id=uuid.uuid4(), tenant_id=tid, environment="PRODUCTION", status="CONNECTED", ebay_user_id="shop"))
        db.commit()
    monkeypatch.setattr(ebay_extras_service.ebay_oauth_service, "get_valid_access_token", lambda a, d: "t")
    return headers


def test_shipping_policies_and_traffic(register, client, monkeypatch):
    headers = _connect(register, client, monkeypatch)

    def fake_get(url, **k):
        if "fulfillment_policy" in url:
            return _Resp({"fulfillmentPolicies": [{"name": "Standard", "handlingTime": {"value": 1},
                          "shippingOptions": [{"shippingServices": [{"shippingServiceCode": "DE_DHLPaket", "shippingCost": {"value": "3.99"}}]}]}]})
        return _Resp({"header": {"metrics": [{"metric": "LISTING_VIEWS_TOTAL"}, {"metric": "TRANSACTION"}]},
                      "records": [{"dimensionValues": [{"value": "111"}], "metricValues": [{"value": 50}, {"value": 2}]}]})

    monkeypatch.setattr(ebay_extras_service.httpx, "get", fake_get)
    pol = client.get("/ebay/shipping-policies", headers=headers).json()
    assert pol["accounts"][0]["policies"][0]["services"][0]["service"] == "DE_DHLPaket"
    tr = client.get("/ebay/traffic", headers=headers).json()
    assert tr["accounts"][0]["listings"][0] == {"item_id": "111", "views": 50, "sales": 2, "conversion": None}


def test_cases_collects_all_types_and_tolerates_failures(register, client, monkeypatch):
    headers = _connect(register, client, monkeypatch)

    def fake_get(url, **k):
        if "return/search" in url:
            return _Resp({"members": [{"returnId": "R1", "state": "OPEN", "buyerLoginName": "b1"}]})
        if "payment_dispute" in url:
            return _Resp({"paymentDisputeSummaries": [{"paymentDisputeId": "D1", "paymentDisputeStatus": "OPEN"}]})
        return _Resp(status=403, text="no scope")

    monkeypatch.setattr(ebay_extras_service.httpx, "get", fake_get)
    out = client.get("/ebay/cases", headers=headers).json()
    assert sorted(i["type"] for i in out["items"]) == ["dispute", "return"]
    assert len(out["errors"]) == 2


def test_mask_personal_data():
    masked = ai_reply_service.mask_personal_data(
        "Hallo, ich bin Max Mustermann, Hauptstraße 12, 04720 Döbeln, mail max@x.de, Tel +49 170 1234567", ["Max Mustermann"])
    for secret in ("Max", "Mustermann", "Hauptstraße", "04720", "max@x.de", "1234567"):
        assert secret not in masked


def test_suggest_reply_falls_through_providers(register, client, monkeypatch):
    headers = _connect(register, client, monkeypatch)
    no_key = client.post("/ebay/suggest-reply", headers=headers, json={"message": "Wo ist mein Paket?"})
    assert no_key.status_code == 503

    monkeypatch.setattr(ai_reply_service.settings, "groq_api_key", "g")
    monkeypatch.setattr(ai_reply_service.settings, "openrouter_api_key", "o")
    # keys alone are not enough: the drafts are off until switched on
    assert client.post("/ebay/suggest-reply", headers=headers, json={"message": "Hallo"}).status_code == 503
    monkeypatch.setattr(ai_reply_service.settings, "ai_reply_enabled", True)
    seen = []

    def fake_post(url, **k):
        seen.append((url, str(k.get("json"))))
        if "groq" in url:
            return _Resp(status=429)
        return _Resp({"choices": [{"message": {"content": "Guten Tag, ich sehe nach."}}]})

    monkeypatch.setattr(ai_reply_service.httpx, "post", fake_post)
    out = client.post("/ebay/suggest-reply", headers=headers,
                      json={"message": "Schreib an max@x.de", "buyer_name": "Max Muster"}).json()
    assert out == {"reply": "Guten Tag, ich sehe nach.", "provider": "openrouter", "sent_text": "Schreib an [E-MAIL]"}
    assert "'data_collection': 'deny'" in seen[-1][1]
    assert all("max@x.de" not in body for _, body in seen)


def test_subscription_raw_shows_ebays_answer_for_each_case(register, client, monkeypatch):
    headers = _connect(register, client, monkeypatch)
    sub = {"subscriptions": [{"subscriptionType": "BASIC", "status": "ACTIVE", "marketplaceId": "EBAY_DE"}]}
    monkeypatch.setattr(ebay_extras_service.httpx, "get", lambda url, **k: _Resp(sub))
    r = client.get("/ebay/subscription-raw", headers=headers).json()
    assert r[0]["ok"] is True and r[0]["data"]["subscriptions"][0]["status"] == "ACTIVE"
    # an account without a subscription / missing scope: eBay's error text comes back, no crash
    monkeypatch.setattr(ebay_extras_service.httpx, "get", lambda url, **k: _Resp(status=404, text="no subscription"))
    r = client.get("/ebay/subscription-raw", headers=headers).json()
    assert r[0]["ok"] is False and r[0]["http_status"] == 404 and "no subscription" in r[0]["error"]


def test_mask_cuts_signature_and_numbers():
    masked = ai_reply_service.mask_personal_data(
        "Hallo Frau Schulz, ich heiße Erika Beispiel. Wo bleibt Bestellung 05-15486-11749 "
        "(A0064CF4DB0000000161)?\n\nViele Grüße\nErika Beispiel\nBeispieldorf")
    for secret in ("Schulz", "Erika", "Beispiel", "15486", "A0064CF4"):
        assert secret not in masked
    assert "Wo bleibt Bestellung" in masked


def test_mask_keeps_a_question_that_follows_a_greeting_word():
    text = "Viele Grüße aus dem Urlaub, passt das Kabel an mein Handy?"
    assert ai_reply_service.mask_personal_data(text) == text


def test_suggest_reply_sends_only_the_message_and_rejects_html(register, client, monkeypatch):
    headers = _connect(register, client, monkeypatch)
    monkeypatch.setattr(ai_reply_service.settings, "groq_api_key", "g")
    monkeypatch.setattr(ai_reply_service.settings, "ai_reply_enabled", True)
    bodies = []

    def fake_post(url, **k):
        bodies.append(k["json"]["messages"][1]["content"])
        return _Resp({"choices": [{"message": {"content": "Guten Tag, [BITTE ERGÄNZEN: Status]"}}]})

    monkeypatch.setattr(ai_reply_service.httpx, "post", fake_post)
    client.post("/ebay/suggest-reply", headers=headers, json={"message": "Wo ist mein Paket?"})
    assert bodies == ["Nachricht des Käufers:\nWo ist mein Paket?\n\nSchreibe die Antwort."]
    html = client.post("/ebay/suggest-reply", headers=headers, json={"message": "<html><body>x</body></html>"})
    assert html.status_code == 422 and len(bodies) == 1


def test_messages_are_fetched_in_batches_of_ten_without_html(register, client, monkeypatch):
    headers = _connect(register, client, monkeypatch)
    batches = []

    def fake_trading(account, token, call, body):
        if "ReturnHeaders" in body:
            inner = "".join(f"<Message><MessageID>{i}</MessageID></Message>" for i in range(25))
        else:
            ids = re.findall(r"<MessageID>(\d+)</MessageID>", body)
            batches.append(len(ids))
            inner = "".join(f"<Message><MessageID>{i}</MessageID><Content>&lt;html&gt;</Content></Message>" for i in ids)
        return ET.fromstring(f'<R xmlns="urn:ebay:apis:eBLBaseComponents"><Messages>{inner}</Messages></R>')

    monkeypatch.setattr(ebay_extras_service, "_trading", fake_trading)
    items = client.get("/ebay/messages", headers=headers).json()["items"]
    assert batches == [10, 10]
    assert len(items) == 20 and all(m["text"] is None for m in items)
