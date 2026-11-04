"""
eBay's Marketplace Account Deletion requirement. When any eBay user asks eBay
to delete their data, eBay notifies every app that stores eBay data, and the
app must remove that user's data for good.

An order's money and product figures stay, because they describe the seller's
own bookkeeping and aren't the buyer's data. Everything identifying the buyer
goes. The eBay order id is replaced by a one-way hash rather than removed, so a
later sync recognises the order and doesn't import the buyer again.
"""

import base64
import hashlib
import json
import re
import time

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_der_public_key
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.models.ebay_account import EbayAccount
from app.models.order import Order
from app.models.print_job import PrintJob
from app.models.stock_movement import StockMovement

DELETION_TOPIC = "MARKETPLACE_ACCOUNT_DELETION"

API_BASE = {
    "SANDBOX": "https://api.sandbox.ebay.com",
    "PRODUCTION": "https://api.ebay.com",
}
APP_SCOPE = "https://api.ebay.com/oauth/api_scope"

# eBay asks for public keys to be cached rather than fetched per notification.
PUBLIC_KEY_TTL_SECONDS = 3600
_public_keys: dict[str, tuple[float, str]] = {}


class PublicKeyUnavailable(Exception):
    pass


def challenge_response(challenge_code: str) -> str:
    raw = challenge_code + settings.ebay_deletion_verification_token + settings.ebay_deletion_endpoint
    return hashlib.sha256(raw.encode()).hexdigest()


def deleted_order_ref(ebay_order_id: str) -> str:
    return "deleted:" + hashlib.sha256(ebay_order_id.encode()).hexdigest()


def _fetch_public_key(key_id: str) -> str:
    base_url = API_BASE[settings.ebay_environment]
    try:
        token_resp = httpx.post(
            f"{base_url}/identity/v1/oauth2/token",
            auth=(settings.ebay_client_id, settings.ebay_client_secret),
            data={"grant_type": "client_credentials", "scope": APP_SCOPE},
            timeout=10.0,
        )
        token_resp.raise_for_status()
        key_resp = httpx.get(
            f"{base_url}/commerce/notification/v1/public_key/{key_id}",
            headers={"Authorization": f"Bearer {token_resp.json()['access_token']}"},
            timeout=10.0,
        )
        key_resp.raise_for_status()
        return key_resp.json()["key"]
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise PublicKeyUnavailable(type(exc).__name__) from exc


def _public_key(key_id: str) -> str:
    cached = _public_keys.get(key_id)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    key = _fetch_public_key(key_id)
    _public_keys[key_id] = (time.monotonic() + PUBLIC_KEY_TTL_SECONDS, key)
    return key


def signature_is_valid(body: bytes, header: str | None) -> bool:
    """Raises PublicKeyUnavailable when the answer can't be known yet."""
    if not header:
        return False
    try:
        decoded = json.loads(base64.b64decode(header))
        key_id = decoded["kid"]
        signature = base64.b64decode(decoded["signature"])
    except (ValueError, KeyError, TypeError):
        return False
    # kid goes into a URL, so it's restricted to what a key id can look like.
    if not isinstance(key_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key_id):
        return False

    # eBay's key text isn't reliably line-wrapped PEM, so only the base64 inside is used.
    key_text = _public_key(key_id)
    key_base64 = re.sub(r"-----[A-Z ]+-----|\s", "", key_text)
    try:
        public_key = load_der_public_key(base64.b64decode(key_base64))
        public_key.verify(signature, body, ec.ECDSA(hashes.SHA1()))
    except (InvalidSignature, ValueError, TypeError, AttributeError):
        return False
    return True


def _wipe_order(order: Order, db: Session) -> None:
    old_ref = order.external_order_ref
    if old_ref and not old_ref.startswith("deleted:"):
        new_ref = deleted_order_ref(old_ref)
        order.external_order_ref = new_ref
        db.query(StockMovement).filter(
            StockMovement.tenant_id == order.tenant_id,
            StockMovement.reason == "ebay_sync",
            StockMovement.reference == old_ref,
        ).update({StockMovement.reference: new_ref}, synchronize_session=False)

    order.buyer_username = None
    order.recipient_name = None
    order.recipient_street1 = None
    order.recipient_street2 = None
    order.recipient_city = None
    order.recipient_state = None
    order.recipient_zip = None
    order.recipient_country_code = None
    order.recipient_phone = None
    order.tracking_number = None

    # A stored label PDF shows the buyer's name and address.
    db.query(PrintJob).filter(PrintJob.order_id == order.id).update(
        {PrintJob.label_pdf_data: None, PrintJob.error_message: None}, synchronize_session=False
    )


def delete_user_data(username: str | None, user_id: str | None, db: Session) -> None:
    """Safe to repeat, since eBay resends notifications until acknowledged."""
    identifiers = [value for value in (username, user_id) if value]
    if not identifiers:
        return

    # The user as a buyer in any seller's orders.
    for order in db.query(Order).filter(Order.buyer_username.in_(identifiers)).all():
        _wipe_order(order, db)

    # The user as a connected seller: their tokens and their imported orders.
    accounts = (
        db.query(EbayAccount)
        .filter(or_(EbayAccount.ebay_user_id.in_(identifiers), EbayAccount.ebay_immutable_id.in_(identifiers)))
        .all()
    )
    for account in accounts:
        imported_orders = (
            db.query(Order)
            .filter(
                Order.tenant_id == account.tenant_id,
                or_(Order.buyer_username.isnot(None), Order.recipient_name.isnot(None), Order.recipient_street1.isnot(None)),
            )
            .all()
        )
        for order in imported_orders:
            _wipe_order(order, db)

        account.ebay_user_id = None
        account.ebay_immutable_id = None
        account.access_token_encrypted = None
        account.refresh_token_encrypted = None
        account.access_token_expires_at = None
        account.refresh_token_expires_at = None
        account.status = "DISCONNECTED"

    db.commit()
