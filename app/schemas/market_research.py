import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class MarketResearchItem(BaseModel):
    title: str | None
    price: str | None
    currency: str | None
    condition: str | None
    image_url: str | None
    item_web_url: str | None
    seller_username: str | None
    seller_feedback_percentage: str | None
    seller_feedback_score: int | None
    estimated_available_quantity: int | None
    estimated_sold_quantity: int | None


class TrackedSearchCreate(BaseModel):
    query: str = Field(..., min_length=1, max_length=255)


class TrackedSearchOut(BaseModel):
    id: uuid.UUID
    query: str
    created_at: datetime

    class Config:
        from_attributes = True


class MarketSnapshotOut(BaseModel):
    snapshot_date: date
    item_count: int
    total_estimated_sold: int | None
    average_price: Decimal | None

    class Config:
        from_attributes = True
