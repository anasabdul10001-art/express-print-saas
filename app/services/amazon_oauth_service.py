"""
Amazon SP-API authorization (Login with Amazon, "website authorization workflow").

    1. build_authorize_url()  -> seller is sent to Seller Central's consent page
    2. Amazon redirects to /amazon/callback?spapi_oauth_code&selling_partner_id&state
    3. handle_callback()      -> checks our one-time state, exchanges the code (valid
                                 5 minutes) for a long-lived refresh token, stores it encrypted
    4. get_access_token()     -> refresh token -> short-lived access token (kept in memory)

For the owner's OWN seller account there is a shortcut that needs no public
app: "self-authorize" the app in Seller Central (Apps and Services > Develop
Apps > Authorize) and paste the refresh token it shows - see
connect_with_refresh_token().
"""

import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from uuid import UUID

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.core.crypto import decrypt_token, encrypt_token
from app.models.amazon_account import AmazonAccount, AmazonOAuthState

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
STATE_TTL_MINUTES = 10

# account id -> (access token, monotonic expiry); tokens live ~1 hour.
_token_cache: dict = {}


def configured() -> bool:
    return bool(settings.amazon_lwa_client_id and settings.amazon_lwa_client_secret)


def consent_flow_configured() -> bool:
    return configured() and bool(settings.amazon_application_id)


def build_authorize_url(tenant_id: UUID, db: Session) -> str:
    if not consent_flow_configured():
        raise ValueError("amazon_not_configured")
    state = secrets.token_urlsafe(32)
    db.add(AmazonOAuthState(
        tenant_id=tenant_id, state=state,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=STATE_TTL_MINUTES),
    ))
    db.commit()
    params = {"application_id": settings.amazon_application_id, "state": state}
    if settings.amazon_draft_app:
        params["version"] = "beta"  # only while the app is still in Draft status
    return f"{settings.amazon_seller_central_url}/apps/authorize/consent?{urlencode(params)}"


def _lwa(data: dict) -> dict:
    resp = httpx.post(
        LWA_TOKEN_URL,
        data={**data, "client_id": settings.amazon_lwa_client_id, "client_secret": settings.amazon_lwa_client_secret},
        timeout=15.0,
    )
    if resp.status_code != 200:
        raise ValueError(f"amazon_lwa_failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


def _store(tenant_id: UUID, seller_id: str, refresh_token: str, db: Session) -> AmazonAccount:
    account = (
        db.query(AmazonAccount)
        .filter(AmazonAccount.tenant_id == tenant_id, AmazonAccount.seller_id == seller_id)
        .first()
    )
    if not account:
        account = AmazonAccount(tenant_id=tenant_id, seller_id=seller_id, marketplace_id=settings.amazon_marketplace_id)
        db.add(account)
    account.refresh_token_encrypted = encrypt_token(refresh_token)
    account.status = "CONNECTED"
    account.connected_at = account.connected_at or datetime.now(timezone.utc)
    db.commit()
    db.refresh(account)
    _token_cache.pop(str(account.id), None)
    return account


def handle_callback(query: dict, db: Session) -> AmazonAccount:
    code, state, seller_id = query.get("spapi_oauth_code"), query.get("state"), query.get("selling_partner_id")
    if not code or not state or not seller_id:
        raise ValueError("missing_params")

    oauth_state = db.query(AmazonOAuthState).filter(AmazonOAuthState.state == state).first()
    if not oauth_state:
        raise ValueError("invalid_or_unknown_state")
    tenant_id = oauth_state.tenant_id
    expired = oauth_state.expires_at < datetime.now(timezone.utc)
    db.delete(oauth_state)  # one-time use, whatever happens next
    db.commit()
    if expired:
        raise ValueError("authorization_request_expired")

    payload = _lwa({"grant_type": "authorization_code", "code": code, "redirect_uri": settings.amazon_redirect_uri})
    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        raise ValueError("amazon_lwa_failed: no refresh_token")
    return _store(tenant_id, seller_id, refresh_token, db)


def connect_with_refresh_token(tenant_id: UUID, seller_id: str, refresh_token: str, db: Session) -> AmazonAccount:
    """Own-account shortcut: the token must work for OUR LWA app (checked by
    actually refreshing it once) before anything is stored."""
    if not configured():
        raise ValueError("amazon_not_configured")
    seller_id, refresh_token = seller_id.strip(), refresh_token.strip()
    if not seller_id or not refresh_token:
        raise ValueError("missing_params")
    payload = _lwa({"grant_type": "refresh_token", "refresh_token": refresh_token})
    if not payload.get("access_token"):
        raise ValueError("amazon_lwa_failed: no access_token")
    return _store(tenant_id, seller_id, refresh_token, db)


def get_access_token(account: AmazonAccount, db: Session) -> str:
    cached = _token_cache.get(str(account.id))
    if cached and cached[1] > time.monotonic() + 60:
        return cached[0]
    if not account.refresh_token_encrypted:
        raise ValueError("no_refresh_token_reconnect_required")
    try:
        payload = _lwa({"grant_type": "refresh_token", "refresh_token": decrypt_token(account.refresh_token_encrypted)})
    except ValueError as exc:
        if "invalid_grant" in str(exc):
            account.status = "ERROR"  # revoked or expired (re-authorize yearly)
            db.commit()
        raise
    token = payload["access_token"]
    _token_cache[str(account.id)] = (token, time.monotonic() + int(payload.get("expires_in", 3600)))
    return token


def disconnect(account: AmazonAccount, db: Session) -> None:
    account.refresh_token_encrypted = None
    account.status = "DISCONNECTED"
    _token_cache.pop(str(account.id), None)
    db.commit()
