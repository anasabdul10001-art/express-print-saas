import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

CommissionType = Literal["PERCENTAGE_RECURRING", "FLAT_ONE_TIME"]
AffiliateStatus = Literal["ACTIVE", "DISABLED"]


class AffiliateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    email: EmailStr | None = None
    # Optional: set when this affiliate is also an existing ShipSync tenant.
    tenant_id: uuid.UUID | None = None
    commission_type: CommissionType
    commission_value: Decimal = Field(..., gt=0)


class AffiliateUpdate(BaseModel):
    """PATCH semantics - only sent fields get changed."""
    name: str | None = Field(None, min_length=1, max_length=255)
    email: EmailStr | None = None
    commission_type: CommissionType | None = None
    commission_value: Decimal | None = Field(None, gt=0)
    status: AffiliateStatus | None = None


class AffiliateOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID | None
    name: str
    email: str | None
    referral_code: str
    commission_type: CommissionType
    commission_value: Decimal
    balance_owed: Decimal
    total_earned: Decimal
    status: AffiliateStatus
    created_at: datetime
    # Computed, not stored - see app/routers/admin.py.
    referral_count: int = 0

    class Config:
        from_attributes = True


class AffiliateCommissionOut(BaseModel):
    id: uuid.UUID
    referral_id: uuid.UUID
    invoice_id: uuid.UUID | None
    amount: Decimal
    commission_type: CommissionType
    description: str | None
    created_at: datetime

    class Config:
        from_attributes = True


class AffiliatePayoutCreate(BaseModel):
    amount: Decimal = Field(..., gt=0)
    note: str | None = Field(None, max_length=500)


class AffiliatePayoutOut(BaseModel):
    id: uuid.UUID
    amount: Decimal
    note: str | None
    created_at: datetime

    class Config:
        from_attributes = True


# --- Self-service affiliate portal (see app/routers/affiliate_portal.py) ---

class AffiliateLoginRequest(BaseModel):
    email: EmailStr
    password: str


class AffiliateForgotPasswordRequest(BaseModel):
    email: EmailStr


class AffiliateSetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(..., min_length=8)


class AffiliateMeOut(BaseModel):
    id: uuid.UUID
    name: str
    email: str | None
    referral_code: str
    commission_type: CommissionType
    commission_value: Decimal
    balance_owed: Decimal
    total_earned: Decimal
    status: AffiliateStatus
    created_at: datetime
    referral_count: int = 0

    class Config:
        from_attributes = True
