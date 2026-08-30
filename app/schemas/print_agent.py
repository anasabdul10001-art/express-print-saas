import uuid

from pydantic import BaseModel


class PrintAgentCreate(BaseModel):
    name: str


class PrintAgentCreatedResponse(BaseModel):
    id: uuid.UUID
    name: str
    api_key: str  # shown once - the raw, unhashed key. Save it immediately.


class PrintAgentOut(BaseModel):
    id: uuid.UUID
    name: str
    status: str

    class Config:
        from_attributes = True
