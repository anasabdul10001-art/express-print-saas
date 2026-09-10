from pydantic import BaseModel


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
