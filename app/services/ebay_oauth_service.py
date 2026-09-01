"""
eBay OAuth 2.0 (Authorization Code flow) for connecting a tenant's eBay
seller account.

Flow implemented here:
    1. build_authorize_url()  -> tenant clicks "Connect eBay", browser goes
                                   to eBay's consent screen
    2. eBay redirects back to our /ebay/callback with ?code=...&state=...
    3. handle_callback()      -> validates state (CSRF), exchanges the code
                                   for access + refresh tokens, stores them
                                   encrypted
    4. get_valid_access_token() -> called by the order-sync service whenever
                                     it needs to call eBay's API; transparently
                                     refreshes the token if it's expired
"""

import base64
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from uuid import UUID

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.core.crypto import decrypt_token, encrypt_token
from app.models.ebay_account import EbayAccount, EbayOAuthState

EBAY_URLS = {
    "SANDBOX": {
        "authorize": "https://auth.sandbox.ebay.com/oauth2/authorize",
        "token": "https://api.sandbox.ebay.com/identity/v1/oauth2/token",
    },
    "PRODUCTION": {
        "authorize": "https://auth.ebay.com/oauth2/authorize",
        "token": "https://api.ebay.com/identity/v1/oauth2/token",
    },
}

# Keep this list to exactly what the platform actually uses today.
# Adding scopes later just means re-running the consent flow, it's not a
# breaking change - so there's no reason to over-ask up front.
SCOPES = " ".join(
    [
        "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
        "https://api.ebay.com/oauth/api_scope/sell.inventory",
    ]
)

STATE_TTL_MINUTES = 10


def build_authorize_url(tenant_id: UUID, db: Session) -> str:
    state = secrets.token_urlsafe(32)
    db.add(
        EbayOAuthState(
            tenant_id=tenant_id,
            state=state,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=STATE_TTL_MINUTES),
        )
    )
    db.commit()

    params = {
        "client_id": settings.ebay_client_id,
        "redirect_uri": settings.ebay_ru_name,  # eBay's "RuName", not a raw URL
        "response_type": "code",
        "state": state,
        "scope": SCOPES,
    }
    base_url = EBAY_URLS[settings.ebay_environment]["authorize"]
    return f"{base_url}?{urlencode(params)}"


def handle_callback(code: str, state: str, db: Session) -> EbayAccount:
    oauth_state = db.query(EbayOAuthState).filter(EbayOAuthState.state == state).first()
    if not oauth_state:
        raise ValueError("invalid_or_unknown_state")

    tenant_id = oauth_state.tenant_id
    is_expired = oauth_state.expires_at < datetime.now(timezone.utc)

    # One-time use regardless of outcome - a state must never be replayable.
    db.delete(oauth_state)
    db.commit()

    if is_expired:
        raise ValueError("authorization_request_expired")

    payload = _exchange_code_for_tokens(code)
    return _store_tokens(tenant_id, payload, db)


def get_valid_access_token(account: EbayAccount, db: Session) -> str:
    """Returns a usable access token, refreshing first if needed. Called by
    whatever service talks to eBay's Order/Inventory APIs."""
    now = datetime.now(timezone.utc)
    buffer = timedelta(minutes=2)  # refresh a bit early, never on the exact edge

    if account.access_token_expires_at and account.access_token_expires_at > now + buffer:
        return decrypt_token(account.access_token_encrypted)

    return _refresh_access_token(account, db)


def disconnect(account: EbayAccount, db: Session) -> None:
    account.access_token_encrypted = None
    account.refresh_token_encrypted = None
    account.access_token_expires_at = None
    account.refresh_token_expires_at = None
    account.status = "DISCONNECTED"
    db.commit()


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------

def _basic_auth_header() -> dict:
    raw = f"{settings.ebay_client_id}:{settings.ebay_client_secret}".encode()
    return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}


def _exchange_code_for_tokens(code: str) -> dict:
    token_url = EBAY_URLS[settings.ebay_environment]["token"]
    resp = httpx.post(
        token_url,
        headers={**_basic_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.ebay_ru_name,
        },
        timeout=15.0,
    )
    if resp.status_code != 200:
        # Never leak client_secret; eBay's error body doesn't contain it, but
        # be deliberate about only surfacing the response body.
        raise ValueError(f"ebay_token_exchange_failed: {resp.text}")
    return resp.json()


def _store_tokens(tenant_id: UUID, payload: dict, db: Session) -> EbayAccount:
    account = db.query(EbayAccount).filter(EbayAccount.tenant_id == tenant_id).first()
    if not account:
        account = EbayAccount(tenant_id=tenant_id)
        db.add(account)

    now = datetime.now(timezone.utc)
    account.access_token_encrypted = encrypt_token(payload["access_token"])
    account.access_token_expires_at = now + timedelta(seconds=payload["expires_in"])

    if "refresh_token" in payload:
        account.refresh_token_encrypted = encrypt_token(payload["refresh_token"])
        # eBay typically issues an 18-month refresh token; fall back safely
        # if the field is ever missing from the response.
        refresh_ttl = payload.get("refresh_token_expires_in", 47_304_000)
        account.refresh_token_expires_at = now + timedelta(seconds=refresh_ttl)

    account.status = "CONNECTED"
    account.environment = settings.ebay_environment
    account.connected_at = account.connected_at or now
    db.commit()
    db.refresh(account)
    return account


def _refresh_access_token(account: EbayAccount, db: Session) -> str:
    if not account.refresh_token_encrypted:
        account.status = "ERROR"
        db.commit()
        raise ValueError("no_refresh_token_reconnect_required")

    refresh_token = decrypt_token(account.refresh_token_encrypted)
    token_url = EBAY_URLS[account.environment]["token"]
    resp = httpx.post(
        token_url,
        headers={**_basic_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": SCOPES,
        },
        timeout=15.0,
    )
    if resp.status_code != 200:
        account.status = "ERROR"
        db.commit()
        raise ValueError(f"ebay_token_refresh_failed: {resp.text}")

    payload = resp.json()
    now = datetime.now(timezone.utc)
    account.access_token_encrypted = encrypt_token(payload["access_token"])
    account.access_token_expires_at = now + timedelta(seconds=payload["expires_in"])
    account.status = "CONNECTED"
    db.commit()
    return payload["access_token"]
