import uuid

from pydantic import BaseModel


class CheckoutSessionRequest(BaseModel):
    plan_id: uuid.UUID


class CheckoutSessionOut(BaseModel):
    url: str


class PortalSessionOut(BaseModel):
    url: str
