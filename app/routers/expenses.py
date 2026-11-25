import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db
from app.models.expense import Expense, ExpenseReceipt
from app.models.user import User
from app.schemas.expense import ExpenseCreate, ExpenseOut, ExpenseSummaryOut, ExpenseUpdate, ReceiptOut
from app.services import document_files

router = APIRouter(prefix="/expenses", tags=["expenses"])

MAX_RECEIPTS = 5


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


@router.patch("/{expense_id}", response_model=ExpenseOut)
def update_expense(
    expense_id: str,
    payload: ExpenseUpdate,
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

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(expense, field, value)

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


def _receipt(db: Session, tenant_id, expense_id, receipt_id) -> ExpenseReceipt:
    receipt = db.query(ExpenseReceipt).filter(
        ExpenseReceipt.id == receipt_id, ExpenseReceipt.expense_id == expense_id,
        ExpenseReceipt.tenant_id == tenant_id).first()
    if not receipt:
        raise HTTPException(status_code=404, detail="Beleg nicht gefunden")
    return receipt


@router.post("/{expense_id}/receipts", response_model=ReceiptOut, status_code=201)
def add_receipt(
    expense_id: uuid.UUID,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # the row lock keeps two uploads at once from passing the limit together
    expense = (db.query(Expense)
               .filter(Expense.id == expense_id, Expense.tenant_id == current_user.tenant_id)
               .with_for_update().first())
    if not expense:
        raise HTTPException(status_code=404, detail="Ausgabe nicht gefunden")
    if db.query(ExpenseReceipt.id).filter(ExpenseReceipt.expense_id == expense_id).count() >= MAX_RECEIPTS:
        raise HTTPException(status_code=400, detail=f"Höchstens {MAX_RECEIPTS} Belege je Ausgabe.")
    content = file.file.read(document_files.MAX_FILE_BYTES + 1)
    receipt = ExpenseReceipt(
        tenant_id=current_user.tenant_id, expense_id=expense_id,
        file_name=(file.filename or "beleg")[-255:], content_type=document_files.file_type(content),
        file_data=content)
    db.add(receipt)
    db.commit()
    db.refresh(receipt)
    return receipt


@router.get("/{expense_id}/receipts/{receipt_id}")
def download_receipt(
    expense_id: uuid.UUID,
    receipt_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    receipt = _receipt(db, current_user.tenant_id, expense_id, receipt_id)
    return document_files.download(receipt.file_name, receipt.content_type, receipt.file_data)


@router.delete("/{expense_id}/receipts/{receipt_id}", status_code=204)
def delete_receipt(
    expense_id: uuid.UUID,
    receipt_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db.delete(_receipt(db, current_user.tenant_id, expense_id, receipt_id))
    db.commit()
