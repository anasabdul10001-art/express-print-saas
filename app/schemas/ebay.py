import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

EbayStatus = Literal["CONNECTED", "DISCONNECTED", "ERROR"]


class AuthorizeUrlOut(BaseModel):
    authorize_url: str


class EbayAccountOut(BaseModel):
    id: uuid.UUID
    status: EbayStatus
    environment: str
    ebay_user_id: str | None = None
    connected_at: datetime | None = None
    last_sync_at: datetime | None = None

    class Config:
        from_attributes = True
