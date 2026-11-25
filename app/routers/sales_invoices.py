"""A seller's invoices to their buyers: seller details, issue, cancel, download."""

import json
import uuid
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db
from app.models.order import Order
from app.models.sales_invoice import SalesInvoice, SellerProfile
from app.models.tenant import Tenant
from app.models.user import User
from app.services import einvoice_service
from app.services import sales_invoice_service as service

router = APIRouter(prefix="/sales-invoices", tags=["sales-invoices"])

PROFILE_FIELDS = ("legal_name", "address", "tax_number", "vat_id", "email", "number_prefix")


class ProfileIn(BaseModel):
    legal_name: str = Field("", max_length=255)
    address: str = Field("", max_length=1000)
    tax_number: str = Field("", max_length=50)
    vat_id: str = Field("", max_length=30)
    email: str = Field("", max_length=255)
    number_prefix: str = Field("RE", max_length=10, pattern=r"^[A-Za-z0-9-]*$")

    @field_validator("legal_name", "address", "tax_number", "vat_id", "email", mode="before")
    @classmethod
    def strip(cls, value):
        return (value or "").strip()


class IssueIn(BaseModel):
    order_id: uuid.UUID
    # When the goods were delivered / shipped. Asked from the seller, never guessed (§ 14 (4) Nr. 6 UStG).
    delivery_date: date


def _locked_profile(db: Session, tenant_id) -> SellerProfile:
    """The tenant's profile row, locked until commit: issuing and cancelling are serialised,
    so two clicks can't take the same number or invoice one order twice."""
    db.execute(insert(SellerProfile).values(tenant_id=tenant_id, number_prefix="RE", last_number=0)
               .on_conflict_do_nothing())
    return db.query(SellerProfile).filter(SellerProfile.tenant_id == tenant_id).with_for_update().one()


def _own_invoice(db: Session, tenant_id, invoice_id) -> SalesInvoice:
    invoice = db.query(SalesInvoice).filter(
        SalesInvoice.id == invoice_id, SalesInvoice.tenant_id == tenant_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Rechnung nicht gefunden.")
    return invoice


def _next_number(profile: SellerProfile, today: date) -> str:
    profile.last_number += 1
    prefix = f"{profile.number_prefix}-" if profile.number_prefix else ""
    return f"{prefix}{today.year}-{profile.last_number:04d}"


def _out(invoice: SalesInvoice, cancelled_ids: set) -> dict:
    return {
        "id": str(invoice.id),
        "order_id": str(invoice.order_id) if invoice.order_id else None,
        "number": invoice.number,
        "kind": invoice.kind,
        "issue_date": invoice.issue_date.isoformat(),
        "gross_amount": str(invoice.gross_amount),
        "cancelled": invoice.id in cancelled_ids,
        "has_xml": invoice.xml is not None,
    }


@router.get("/profile")
def get_profile(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    profile = db.get(SellerProfile, current_user.tenant_id)
    result = {name: (getattr(profile, name) if profile else None) or "" for name in PROFILE_FIELDS}
    if not profile:
        result["number_prefix"] = "RE"
    return result


@router.put("/profile")
def save_profile(payload: ProfileIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    profile = _locked_profile(db, current_user.tenant_id)
    for name in PROFILE_FIELDS:
        setattr(profile, name, getattr(payload, name))
    db.commit()
    return get_profile(current_user=current_user, db=db)


@router.get("")
def list_invoices(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    invoices = (db.query(SalesInvoice).filter(SalesInvoice.tenant_id == current_user.tenant_id)
                .order_by(SalesInvoice.created_at.desc()).all())
    cancelled = {i.cancels_id for i in invoices if i.cancels_id}
    return [_out(i, cancelled) for i in invoices]


@router.post("", status_code=201)
def issue(payload: IssueIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant_id = current_user.tenant_id
    profile = _locked_profile(db, tenant_id)
    order = db.query(Order).filter(Order.id == payload.order_id, Order.tenant_id == tenant_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Bestellung nicht gefunden.")

    existing = db.query(SalesInvoice).filter(SalesInvoice.tenant_id == tenant_id,
                                             SalesInvoice.order_id == order.id).all()
    cancelled = {i.cancels_id for i in existing if i.cancels_id}
    active = next((i for i in existing if i.kind == "invoice" and i.id not in cancelled), None)
    if active:
        raise HTTPException(status_code=409, detail=f"Für diese Bestellung gibt es schon die Rechnung {active.number}.")

    tenant = db.get(Tenant, tenant_id)
    try:
        snapshot = service.build_snapshot(order, profile, tenant.vat_mode, payload.delivery_date)
    except service.InvoiceError as err:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(err))

    today = datetime.now(service.BERLIN).date()
    invoice = SalesInvoice(
        tenant_id=tenant_id, order_id=order.id, number=_next_number(profile, today), kind="invoice",
        issue_date=today, gross_amount=snapshot["gross"], data=json.dumps(snapshot, ensure_ascii=False),
    )
    invoice.pdf = service.render_pdf(snapshot, invoice.number, today)
    invoice.xml = einvoice_service.build_xml(snapshot, invoice.number, today)
    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    return _out(invoice, set())


@router.post("/{invoice_id}/cancel", status_code=201)
def cancel(invoice_id: uuid.UUID, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Issues a cancellation invoice. The original stays as it is."""
    tenant_id = current_user.tenant_id
    profile = _locked_profile(db, tenant_id)
    original = _own_invoice(db, tenant_id, invoice_id)
    if original.kind != "invoice":
        raise HTTPException(status_code=400, detail="Eine Stornorechnung kann nicht storniert werden.")
    if db.query(SalesInvoice).filter(SalesInvoice.cancels_id == original.id).first():
        raise HTTPException(status_code=409, detail="Diese Rechnung ist bereits storniert.")

    snapshot = service.storno_snapshot(json.loads(original.data), original.number, original.issue_date)
    today = datetime.now(service.BERLIN).date()
    storno = SalesInvoice(
        tenant_id=tenant_id, order_id=original.order_id, number=_next_number(profile, today), kind="storno",
        cancels_id=original.id, issue_date=today, gross_amount=snapshot["gross"],
        data=json.dumps(snapshot, ensure_ascii=False),
    )
    storno.pdf = service.render_pdf(snapshot, storno.number, today)
    storno.xml = einvoice_service.build_xml(snapshot, storno.number, today)
    db.add(storno)
    db.commit()
    db.refresh(storno)
    return _out(storno, set())


@router.get("/{invoice_id}/pdf")
def download(invoice_id: uuid.UUID, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    invoice = _own_invoice(db, current_user.tenant_id, invoice_id)
    return Response(content=invoice.pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{invoice.number}.pdf"'})


@router.get("/{invoice_id}/xml")
def download_xml(invoice_id: uuid.UUID, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    invoice = _own_invoice(db, current_user.tenant_id, invoice_id)
    if invoice.xml is None:
        raise HTTPException(status_code=404, detail="Zu dieser Rechnung gibt es keine E-Rechnung "
                            "(vollständige Käuferanschrift und Verkäuferanschrift mit PLZ und Ort nötig).")
    return Response(content=invoice.xml, media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="{invoice.number}.xml"'})

