import uuid

from pydantic import BaseModel, HttpUrl


class TestPrintJobCreate(BaseModel):
    print_agent_id: uuid.UUID
    label_pdf_url: HttpUrl
    rotation_degrees: int = 90
    label_format: str = "4x6"


class PrintJobStatusUpdate(BaseModel):
    status: str  # "printed" or "failed"
    error_message: str | None = None
