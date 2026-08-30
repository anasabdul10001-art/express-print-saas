import uuid
from typing import Literal

from pydantic import BaseModel

PrintMode = Literal["INSTANT", "DASHBOARD_MANUAL", "INTEGRATED"]


class PrintModeUpdate(BaseModel):
    print_mode: PrintMode


class TenantOut(BaseModel):
    id: uuid.UUID
    company_name: str
    print_mode: PrintMode

    class Config:
        from_attributes = True
