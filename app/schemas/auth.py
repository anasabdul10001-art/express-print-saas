import uuid

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    company_name: str = Field(..., min_length=1, max_length=255)
    email: EmailStr
    password: str = Field(..., min_length=8)
    full_name: str | None = None
    # Which pricing-page plan the signup came from, e.g. "Pro". Optional -
    # a direct registration with no plan selected falls back to a default
    # plan in the register endpoint.
    plan_name: str | None = None
    # An affiliate's referral_code, e.g. from a ?ref=CODE link (see
    # register.html and app/models/affiliate.py). Optional - a bad or
    # unknown code is silently ignored rather than blocking signup, same
    # spirit as plan_name above.
    referral_code: str | None = None
    # Explicit confirmation, captured at registration, that the customer wants
    # the service to start immediately and understands they lose the statutory
    # 14-day withdrawal right once it's fully performed (§ 356 Abs. 4 BGB).
    # Required because ShipSync grants full account access right away - see
    # app/routers/auth.py's register().
    early_service_consent: bool = Field(
        ...,
        description="Must be true: confirms the customer wants immediate access and accepts losing the statutory withdrawal right upon full performance.",
    )


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(..., min_length=8)


class UserMeOut(BaseModel):
    id: uuid.UUID
    email: EmailStr
    full_name: str | None
    avatar_url: str | None
    tenant_id: uuid.UUID
    role: str
    is_superadmin: bool

    class Config:
        from_attributes = True


class UpdateEmailRequest(BaseModel):
    email: EmailStr
    current_password: str = Field(..., description="Confirms it's really you before changing your login email")


class UpdateProfileRequest(BaseModel):
    full_name: str | None = None


class UpdatePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8)
