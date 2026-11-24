"""
Shopify OAuth 2.0 (authorization-code grant) for connecting a tenant's store.

    1. build_authorize_url(shop)  -> merchant is sent to their store's consent screen
    2. Shopify redirects to /shopify/callback?code&hmac&shop&state&timestamp
    3. handle_callback()          -> checks the HMAC (Shopify signs every redirect with
                                     the app secret) and the CSRF state, exchanges the
                                     code for an offline access token, stores it encrypted
Offline tokens do not expire; if Shopify ever answers 401 the account is
marked ERROR and the merchant reconnects.
"""

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from uuid import UUID

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.core.crypto import decrypt_token, encrypt_token
from app.models.shopify_account import ShopifyAccount, ShopifyOAuthState

STATE_TTL_MINUTES = 10
_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*\.myshopify\.com$")


def normalize_shop(raw: str) -> str:
    """'My-Store', 'my-store.myshopify.com' or 'https://my-store.myshopify.com/admin'
    -> 'my-store.myshopify.com'. Anything that is not a myshopify.com host is
    rejected: the domain is used to build URLs the token is sent to, so it must
    never be attacker-chosen."""
    shop = (raw or "").strip().lower()
    shop = re.sub(r"^https?://", "", shop).split("/")[0]
    if shop and "." not in shop:
        shop += ".myshopify.com"
    if not _DOMAIN_RE.match(shop):
        raise ValueError("invalid_shop_domain")
    return shop


def configured() -> bool:
    return bool(settings.shopify_client_id and settings.shopify_client_secret)


def build_authorize_url(tenant_id: UUID, shop_input: str, db: Session) -> str:
    if not configured():
        raise ValueError("shopify_not_configured")
    shop = normalize_shop(shop_input)
    state = secrets.token_urlsafe(32)
    db.add(ShopifyOAuthState(
        tenant_id=tenant_id,
        shop_domain=shop,
        state=state,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=STATE_TTL_MINUTES),
    ))
    db.commit()
    params = {
        "client_id": settings.shopify_client_id,
        "scope": settings.shopify_scopes,
        "redirect_uri": settings.shopify_redirect_uri,
        "state": state,
    }
    return f"https://{shop}/admin/oauth/authorize?{urlencode(params)}"


def verify_hmac(query: dict) -> bool:
    """Shopify's signature: HMAC-SHA256 (app secret) over the sorted key=value
    pairs of the query string without the hmac parameter itself."""
    given = query.get("hmac")
    if not given or not settings.shopify_client_secret:
        return False
    message = "&".join(f"{k}={v}" for k, v in sorted(query.items()) if k != "hmac")
    expected = hmac.new(settings.shopify_client_secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, given)


def handle_callback(query: dict, db: Session) -> ShopifyAccount:
    if not verify_hmac(query):
        raise ValueError("invalid_hmac")
    code, state = query.get("code"), query.get("state")
    if not code or not state:
        raise ValueError("missing_params")

    oauth_state = db.query(ShopifyOAuthState).filter(ShopifyOAuthState.state == state).first()
    if not oauth_state:
        raise ValueError("invalid_or_unknown_state")
    tenant_id, expected_shop = oauth_state.tenant_id, oauth_state.shop_domain
    expired = oauth_state.expires_at < datetime.now(timezone.utc)
    db.delete(oauth_state)  # one-time use, whatever happens next
    db.commit()
    if expired:
        raise ValueError("authorization_request_expired")

    shop = normalize_shop(query.get("shop", ""))
    if shop != expected_shop:
        raise ValueError("shop_mismatch")

    resp = httpx.post(
        f"https://{shop}/admin/oauth/access_token",
        json={"client_id": settings.shopify_client_id, "client_secret": settings.shopify_client_secret, "code": code},
        timeout=15.0,
    )
    if resp.status_code != 200:
        raise ValueError(f"shopify_token_exchange_failed: {resp.text[:300]}")
    payload = resp.json()
    token = payload.get("access_token")
    if not token:
        raise ValueError("shopify_token_exchange_failed: no access_token")

    account = (
        db.query(ShopifyAccount)
        .filter(ShopifyAccount.tenant_id == tenant_id, ShopifyAccount.shop_domain == shop)
        .first()
    )
    if not account:
        account = ShopifyAccount(tenant_id=tenant_id, shop_domain=shop)
        db.add(account)
    now = datetime.now(timezone.utc)
    account.access_token_encrypted = encrypt_token(token)
    account.scopes = payload.get("scope")
    account.status = "CONNECTED"
    account.connected_at = account.connected_at or now
    db.commit()
    db.refresh(account)
    return account


def get_access_token(account: ShopifyAccount) -> str:
    if not account.access_token_encrypted:
        raise ValueError("no_token_reconnect_required")
    return decrypt_token(account.access_token_encrypted)


def disconnect(account: ShopifyAccount, db: Session) -> None:
    account.access_token_encrypted = None
    account.status = "DISCONNECTED"
    db.commit()
