import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AmazonAuthorizeUrlOut(BaseModel):
    authorize_url: str


class AmazonAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str
    seller_id: str
    marketplace_id: str
    connected_at: datetime | None = None
    last_sync_at: datetime | None = None
