"""A tenant can connect several eBay accounts; reconnecting one never duplicates it."""

from app.database import SessionLocal
from app.services import ebay_oauth_service, ebay_order_service


class _Resp:
    status_code = 200
    text = ""

    def __init__(self, orders):
        self._orders = orders

    def json(self):
        return {"orders": self._orders}


def _tenant(register, client):
    headers, _ = register()
    return headers, client.get("/auth/me", headers=headers).json()["tenant_id"]


def _store(tenant_id, name, immutable):
    payload = {"access_token": "a", "expires_in": 7200, "refresh_token": "r"}
    with SessionLocal() as db:
        acc = ebay_oauth_service._store_tokens(tenant_id, payload, db, name, immutable)
        acc.ebay_user_id, acc.ebay_immutable_id = name, immutable
        db.commit()
        return str(acc.id)


def test_second_account_gets_own_row_and_same_account_is_reused(register, client):
    headers, tenant_id = _tenant(register, client)
    a1 = _store(tenant_id, "shop_a", "id-a")
    a2 = _store(tenant_id, "shop_b", "id-b")
    assert a1 != a2
    assert _store(tenant_id, "shop_a", "id-a") == a1  # reconnect -> same row
    names = sorted(a["ebay_user_id"] for a in client.get("/ebay/accounts", headers=headers).json())
    assert names == ["shop_a", "shop_b"]


def test_disconnect_one_account_keeps_the_other(register, client):
    headers, tenant_id = _tenant(register, client)
    a1 = _store(tenant_id, "shop_a", "id-a")
    _store(tenant_id, "shop_b", "id-b")
    assert client.post(f"/ebay/disconnect?account_id={a1}", headers=headers).status_code == 200
    names = [a["ebay_user_id"] for a in client.get("/ebay/accounts", headers=headers).json()]
    assert names == ["shop_b"]


def test_sync_covers_all_accounts_and_tags_orders(register, client, monkeypatch):
    headers, tenant_id = _tenant(register, client)
    _store(tenant_id, "shop_a", "id-a")
    _store(tenant_id, "shop_b", "id-b")
    monkeypatch.setattr(ebay_order_service.ebay_oauth_service, "get_valid_access_token", lambda a, d: "t")
    calls = {"n": 0}

    def fake_get(url, **kwargs):
        if "finances" in url:
            r = _Resp([])
            r.json = lambda: {"transactions": []}
            return r
        label = "A" if calls["n"] == 0 else "B"
        calls["n"] += 1
        return _Resp([{"orderId": f"ord-{label}", "lineItems": [
            {"sku": f"S-{label}", "title": "x", "quantity": 1, "lineItemCost": {"value": "5.00"}}]}])

    monkeypatch.setattr(ebay_order_service.httpx, "get", fake_get)
    result = client.post("/ebay/sync-orders", headers=headers).json()
    assert result["created"] == 2
    out = client.get("/orders", headers=headers).json()
    assert sorted(o["ebay_account_name"] for o in out) == ["shop_a", "shop_b"]
