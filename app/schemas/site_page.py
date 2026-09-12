from datetime import datetime

from pydantic import BaseModel, Field


class SitePageOut(BaseModel):
    slug: str
    title: str
    content: str
    updated_at: datetime | None

    class Config:
        from_attributes = True


class SitePageUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=255)
    content: str | None = Field(None, min_length=1)
