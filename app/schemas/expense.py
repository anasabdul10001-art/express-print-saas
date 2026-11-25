import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field


class ExpenseCreate(BaseModel):
    description: str = Field(..., min_length=1, max_length=255)
    amount: Decimal = Field(..., gt=0)
    expense_date: date
    category: str | None = Field(None, max_length=50)
    supplier: str | None = Field(None, max_length=255)
    invoice_number: str | None = Field(None, max_length=100)


class ExpenseUpdate(BaseModel):
    """All fields optional - PATCH semantics, only sent fields get changed."""
    description: str | None = Field(None, min_length=1, max_length=255)
    amount: Decimal | None = Field(None, gt=0)
    expense_date: date | None = None
    category: str | None = Field(None, max_length=50)
    supplier: str | None = Field(None, max_length=255)
    invoice_number: str | None = Field(None, max_length=100)


class ReceiptOut(BaseModel):
    id: uuid.UUID
    file_name: str

    class Config:
        from_attributes = True


class ExpenseOut(BaseModel):
    id: uuid.UUID
    description: str
    amount: Decimal
    expense_date: date
    category: str | None = None
    supplier: str | None = None
    invoice_number: str | None = None
    receipts: list[ReceiptOut] = []

    class Config:
        from_attributes = True


class ExpenseSummaryOut(BaseModel):
    """
    Net profit = gross profit from orders (already computed in
    OrderSummaryOut) minus total expenses for the same period. Kept as a
    separate endpoint rather than folded into OrderSummaryOut, since orders
    and expenses are conceptually separate concerns - the frontend combines
    the two numbers itself.
    """
    expenses_month: Decimal
