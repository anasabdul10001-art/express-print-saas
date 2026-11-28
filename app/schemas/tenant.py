import math
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, computed_field

PrintMode = Literal["INSTANT", "DASHBOARD_MANUAL", "INTEGRATED"]


class PrintModeUpdate(BaseModel):
    print_mode: PrintMode


def _days_left(end: datetime | None) -> int | None:
    """Started days until `end` (a 14-day trial shows 14 right after sign-up); 0 once it has passed."""
    if end is None:
        return None
    return max(math.ceil((end - datetime.now(timezone.utc)).total_seconds() / 86400), 0)


LabelPaper = Literal["100x150", "105x148", "100x200", "original"]


class LabelPaperUpdate(BaseModel):
    label_paper: LabelPaper


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
    label_paper: LabelPaper
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
    current_period_end: datetime | None = None

    @computed_field
    @property
    def trial_days_left(self) -> int | None:
        # a paid subscription replaces the trial
        return None if self.subscription_status == "active" else _days_left(self.trial_ends_at)

    @computed_field
    @property
    def period_days_left(self) -> int | None:
        return _days_left(self.current_period_end) if self.subscription_status == "active" else None

    class Config:
        from_attributes = True
