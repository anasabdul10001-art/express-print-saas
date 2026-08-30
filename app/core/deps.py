from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Header, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy.orm import Session

from app.core.security import decode_access_token, hash_api_key
from app.database import get_db  # noqa: F401 - re-exported so existing `from app.core.deps import get_db` imports keep working
from app.models.print_agent import PrintAgent
from app.models.user import User

# tokenUrl is just used for Swagger UI's "Authorize" button - it doesn't
# affect actual auth logic.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Authenticates a human user (dashboard/API) via JWT bearer token."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_access_token(token)
        user_id = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.query(User).filter(User.id == user_id).first()
    if user is None or not user.is_active:
        raise credentials_exception
    return user


def get_print_agent_from_api_key(
    authorization: str = Header(..., alias="Authorization"),
    db: Session = Depends(get_db),
) -> PrintAgent:
    """
    Authenticates a Print Agent (the local Windows script) via a long-lived
    API key sent as 'Authorization: Bearer <key>' - deliberately NOT the same
    JWT scheme used for human users, since this token never expires on its
    own (it's tied to a physical machine, not a login session) and is
    revoked by disabling the agent, not by expiry.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    raw_key = authorization[len("Bearer "):].strip()
    key_hash = hash_api_key(raw_key)

    agent = db.query(PrintAgent).filter(PrintAgent.api_key_hash == key_hash).first()
    if not agent:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if agent.status == "disabled":
        raise HTTPException(status_code=403, detail="This print agent has been disabled")

    agent.last_seen_at = datetime.now(timezone.utc)
    agent.status = "online"
    db.commit()

    return agent
