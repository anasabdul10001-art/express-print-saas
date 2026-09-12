from datetime import datetime

from pydantic import BaseModel, Field


class SitePageOut(BaseModel):
    slug: str
    title: str
    content: str
    updated_at: datetime

    class Config:
        from_attributes = True


class SitePageAdminOut(SitePageOut):
    category: str


class SitePageUpdate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1)
