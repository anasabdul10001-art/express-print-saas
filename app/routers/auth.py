from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.config import settings
from app.core.deps import get_current_user, get_db
from app.core.security import create_access_token, generate_api_key, hash_api_key, hash_password, verify_password
from app.models.password_reset_token import PasswordResetToken
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.auth import (
    ForgotPasswordRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenResponse,
    UpdateEmailRequest,
    UserMeOut,
)
from app.services.email_service import send_password_reset_email

router = APIRouter(prefix="/auth", tags=["auth"])

RESET_TOKEN_TTL_HOURS = 1


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.email == payload.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    # Tenant and User are created in the SAME transaction: if anything fails
    # before commit(), both roll back together - no orphaned tenant left behind.
    tenant = Tenant(company_name=payload.company_name)
    db.add(tenant)
    db.flush()  # assigns tenant.id without committing yet

    user = User(
        tenant_id=tenant.id,
        email=payload.email,
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
        role="owner",
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token({"sub": str(user.id), "tenant_id": str(tenant.id)})
    return TokenResponse(access_token=token)


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_access_token({"sub": str(user.id), "tenant_id": str(user.tenant_id)})
    return TokenResponse(access_token=token)


@router.post("/forgot-password")
def forgot_password(payload: ForgotPasswordRequest, db: Session = Depends(get_db)):
    """
    Always returns the same generic message, whether or not the email is
    registered - otherwise this endpoint would let anyone check which
    email addresses have an account here.
    """
    user = db.query(User).filter(User.email == payload.email).first()

    if user:
        raw_token = generate_api_key()
        db.add(PasswordResetToken(
            user_id=user.id,
            token_hash=hash_api_key(raw_token),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=RESET_TOKEN_TTL_HOURS),
        ))
        db.commit()

        reset_link = f"{settings.frontend_url}/reset-password.html?token={raw_token}"
        try:
            send_password_reset_email(user.email, reset_link)
        except RuntimeError:
            pass  # don't leak email-delivery failures to the caller either

    return {"message": "Falls ein Konto mit dieser E-Mail-Adresse existiert, wurde eine E-Mail mit einem Link zum Zurücksetzen gesendet."}


@router.post("/reset-password")
def reset_password(payload: ResetPasswordRequest, db: Session = Depends(get_db)):
    token_hash = hash_api_key(payload.token)
    reset_token = db.query(PasswordResetToken).filter(PasswordResetToken.token_hash == token_hash).first()

    if not reset_token or reset_token.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Der Link ist ungültig oder abgelaufen. Bitte fordere einen neuen an.")

    user = db.query(User).filter(User.id == reset_token.user_id).first()
    user.password_hash = hash_password(payload.new_password)
    db.delete(reset_token)  # single-use
    db.commit()

    return {"message": "Passwort erfolgreich geändert. Du kannst dich jetzt anmelden."}


@router.get("/me", response_model=UserMeOut)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@router.patch("/me/email", response_model=UserMeOut)
def update_email(
    payload: UpdateEmailRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=401, detail="Passwort ist falsch.")

    existing = db.query(User).filter(User.email == payload.email, User.id != current_user.id).first()
    if existing:
        raise HTTPException(status_code=409, detail="Diese E-Mail-Adresse wird bereits verwendet.")

    current_user.email = payload.email
    db.commit()
    db.refresh(current_user)
    return current_user
