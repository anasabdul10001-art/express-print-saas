import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ShopifyAuthorizeUrlOut(BaseModel):
    authorize_url: str


class ShopifyAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str
    shop_domain: str
    shop_name: str | None = None
    connected_at: datetime | None = None
    last_sync_at: datetime | None = None
