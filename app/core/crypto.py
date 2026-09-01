"""
Encrypts/decrypts secrets we store at rest (currently: eBay OAuth tokens).

This is symmetric encryption (Fernet, AES128-CBC + HMAC under the hood),
NOT the same thing as password hashing in security.py. Passwords are
hashed one-way because we only ever need to *verify* them. OAuth tokens
must be recoverable in plaintext (we need to send the real access token
to eBay's API), so they're encrypted two-way instead.

TOKEN_ENCRYPTION_KEY must be a Fernet key: 32 url-safe base64-encoded
bytes. Generate one with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from cryptography.fernet import Fernet

from app.config import settings

_fernet = Fernet(settings.token_encryption_key.encode())


def encrypt_token(raw_value: str) -> str:
    return _fernet.encrypt(raw_value.encode()).decode()


def decrypt_token(encrypted_value: str) -> str:
    return _fernet.decrypt(encrypted_value.encode()).decode()
