"""
Self-service portal for affiliates (see app/models/affiliate.py) - lets an
affiliate log in on their own, independent of any ShipSync tenant login they
might also have, and see their own referral link, balance, and history
without going through the admin panel.

Login is possible only once a password has been set (via the emailed
"set your password" link - triggered when an admin creates an affiliate
with an email, or via the forgot-password flow below). An affiliate with no
email on file can never get portal access; the admin panel remains the only
way to manage them.
"""

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.core.deps import get_current_affiliate, get_db
from app.core.rate_limit import limiter
from app.core.security import create_access_token, hash_api_key, hash_password, verify_password
from app.models.affiliate import Affiliate, AffiliateCommission, AffiliatePayout, AffiliateReferral
from app.models.affiliate_password_token import AffiliatePasswordToken
from app.schemas.affiliate import (
    AffiliateCommissionOut,
    AffiliateForgotPasswordRequest,
    AffiliateLoginRequest,
    AffiliateMeOut,
    AffiliatePayoutOut,
    AffiliateSetPasswordRequest,
)
from app.schemas.auth import TokenResponse
from app.services.email_service import send_affiliate_password_email

router = APIRouter(prefix="/affiliate", tags=["affiliate-portal"])

SET_PASSWORD_TOKEN_TTL_HOURS = 24


def _with_referral_count(db: Session, affiliate: Affiliate) -> AffiliateMeOut:
    out = AffiliateMeOut.model_validate(affiliate)
    out.referral_count = (
        db.query(func.count(AffiliateReferral.id))
        .filter(AffiliateReferral.affiliate_id == affiliate.id)
        .scalar()
    ) or 0
    return out


def _issue_set_password_token(db: Session, affiliate: Affiliate) -> str:
    raw_token = secrets.token_urlsafe(32)
    db.add(AffiliatePasswordToken(
        affiliate_id=affiliate.id,
        token_hash=hash_api_key(raw_token),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=SET_PASSWORD_TOKEN_TTL_HOURS),
    ))
    return raw_token


@router.post("/login", response_model=TokenResponse)
@limiter.limit("5/minute")
def affiliate_login(request: Request, payload: AffiliateLoginRequest, db: Session = Depends(get_db)):
    affiliate = db.query(Affiliate).filter(Affiliate.email == payload.email).first()
    if not affiliate or not affiliate.password_hash or not verify_password(payload.password, affiliate.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if affiliate.status != "ACTIVE":
        raise HTTPException(status_code=403, detail="Dieses Partnerkonto wurde deaktiviert.")

    token = create_access_token({"sub": str(affiliate.id), "scope": "affiliate"})
    return TokenResponse(access_token=token)


@router.post("/forgot-password")
def affiliate_forgot_password(payload: AffiliateForgotPasswordRequest, db: Session = Depends(get_db)):
    """
    Always returns the same generic message, whether or not the email
    belongs to an affiliate - same reasoning as /auth/forgot-password.
    """
    affiliate = db.query(Affiliate).filter(Affiliate.email == payload.email).first()

    if affiliate:
        raw_token = _issue_set_password_token(db, affiliate)
        db.commit()

        set_password_link = f"{settings.frontend_url}/affiliate-set-password.html?token={raw_token}"
        try:
            send_affiliate_password_email(affiliate.email, set_password_link, is_initial_setup=False)
        except RuntimeError:
            pass  # don't leak email-delivery failures to the caller either

    return {"message": "Falls ein Partnerkonto mit dieser E-Mail-Adresse existiert, wurde eine E-Mail mit einem Link gesendet."}


@router.post("/set-password")
def affiliate_set_password(payload: AffiliateSetPasswordRequest, db: Session = Depends(get_db)):
    token_hash = hash_api_key(payload.token)
    token = db.query(AffiliatePasswordToken).filter(AffiliatePasswordToken.token_hash == token_hash).first()

    if not token or token.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Der Link ist ungültig oder abgelaufen. Bitte fordere einen neuen an.")

    affiliate = db.query(Affiliate).filter(Affiliate.id == token.affiliate_id).first()
    affiliate.password_hash = hash_password(payload.new_password)
    db.delete(token)  # single-use
    db.commit()

    return {"message": "Passwort erfolgreich gespeichert. Du kannst dich jetzt anmelden."}


@router.get("/me", response_model=AffiliateMeOut)
def get_affiliate_me(
    current_affiliate: Affiliate = Depends(get_current_affiliate),
    db: Session = Depends(get_db),
):
    return _with_referral_count(db, current_affiliate)


@router.get("/commissions", response_model=list[AffiliateCommissionOut])
def list_own_commissions(
    current_affiliate: Affiliate = Depends(get_current_affiliate),
    db: Session = Depends(get_db),
):
    return (
        db.query(AffiliateCommission)
        .filter(AffiliateCommission.affiliate_id == current_affiliate.id)
        .order_by(AffiliateCommission.created_at.desc())
        .all()
    )


@router.get("/payouts", response_model=list[AffiliatePayoutOut])
def list_own_payouts(
    current_affiliate: Affiliate = Depends(get_current_affiliate),
    db: Session = Depends(get_db),
):
    return (
        db.query(AffiliatePayout)
        .filter(AffiliatePayout.affiliate_id == current_affiliate.id)
        .order_by(AffiliatePayout.created_at.desc())
        .all()
    )
