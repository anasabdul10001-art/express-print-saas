import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from jose import jwt
from passlib.context import CryptContext

from app.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.access_token_expire_minutes)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.secret_key, algorithm=settings.algorithm)


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])


def generate_api_key() -> str:
    """
    Generates a 256-bit random key for a Print Agent. This is NOT a password,
    so it doesn't need bcrypt's slow hashing - it needs to survive a HIGH
    QUERY VOLUME (the Agent polls every ~7 seconds, 24/7). We use SHA-256
    instead of bcrypt for verification speed. This is safe here because the
    key itself has 256 bits of randomness (unguessable), unlike a
    human-chosen password which needs bcrypt's slowness to resist
    brute-forcing a small pool of likely values.
    """
    return secrets.token_urlsafe(32)


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()
