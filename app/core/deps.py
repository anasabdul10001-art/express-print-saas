from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Header, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy.orm import Session

from app.core.security import decode_access_token, hash_api_key
from app.database import get_db  # noqa: F401 - re-exported so existing `from app.core.deps import get_db` imports keep working
from app.models.affiliate import Affiliate
from app.models.print_agent import PrintAgent
from app.models.tenant import Tenant
from app.models.user import User

# tokenUrl is just used for Swagger UI's "Authorize" button - it doesn't
# affect actual auth logic.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

TRIAL_EXPIRED_DETAIL = "Deine kostenlose Testphase ist abgelaufen. Bitte wähle einen bezahlten Plan, um fortzufahren."


def trial_expired(tenant: Tenant | None) -> bool:
    """
    True once a tenant's trial deadline has passed. `trial_ends_at` is null
    for tenants that never had a trial (no plan matched at signup, or the
    plan has no trial_days) - those are never blocked here. There's no
    "paid/subscribed" flag yet (see app/models/plan.py), so once a trial
    ends there is currently no way back in except a superadmin manually
    clearing trial_ends_at - that's the intended behavior until real
    billing exists.
    """
    return tenant is not None and tenant.trial_ends_at is not None and tenant.trial_ends_at < datetime.now(timezone.utc)


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

    # Superadmins run the platform - a trial deadline on their own tenant
    # (if they even have one) never applies to them.
    if not user.is_superadmin:
        tenant = db.query(Tenant).filter(Tenant.id == user.tenant_id).first()
        if trial_expired(tenant):
            raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=TRIAL_EXPIRED_DETAIL)

    return user


def get_current_superadmin(current_user: User = Depends(get_current_user)) -> User:
    """
    Gate for the cross-tenant Super Admin panel. `is_superadmin` is a
    platform-level flag on the user row, unrelated to their `role` within
    their own tenant, and is never settable through any tenant-facing
    endpoint - only ever flipped directly in the database.
    """
    if not current_user.is_superadmin:
        raise HTTPException(status_code=403, detail="Nur für Plattform-Administratoren")
    return current_user


def get_current_affiliate(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> Affiliate:
    """
    Authenticates an affiliate in the self-service portal via JWT bearer
    token - a completely separate identity from `get_current_user`'s tenant
    users, even for an affiliate who also happens to be a tenant (see
    Affiliate.password_hash's docstring). The "scope" claim (absent from
    regular user tokens) keeps the two token kinds from being interchangeable.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_access_token(token)
        if payload.get("scope") != "affiliate":
            raise credentials_exception
        affiliate_id = payload.get("sub")
        if affiliate_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    affiliate = db.query(Affiliate).filter(Affiliate.id == affiliate_id).first()
    if affiliate is None:
        raise credentials_exception
    if affiliate.status != "ACTIVE":
        raise HTTPException(status_code=403, detail="Dieses Partnerkonto wurde deaktiviert.")

    return affiliate


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
