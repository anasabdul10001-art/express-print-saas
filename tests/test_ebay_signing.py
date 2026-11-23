"""eBay digital signature (RFC 9421, Ed25519) for the Finances API."""
import base64
import re
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.core.crypto import encrypt_token
from app.database import SessionLocal
from app.models.ebay_account import EbayAccount
from app.services import ebay_signing_service as svc

URL = "https://apiz.ebay.com/sell/finances/v1/transaction?limit=200"


def test_signature_verifies_and_matches_rfc9421_base():
    key = Ed25519PrivateKey.generate()
    h = svc.signature_headers("JWE.VALUE", key, "get", URL, created=1700000000)
    assert h["Signature-Input"] == 'sig1=("x-ebay-signature-key" "@method" "@path" "@authority");created=1700000000'
    base = chr(10).join([
        '"x-ebay-signature-key": JWE.VALUE',
        '"@method": GET',
        '"@path": /sell/finances/v1/transaction',
        '"@authority": apiz.ebay.com',
        '"@signature-params": ("x-ebay-signature-key" "@method" "@path" "@authority");created=1700000000',
    ])
    sig = base64.b64decode(re.fullmatch(r"sig1=:(.+):", h["Signature"]).group(1))
    key.public_key().verify(sig, base.encode())  # raises if wrong


def test_private_key_loads_with_or_without_pem_header():
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    bare = "".join(pem.strip().splitlines()[1:-1])
    for value in (pem, bare):
        loaded = svc._load_private_key(value)
        assert loaded.sign(b"x") == key.sign(b"x")


def test_key_is_created_once_and_stored_encrypted(register, client, monkeypatch):
    headers, _ = register()
    tenant_id = client.get("/auth/me", headers=headers).json()["tenant_id"]
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    calls = []

    class R:
        status_code = 201
        text = ""
        def json(self):
            return {"signingKeyId": "k1", "jwe": "JWE", "privateKey": pem}

    def fake_post(url, **kw):
        calls.append(url)
        if "oauth2/token" in url:
            r = R(); r.status_code = 200; r.json = lambda: {"access_token": "app"}; return r
        return R()

    monkeypatch.setattr(svc.httpx, "post", fake_post)
    with SessionLocal() as db:
        acc = EbayAccount(id=uuid.uuid4(), tenant_id=tenant_id, environment="PRODUCTION", status="CONNECTED")
        db.add(acc); db.commit()
        h1 = svc.signed_headers(acc, db, "GET", URL)
        h2 = svc.signed_headers(acc, db, "GET", URL)
        assert h1["x-ebay-signature-key"] == "JWE" == h2["x-ebay-signature-key"]
        assert sum("signing_key" in c for c in calls) == 1
        assert "BEGIN" not in acc.signing_private_key_encrypted
