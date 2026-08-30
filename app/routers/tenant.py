import uuid
from typing import Literal

from pydantic import BaseModel

PrintMode = Literal["INSTANT", "DASHBOARD_MANUAL", "INTEGRATED"]


class PrintModeUpdate(BaseModel):
    print_mode: PrintMode


class TenantUpdate(BaseModel):
    company_name: str | None = None
    country_code: str | None = None


class TenantOut(BaseModel):
    id: uuid.UUID
    company_name: str
    country_code: str
    print_mode: PrintMode

    class Config:
        from_attributes = True
