from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

DhlStatus = Literal["CONNECTED", "DISCONNECTED", "ERROR"]


class DhlAccountConnect(BaseModel):
    billing_number: str = Field(..., min_length=1, max_length=20)
    api_username: str = Field(..., min_length=1)
    api_password: str = Field(..., min_length=1)
    sender_name: str = Field(..., min_length=1, max_length=255)
    sender_street: str = Field(..., min_length=1, max_length=255)
    sender_zip: str = Field(..., min_length=1, max_length=20)
    sender_city: str = Field(..., min_length=1, max_length=100)
    sender_country: str = Field("DE", min_length=2, max_length=2)


class DhlAccountOut(BaseModel):
    # Never echoes api_username/api_password back - those stay write-only.
    billing_number: str | None
    sender_name: str | None
    sender_street: str | None
    sender_zip: str | None
    sender_city: str | None
    sender_country: str
    status: DhlStatus
    environment: str
    connected_at: datetime | None

    class Config:
        from_attributes = True
