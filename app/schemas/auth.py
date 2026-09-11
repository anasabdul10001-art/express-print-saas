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
