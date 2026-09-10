from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db
from app.models.expense import Expense
from app.models.user import User
from app.schemas.expense import ExpenseCreate, ExpenseOut, ExpenseSummaryOut

router = APIRouter(prefix="/expenses", tags=["expenses"])


@router.get("", response_model=list[ExpenseOut])
def list_expenses(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return (
        db.query(Expense)
        .filter(Expense.tenant_id == current_user.tenant_id)
        .order_by(Expense.expense_date.desc(), Expense.created_at.desc())
        .all()
    )


@router.get("/summary", response_model=ExpenseSummaryOut)
def expense_summary(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """
    Same UTC-calendar-month boundary used by /orders/summary, so the two
    numbers the frontend combines (gross profit, expenses) always refer to
    the exact same window.
    """
    now = datetime.now(timezone.utc)
    start_of_month = now.replace(day=1).date()

    total = (
        db.query(func.sum(Expense.amount))
        .filter(Expense.tenant_id == current_user.tenant_id, Expense.expense_date >= start_of_month)
        .scalar()
    ) or Decimal("0")

    return ExpenseSummaryOut(expenses_month=total)


@router.post("", response_model=ExpenseOut, status_code=201)
def create_expense(
    payload: ExpenseCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    expense = Expense(tenant_id=current_user.tenant_id, **payload.model_dump())
    db.add(expense)
    db.commit()
    db.refresh(expense)
    return expense


@router.delete("/{expense_id}", status_code=204)
def delete_expense(
    expense_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    expense = (
        db.query(Expense)
        .filter(Expense.id == expense_id, Expense.tenant_id == current_user.tenant_id)
        .first()
    )
    if not expense:
        raise HTTPException(status_code=404, detail="Ausgabe nicht gefunden")

    db.delete(expense)
    db.commit()
