"""
eBay digital signatures (RFC 9421, Ed25519) for the Finances API.

eBay requires them for calls made on behalf of EU/UK sellers; without them
it answers 403 "Missing x-ebay-signature-key header". One keypair per eBay
account is created lazily through the Key Management API (application
token), the private key is stored encrypted.
"""

import base64
import time
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import serialization
from sqlalchemy.orm import Session

from app.config import settings
from app.core.crypto import decrypt_token, encrypt_token
from app.models.ebay_account import EbayAccount

KEY_MANAGEMENT_BASE = {
    "SANDBOX": "https://apiz.sandbox.ebay.com",
    "PRODUCTION": "https://apiz.ebay.com",
}
TOKEN_URL = {
    "SANDBOX": "https://api.sandbox.ebay.com/identity/v1/oauth2/token",
    "PRODUCTION": "https://api.ebay.com/identity/v1/oauth2/token",
}


class SigningError(Exception):
    pass


def _application_token(environment: str) -> str:
    raw = f"{settings.ebay_client_id}:{settings.ebay_client_secret}".encode()
    resp = httpx.post(
        TOKEN_URL[environment],
        headers={"Authorization": f"Basic {base64.b64encode(raw).decode()}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
        timeout=20.0,
    )
    if resp.status_code != 200:
        raise SigningError(f"Application token: eBay antwortet {resp.status_code}")
    return resp.json()["access_token"]


def ensure_signing_key(account: EbayAccount, db: Session) -> None:
    if account.signing_jwe and account.signing_private_key_encrypted:
        return
    token = _application_token(account.environment)
    resp = httpx.post(
        f"{KEY_MANAGEMENT_BASE[account.environment]}/developer/key_management/v1/signing_key",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"signingKeyCipher": "ED25519"},
        timeout=20.0,
    )
    if resp.status_code not in (200, 201):
        raise SigningError(f"Signaturschlüssel: eBay antwortet {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if not data.get("jwe") or not data.get("privateKey"):
        raise SigningError("Signaturschlüssel: unerwartete Antwort von eBay.")
    account.signing_key_id = data.get("signingKeyId")
    account.signing_jwe = data["jwe"]
    account.signing_private_key_encrypted = encrypt_token(data["privateKey"])
    db.commit()


NL = chr(10)


def _load_private_key(value: str):
    value = value.strip()
    if "BEGIN" not in value:
        body = "".join(value.split())
        lines = NL.join(body[k:k + 64] for k in range(0, len(body), 64))
        value = "-----BEGIN PRIVATE KEY-----" + NL + lines + NL + "-----END PRIVATE KEY-----" + NL
    return serialization.load_pem_private_key(value.encode(), password=None)


def signature_headers(jwe: str, private_key, method: str, url: str, created: int | None = None) -> dict:
    """Headers for a request without body (GET): signs jwe, @method, @path, @authority."""
    parts = urlsplit(url)
    created = int(time.time()) if created is None else created
    params = f'("x-ebay-signature-key" "@method" "@path" "@authority");created={created}'
    base = NL.join([
        f'"x-ebay-signature-key": {jwe}',
        f'"@method": {method.upper()}',
        f'"@path": {parts.path}',
        f'"@authority": {parts.netloc}',
        f'"@signature-params": {params}',
    ])
    signature = base64.b64encode(private_key.sign(base.encode())).decode()
    return {
        "x-ebay-signature-key": jwe,
        "Signature-Input": f"sig1={params}",
        "Signature": f"sig1=:{signature}:",
    }


def signed_headers(account: EbayAccount, db: Session, method: str, url: str) -> dict:
    ensure_signing_key(account, db)
    key = _load_private_key(decrypt_token(account.signing_private_key_encrypted))
    return signature_headers(account.signing_jwe, key, method, url)
