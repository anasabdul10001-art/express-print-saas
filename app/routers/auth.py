import uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.config import settings
from app.core.deps import TRIAL_EXPIRED_DETAIL, get_current_user, get_db, trial_expired
from app.core.security import create_access_token, generate_api_key, hash_api_key, hash_password, verify_password
from app.models.affiliate import Affiliate, AffiliateReferral
from app.models.password_reset_token import PasswordResetToken
from app.models.plan import Plan
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.auth import (
    ForgotPasswordRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenResponse,
    UpdateEmailRequest,
    UpdatePasswordRequest,
    UpdateProfileRequest,
    UserMeOut,
)
from app.services.email_service import send_password_reset_email

router = APIRouter(prefix="/auth", tags=["auth"])

RESET_TOKEN_TTL_HOURS = 1

# Reuses the same Supabase bucket product images and the site logo already
# go into (see app/routers/uploads.py, app/routers/admin.py) rather than
# requiring a separate bucket just for avatars.
AVATAR_BUCKET = "product-images"
ALLOWED_AVATAR_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.email == payload.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    if not payload.early_service_consent:
        raise HTTPException(
            status_code=400,
            detail="Bitte bestätige, dass die Nutzung sofort beginnen soll, um die Registrierung abzuschließen.",
        )

    plan = None
    if payload.plan_name:
        plan = (
            db.query(Plan)
            .filter(Plan.is_active == True, Plan.name.ilike(payload.plan_name))  # noqa: E712
            .first()
        )
    if plan is None:
        # No plan requested (or the name didn't match anything active) -
        # fall back to the recommended plan, or the first one shown on the
        # pricing page, so a direct registration still starts a trial.
        plan = (
            db.query(Plan)
            .filter(Plan.is_active == True)  # noqa: E712
            .order_by(Plan.is_recommended.desc(), Plan.display_order.asc(), Plan.created_at.asc())
            .first()
        )

    # Tenant and User are created in the SAME transaction: if anything fails
    # before commit(), both roll back together - no orphaned tenant left behind.
    tenant = Tenant(company_name=payload.company_name)
    if plan is not None:
        tenant.plan_id = plan.id
        if plan.trial_days:
            tenant.trial_ends_at = datetime.now(timezone.utc) + timedelta(days=plan.trial_days)
    db.add(tenant)
    db.flush()  # assigns tenant.id without committing yet

    user = User(
        tenant_id=tenant.id,
        email=payload.email,
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
        role="owner",
        early_service_consent_at=datetime.now(timezone.utc),
    )
    db.add(user)

    if payload.referral_code:
        affiliate = (
            db.query(Affiliate)
            .filter(Affiliate.referral_code == payload.referral_code, Affiliate.status == "ACTIVE")
            .first()
        )
        # Unknown/inactive code: ignore rather than fail the signup - see
        # RegisterRequest.referral_code's docstring.
        if affiliate:
            db.add(AffiliateReferral(affiliate_id=affiliate.id, referred_tenant_id=tenant.id))

    db.commit()
    db.refresh(user)

    token = create_access_token({"sub": str(user.id), "tenant_id": str(tenant.id)})
    return TokenResponse(access_token=token)


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Super Admins have their own dedicated sign-in (see /auth/admin-login)
    # and are deliberately not allowed through the regular, publicly linked
    # login page at all.
    if user.is_superadmin:
        raise HTTPException(status_code=403, detail="Bitte nutze die Admin-Anmeldung, um dich einzuloggen.")

    tenant = db.query(Tenant).filter(Tenant.id == user.tenant_id).first()
    if trial_expired(tenant):
        raise HTTPException(status_code=402, detail=TRIAL_EXPIRED_DETAIL)

    token = create_access_token({"sub": str(user.id), "tenant_id": str(user.tenant_id)})
    return TokenResponse(access_token=token)


@router.post("/admin-login", response_model=TokenResponse)
def admin_login(payload: LoginRequest, db: Session = Depends(get_db)):
    """
    Separate sign-in for the Super Admin panel, deliberately not reachable
    from the regular /auth/login used by tenant users (see there) or linked
    from anywhere in the public site - only whoever has the direct URL to
    admin-login.html can reach this.
    """
    user = db.query(User).filter(User.email == payload.email).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not user.is_superadmin:
        raise HTTPException(status_code=403, detail="Kein Zugriff auf die Admin-Anmeldung.")

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


@router.patch("/me/profile", response_model=UserMeOut)
def update_profile(
    payload: UpdateProfileRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    current_user.full_name = payload.full_name
    db.commit()
    db.refresh(current_user)
    return current_user


@router.post("/me/avatar", response_model=UserMeOut)
async def upload_avatar(
    file: UploadFile,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if file.content_type not in ALLOWED_AVATAR_TYPES:
        raise HTTPException(status_code=400, detail="Nur JPEG, PNG, WEBP oder GIF erlaubt.")

    contents = await file.read()
    if len(contents) > 2 * 1024 * 1024:  # 2 MB - a profile picture has no reason to be bigger
        raise HTTPException(status_code=400, detail="Datei zu groß (max. 2 MB).")

    extension = file.filename.rsplit(".", 1)[-1] if "." in file.filename else "jpg"
    # Fixed path (not a random uuid) so re-uploading simply overwrites the
    # previous picture instead of accumulating old files per user.
    path = f"avatars/{current_user.id}.{extension}"

    upload_url = f"{settings.supabase_url}/storage/v1/object/{AVATAR_BUCKET}/{path}"

    async with httpx.AsyncClient() as client:
        response = await client.post(
            upload_url,
            content=contents,
            headers={
                "Authorization": f"Bearer {settings.supabase_service_key}",
                "apikey": settings.supabase_service_key,
                "Content-Type": file.content_type,
                "x-upsert": "true",  # overwrite if an avatar was already uploaded at this path
            },
        )

    if response.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail="Profilbild-Upload fehlgeschlagen.")

    # Cache-bust: without a changing query string, browsers/CDNs would keep
    # showing the old picture at the same URL after a re-upload.
    public_url = f"{settings.supabase_url}/storage/v1/object/public/{AVATAR_BUCKET}/{path}?v={uuid.uuid4().hex[:8]}"

    current_user.avatar_url = public_url
    db.commit()
    db.refresh(current_user)
    return current_user


@router.patch("/me/password")
def update_password(
    payload: UpdatePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=401, detail="Aktuelles Passwort ist falsch.")

    current_user.password_hash = hash_password(payload.new_password)
    db.commit()

    return {"message": "Passwort erfolgreich geändert."}
