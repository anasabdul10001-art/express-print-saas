"""Supplier invoices: preview the new average costs, book, download, take back."""

import json
import uuid
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core.errors import readable
from app.core.deps import get_current_user, get_db
from app.core.rate_limit import limiter
from app.models.product import Product
from app.models.purchase import Purchase, PurchaseLine
from app.models.stock_movement import StockMovement
from app.models.supplier_item import SupplierItem
from app.models.user import User
from app.services import document_files, ebay_quantity_push, invoice_ai_service, purchase_service, supplier_item_service

router = APIRouter(prefix="/purchases", tags=["purchases"])

MAX_FILE_BYTES = document_files.MAX_FILE_BYTES
ZERO = Decimal("0")


class LineIn(BaseModel):
    product_id: uuid.UUID
    quantity: int = Field(..., ge=1, le=100000)
    # Goods price of the whole line in EUR (without shipping), as really paid.
    line_total: Decimal = Field(..., ge=0, le=1000000, decimal_places=2)


class SupplierItemIn(BaseModel):
    """What this invoice calls one of the products, to recognise it next time."""
    name: str = Field("", max_length=500)
    code: str = Field("", max_length=100)
    product_id: uuid.UUID
    units_per_pack: int = Field(1, ge=1, le=10000)


class MatchItemIn(BaseModel):
    name: str = Field("", max_length=500)
    code: str = Field("", max_length=100)


class MatchIn(BaseModel):
    supplier: str = Field("", max_length=255)
    items: list[MatchItemIn] = Field(..., min_length=1, max_length=100)


class PurchaseIn(BaseModel):
    supplier: str = Field(..., min_length=1, max_length=255)
    invoice_number: str = Field("", max_length=100)
    invoice_date: date
    paid_date: date | None = None
    extra_costs: Decimal = Field(ZERO, ge=0, le=1000000, decimal_places=2)
    add_to_stock: bool = True
    # The grand total on the invoice, if the seller types it in: it must match the lines.
    expected_total: Decimal | None = Field(None, ge=0, decimal_places=2)
    lines: list[LineIn] = Field(..., min_length=1, max_length=100)
    supplier_items: list[SupplierItemIn] = Field(default_factory=list, max_length=300)

    @model_validator(mode="after")
    def checks(self):
        if len({line.product_id for line in self.lines}) != len(self.lines):
            raise ValueError("Jedes Produkt darf nur in einer Zeile stehen.")
        if {item.product_id for item in self.supplier_items} - {line.product_id for line in self.lines}:
            raise ValueError("Eine Zuordnung gehört zu keinem Produkt dieser Rechnung.")
        total = sum((line.line_total for line in self.lines), ZERO) + self.extra_costs
        if self.expected_total is not None and self.expected_total != total:
            raise ValueError(f"Die Zeilen und Nebenkosten ergeben {total} €, auf der Rechnung steht {self.expected_total} €.")
        return self


def _plan(db: Session, tenant_id, data: PurchaseIn, lock: bool) -> list[tuple]:
    """(product, line, computed values) per line. Changes nothing."""
    pieces = sum(line.quantity for line in data.lines)
    unit_extra = data.extra_costs / pieces
    plan = []
    for line in data.lines:
        query = db.query(Product).filter(Product.id == line.product_id, Product.tenant_id == tenant_id)
        product = (query.with_for_update() if lock else query).first()
        if not product:
            raise HTTPException(status_code=404, detail="Produkt nicht gefunden.")
        before = purchase_service.old_pieces(product.stock_quantity, line.quantity, data.add_to_stock)
        # Shipping counts as 0 once the purchase price is known; with no known price there is nothing to average.
        old_shipping = None if product.purchase_price is None else (product.supplier_shipping_cost or ZERO)
        values = {
            "stock_before": before,
            "old_purchase_price": product.purchase_price,
            "old_shipping_cost": product.supplier_shipping_cost,
            "new_purchase_price": purchase_service.average(
                before, product.purchase_price, line.quantity, line.line_total / line.quantity),
            "new_shipping_cost": purchase_service.average(before, old_shipping, line.quantity, unit_extra),
            "unit_extra": unit_extra.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
        }
        plan.append((product, line, values))
    return plan


def _text(value):
    return None if value is None else str(value)


def _line_preview(product, line, v) -> dict:
    return {
        "product_id": str(product.id), "title": product.title, "quantity": line.quantity,
        "unit_price": str((line.line_total / line.quantity).quantize(Decimal("0.0001"))),
        "unit_extra": str(v["unit_extra"]), "stock_before": v["stock_before"],
        "old_purchase_price": _text(v["old_purchase_price"]), "old_shipping_cost": _text(v["old_shipping_cost"]),
        "new_purchase_price": str(v["new_purchase_price"]), "new_shipping_cost": str(v["new_shipping_cost"]),
    }


def _out(purchase: Purchase) -> dict:
    return {
        "id": str(purchase.id), "supplier": purchase.supplier, "invoice_number": purchase.invoice_number,
        "invoice_date": purchase.invoice_date.isoformat(),
        "paid_date": purchase.paid_date.isoformat() if purchase.paid_date else None,
        "extra_costs": str(purchase.extra_costs), "total": str(purchase.total),
        "added_to_stock": purchase.added_to_stock, "file_name": purchase.file_name,
        "cancelled": purchase.cancelled_at is not None,
        "lines": [{
            "title": l.title, "quantity": l.quantity, "line_total": str(l.line_total),
            "old_purchase_price": _text(l.old_purchase_price), "new_purchase_price": str(l.new_purchase_price),
            "new_shipping_cost": str(l.new_shipping_cost),
        } for l in purchase.lines],
    }


def _own(db: Session, tenant_id, purchase_id) -> Purchase:
    purchase = db.query(Purchase).filter(Purchase.id == purchase_id, Purchase.tenant_id == tenant_id).first()
    if not purchase:
        raise HTTPException(status_code=404, detail="Einkauf nicht gefunden.")
    return purchase


@router.get("/ai-status")
def ai_status(current_user: User = Depends(get_current_user)):
    return {"providers": [p["name"] for p in invoice_ai_service.providers()]}


@router.post("/read")
@limiter.limit("6/minute")
def read_with_ai(
    request: Request,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Suggests the form's content from the invoice file. Saves and changes nothing."""
    content = file.file.read(MAX_FILE_BYTES + 1)
    content_type = document_files.file_type(content)
    products = (db.query(Product.id, Product.title).filter(Product.tenant_id == current_user.tenant_id)
                .order_by(Product.title).limit(300).all())
    db.close()  # reading takes a while; no database connection is held during it
    try:
        result = invoice_ai_service.read_invoice(content, content_type, [p.title for p in products])
    except invoice_ai_service.InvoiceReadError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    for line in result["lines"]:
        number = line.pop("product_number")
        line["product_id"] = str(products[number - 1].id) if number else None
        line["line_total"] = str(line["line_total"])
    for key in ("extra_costs", "tax_amount", "total"):
        result[key] = _text(result[key])
    return result


@router.post("/match")
def match_items(payload: MatchIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Which product each invoice item is: remembered, otherwise suggestions. Saves nothing."""
    return supplier_item_service.match(db, current_user.tenant_id, payload.supplier,
                                       [(item.name, item.code) for item in payload.items])


@router.get("/supplier-items")
def list_supplier_items(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = (db.query(SupplierItem, Product.title).join(Product, Product.id == SupplierItem.product_id)
            .filter(SupplierItem.tenant_id == current_user.tenant_id)
            .order_by(SupplierItem.supplier_norm, SupplierItem.item_name))
    return [{"id": str(item.id), "supplier": item.supplier, "name": item.item_name, "code": item.item_code,
             "units_per_pack": item.units_per_pack, "product": title} for item, title in rows]


@router.delete("/supplier-items/{item_id}", status_code=204)
def delete_supplier_item(item_id: uuid.UUID, current_user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    item = db.query(SupplierItem).filter(SupplierItem.id == item_id,
                                         SupplierItem.tenant_id == current_user.tenant_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Zuordnung nicht gefunden.")
    db.delete(item)
    db.commit()


@router.post("/preview")
def preview(payload: PurchaseIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    plan = _plan(db, current_user.tenant_id, payload, lock=False)
    total = sum((l.line_total for l in payload.lines), ZERO) + payload.extra_costs
    return {"total": str(total), "lines": [_line_preview(*entry) for entry in plan]}


@router.post("", status_code=201)
async def book(
    data: str = Form(...),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        payload = PurchaseIn.model_validate(json.loads(data))
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=readable([{**e, "loc": ("body", *e["loc"])} for e in exc.errors()]))
    except ValueError:
        raise HTTPException(status_code=422, detail="Ungültige Eingabe.")
    content = await file.read(MAX_FILE_BYTES + 1)
    content_type = document_files.file_type(content)

    tenant_id = current_user.tenant_id
    number = payload.invoice_number.strip()
    plan = _plan(db, tenant_id, payload, lock=True)
    if number and db.query(Purchase.id).filter(
            Purchase.tenant_id == tenant_id, Purchase.cancelled_at.is_(None),
            Purchase.supplier == payload.supplier.strip(), Purchase.invoice_number == number).first():
        db.rollback()
        raise HTTPException(status_code=409, detail="Diese Rechnung (Lieferant und Nummer) ist schon gebucht.")

    purchase = Purchase(
        tenant_id=tenant_id, supplier=payload.supplier.strip(), invoice_number=number,
        invoice_date=payload.invoice_date, paid_date=payload.paid_date, extra_costs=payload.extra_costs,
        total=sum((l.line_total for l in payload.lines), ZERO) + payload.extra_costs,
        added_to_stock=payload.add_to_stock,
        file_name=(file.filename or "rechnung")[-255:], content_type=content_type, file_data=content,
    )
    for position, (product, line, v) in enumerate(plan):
        purchase.lines.append(PurchaseLine(
            product_id=product.id, position=position, title=product.title, quantity=line.quantity,
            line_total=line.line_total, **v))
        product.purchase_price = v["new_purchase_price"]
        product.supplier_shipping_cost = v["new_shipping_cost"]
        if payload.add_to_stock:
            product.stock_quantity += line.quantity
            db.add(StockMovement(tenant_id=tenant_id, product_id=product.id, quantity_change=line.quantity,
                                 reason="restock", reference=(number or purchase.supplier)[:255]))
    db.add(purchase)
    # only a booked invoice teaches a link: every product was checked against the account above
    supplier_item_service.remember(db, tenant_id, payload.supplier, payload.supplier_items)
    came_in = [ebay_quantity_push.add(product.id, line.quantity)
               for product, line, _ in plan] if payload.add_to_stock else []
    db.commit()
    sent = await run_in_threadpool(ebay_quantity_push.send, db, tenant_id, came_in)
    db.refresh(purchase)
    return {**_out(purchase), "ebay_quantity_results": sent}


@router.get("")
def list_purchases(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = (db.query(Purchase).filter(Purchase.tenant_id == current_user.tenant_id)
            .order_by(Purchase.invoice_date.desc(), Purchase.created_at.desc()).all())
    return [_out(p) for p in rows]


@router.get("/{purchase_id}/file")
def download(purchase_id: uuid.UUID, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    purchase = _own(db, current_user.tenant_id, purchase_id)
    return document_files.download(purchase.file_name, purchase.content_type, purchase.file_data)


@router.post("/{purchase_id}/cancel")
def cancel(purchase_id: uuid.UUID, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Takes a wrong booking back. Only possible while the products' costs are still the ones
    this booking set; the invoice and its file stay on record, marked as cancelled."""
    purchase = _own(db, current_user.tenant_id, purchase_id)
    db.refresh(purchase, with_for_update=True)
    if purchase.cancelled_at is not None:
        raise HTTPException(status_code=409, detail="Dieser Einkauf ist bereits zurückgenommen.")
    changes = []
    for line in purchase.lines:
        product = (db.query(Product).filter(Product.id == line.product_id).with_for_update().first()
                   if line.product_id else None)
        if product is None:
            continue
        if (product.purchase_price != line.new_purchase_price
                or (product.supplier_shipping_cost or ZERO) != line.new_shipping_cost):
            db.rollback()
            raise HTTPException(status_code=409, detail=f"Die Kosten von „{line.title}“ wurden danach geändert. "
                                "Bitte zuerst den neueren Einkauf zurücknehmen.")
        changes.append((product, line))
    taken_out = []
    for product, line in changes:
        product.purchase_price = line.old_purchase_price
        product.supplier_shipping_cost = line.old_shipping_cost
        if purchase.added_to_stock:
            removed = min(line.quantity, product.stock_quantity)
            product.stock_quantity -= removed
            # the whole line: eBay's own number decides what is left to take away
            taken_out.append(ebay_quantity_push.add(product.id, -line.quantity))
            db.add(StockMovement(tenant_id=purchase.tenant_id, product_id=product.id, quantity_change=-removed,
                                 reason="manual_correction", reference=f"Einkauf zurückgenommen {purchase.invoice_number}"[:255]))
    purchase.cancelled_at = datetime.now(timezone.utc)
    db.commit()
    sent = ebay_quantity_push.send(db, current_user.tenant_id, taken_out)
    return {**_out(purchase), "ebay_quantity_results": sent}
