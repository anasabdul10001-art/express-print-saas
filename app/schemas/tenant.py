import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

PrintMode = Literal["INSTANT", "DASHBOARD_MANUAL", "INTEGRATED"]


class PrintModeUpdate(BaseModel):
    print_mode: PrintMode


class TenantUpdate(BaseModel):
    company_name: str | None = None
    country_code: str | None = None
    default_package_weight_kg: Decimal | None = Field(None, gt=0)
    default_package_length_cm: int | None = Field(None, gt=0)
    default_package_width_cm: int | None = Field(None, gt=0)
    default_package_height_cm: int | None = Field(None, gt=0)


class TenantOut(BaseModel):
    id: uuid.UUID
    company_name: str
    country_code: str
    print_mode: PrintMode
    default_package_weight_kg: Decimal
    default_package_length_cm: int
    default_package_width_cm: int
    default_package_height_cm: int
    # Billing (see app/routers/billing.py) - plan_name is computed, not
    # stored, same pattern as app/schemas/admin.py's TenantAdminOut.
    plan_id: uuid.UUID | None = None
    plan_name: str | None = None
    trial_ends_at: datetime | None = None
    subscription_status: str | None = None

    class Config:
        from_attributes = True
