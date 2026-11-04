"""The connected eBay username and refresh tokens issued before the identity scope."""

import httpx

from app.core.crypto import encrypt_token
from app.models.ebay_account import EbayAccount
from app.services import ebay_oauth_service
from app.services.ebay_oauth_service import LEGACY_SCOPES, SCOPES, _fetch_identity, _refresh_access_token


class _FakeDb:
    def commit(self):
        pass


def test_fetch_identity_returns_username_and_id(monkeypatch):
    def fake_get(url, **kwargs):
        assert url == "https://apiz.ebay.com/commerce/identity/v1/user/"
        return httpx.Response(200, json={"username": "lamarconnect", "userId": "abc123"})

    monkeypatch.setattr(ebay_oauth_service.httpx, "get", fake_get)
    assert _fetch_identity("token", "PRODUCTION") == ("lamarconnect", "abc123")


def test_fetch_identity_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(ebay_oauth_service.httpx, "get", lambda url, **kwargs: httpx.Response(403, text="denied"))
    assert _fetch_identity("token", "PRODUCTION") == (None, None)

    def network_down(url, **kwargs):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(ebay_oauth_service.httpx, "get", network_down)
    assert _fetch_identity("token", "PRODUCTION") == (None, None)


def test_refresh_falls_back_to_legacy_scopes(monkeypatch):
    scopes_tried = []

    def fake_post(url, **kwargs):
        scopes_tried.append(kwargs["data"]["scope"])
        if kwargs["data"]["scope"] == SCOPES:
            return httpx.Response(400, json={"error": "invalid_scope"})
        return httpx.Response(200, json={"access_token": "new-token", "expires_in": 7200})

    monkeypatch.setattr(ebay_oauth_service.httpx, "post", fake_post)
    account = EbayAccount(environment="PRODUCTION", refresh_token_encrypted=encrypt_token("old-refresh"))

    assert _refresh_access_token(account, _FakeDb()) == "new-token"
    assert scopes_tried == [SCOPES, LEGACY_SCOPES]
    assert account.status == "CONNECTED"
