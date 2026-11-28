import logging
import uuid
from dataclasses import asdict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.crypto import decrypt_token, encrypt_token
from app.core.deps import get_current_user, get_db
from app.core.rate_limit import limiter
from app.models.internetmarke import BLOCKING_STATES, InternetmarkeAccount, InternetmarkePurchase
from app.models.order import Order
from app.models.print_agent import PrintAgent
from app.models.print_job import PrintJob
from app.models.user import User
from app.schemas.internetmarke import InternetmarkeBuy, InternetmarkeConnect, InternetmarkePageFormat
from app.services import internetmarke_products, internetmarke_service
from app.services.dhl_service import _to_alpha3
from app.services.internetmarke_service import InternetmarkeError, InternetmarkeOutcomeUnknown

router = APIRouter(prefix="/internetmarke", tags=["internetmarke"])
log = logging.getLogger("internetmarke")

TERMS_REQUIRED = "Bitte bestätige zuerst die AGB INTERNETMARKE der Deutschen Post."
# After this long a PENDING purchase is no longer "still running".
STUCK_AFTER_SECONDS = 600


def _account(db: Session, tenant_id) -> InternetmarkeAccount | None:
    return db.query(InternetmarkeAccount).filter(InternetmarkeAccount.tenant_id == tenant_id).first()


def _require_account(db: Session, tenant_id) -> InternetmarkeAccount:
    account = _account(db, tenant_id)
    if not account:
        raise HTTPException(status_code=400, detail="Bitte zuerst deine Portokasse in den Einstellungen verbinden.")
    return account


def _login(account: InternetmarkeAccount):
    return internetmarke_service.login(decrypt_token(account.username_encrypted),
                                       decrypt_token(account.password_encrypted))


def _purchase_out(p: InternetmarkePurchase) -> dict:
    return {"id": str(p.id), "order_id": str(p.order_id) if p.order_id else None, "status": p.status,
            "product_code": p.product_code, "price_cents": p.price_cents, "shop_order_id": p.shop_order_id,
            "track_id": p.track_id, "has_label": p.label_pdf is not None,
            "can_fetch_label": p.status == "BOUGHT" and p.label_pdf is None and p.label_link is not None,
            "created_at": p.created_at}


@router.get("/status")
def get_status(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Never returns the Portokasse credentials."""
    account = _account(db, current_user.tenant_id)
    return {
        "configured": internetmarke_service.is_configured(),
        "connected": account is not None,
        "page_format_id": account.page_format_id if account else None,
        "sender": None if not account else {
            "name": account.sender_name, "street": account.sender_street,
            "zip": account.sender_zip, "city": account.sender_city},
        "products": [{k: v for k, v in asdict(p).items() if k not in ("valid_from", "valid_to")}
                     for p in internetmarke_products.available()],
    }


@router.post("/connect")
@limiter.limit("5/minute")
def connect(request: Request, payload: InternetmarkeConnect,
            current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Stores the credentials only after Deutsche Post accepted them."""
    if not payload.accept_terms:
        raise HTTPException(status_code=400, detail=TERMS_REQUIRED)
    try:
        session = internetmarke_service.login(payload.username, payload.password)
    except InternetmarkeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    account = _account(db, current_user.tenant_id)
    if not account:
        account = InternetmarkeAccount(tenant_id=current_user.tenant_id)
        db.add(account)
    account.username_encrypted = encrypt_token(payload.username)
    account.password_encrypted = encrypt_token(payload.password)
    account.sender_name = payload.sender_name
    account.sender_street = payload.sender_street
    account.sender_zip = payload.sender_zip
    account.sender_city = payload.sender_city
    account.terms_accepted_at = datetime.now(timezone.utc)
    db.commit()
    return {"connected": True, "wallet_cents": session.wallet_cents}


@router.post("/disconnect")
def disconnect(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    account = _require_account(db, current_user.tenant_id)
    db.delete(account)
    db.commit()
    return {"connected": False}


@router.get("/page-formats")
@limiter.limit("10/minute")
def get_page_formats(request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    account = _require_account(db, current_user.tenant_id)
    try:
        session = _login(account)
        return {"wallet_cents": session.wallet_cents, "formats": internetmarke_service.page_formats(session)}
    except InternetmarkeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.put("/page-format")
def set_page_format(payload: InternetmarkePageFormat,
                    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    account = _require_account(db, current_user.tenant_id)
    account.page_format_id = payload.page_format_id
    db.commit()
    return {"page_format_id": account.page_format_id}


def _receiver(order: Order) -> dict:
    required = (order.recipient_name, order.recipient_street1, order.recipient_zip, order.recipient_city)
    if not all(value and value.strip() for value in required):
        raise HTTPException(status_code=400, detail="Für diese Bestellung fehlt eine vollständige Lieferadresse.")
    country = _to_alpha3(order.recipient_country_code)
    if len(country) != 3:
        raise HTTPException(status_code=400, detail="Dieses Zielland wird noch nicht unterstützt.")
    receiver = {"name": order.recipient_name.strip(), "addressLine1": order.recipient_street1.strip(),
                "postalCode": order.recipient_zip.strip(), "city": order.recipient_city.strip(), "country": country}
    if order.recipient_street2 and order.recipient_street2.strip():
        receiver["additionalName"] = order.recipient_street2.strip()
    return receiver


def _queue_print(request: Request, db: Session, purchase: InternetmarkePurchase) -> str | None:
    """Sends a bought label to the printer. Without an agent it stays available as a download."""
    agent = (db.query(PrintAgent)
             .filter(PrintAgent.tenant_id == purchase.tenant_id, PrintAgent.status != "disabled")
             .order_by(PrintAgent.last_seen_at.desc().nullslast()).first())
    order = db.get(Order, purchase.order_id) if purchase.order_id else None
    if not agent or not order:
        return None
    job_id = uuid.uuid4()
    db.add(PrintJob(id=job_id, tenant_id=purchase.tenant_id, print_agent_id=agent.id, order_id=order.id,
                    label_pdf_url=f"{str(request.base_url).rstrip('/')}/print-jobs/{job_id}/label",
                    label_pdf_data=purchase.label_pdf, rotation_degrees=0, label_format="4x6", status="pending"))
    order.status = "ready_to_print"
    db.commit()
    return str(job_id)


def _fetch_label(request: Request, db: Session, purchase: InternetmarkePurchase) -> dict:
    """The stamp is paid at this point, so a failed download must never look like a failed purchase."""
    try:
        purchase.label_pdf = internetmarke_service.download_label(purchase.label_link)
    except InternetmarkeError as exc:
        log.warning("label download failed for purchase %s", purchase.id, exc_info=True)
        return {**_purchase_out(purchase), "print_job_id": None,
                "message": f"Die Marke ist bezahlt. {exc} Bitte \"Etikett erneut laden\" - nicht neu kaufen."}
    db.commit()
    job_id = _queue_print(request, db, purchase)
    return {**_purchase_out(purchase), "print_job_id": job_id,
            "message": "Marke gekauft und an den Drucker gesendet." if job_id
                       else "Marke gekauft. Kein aktiver Drucker - bitte das PDF herunterladen."}


@router.post("/orders/{order_id}/buy", status_code=201)
@limiter.limit("20/minute")
def buy(request: Request, order_id: uuid.UUID, payload: InternetmarkeBuy,
        current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not payload.accept_terms:
        raise HTTPException(status_code=400, detail=TERMS_REQUIRED)
    account = _require_account(db, current_user.tenant_id)
    if account.page_format_id is None:
        raise HTTPException(status_code=400, detail="Bitte zuerst in den Einstellungen ein Druckformat wählen.")
    order = db.query(Order).filter(Order.id == order_id, Order.tenant_id == current_user.tenant_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status == "canceled":
        raise HTTPException(status_code=400, detail="Diese Bestellung wurde storniert.")
    product = next((p for p in internetmarke_products.available() if p.code == payload.product_code), None)
    if not product:
        raise HTTPException(status_code=400, detail="Dieses Porto-Produkt ist nicht verfügbar.")
    if product.price_cents != payload.price_cents:
        raise HTTPException(status_code=409, detail="Der Preis hat sich geändert. Bitte die Seite neu laden.")
    if product.international != ((order.recipient_country_code or "DE").upper() != "DE"):
        raise HTTPException(status_code=400, detail="Dieses Porto passt nicht zum Zielland (Inland/Ausland).")
    receiver = _receiver(order)
    if db.query(InternetmarkePurchase.id).filter(
            InternetmarkePurchase.order_id == order.id,
            InternetmarkePurchase.status.in_(BLOCKING_STATES)).first():
        raise HTTPException(status_code=409, detail="Für diese Bestellung gibt es bereits einen Kauf.")
    sender = {"name": account.sender_name, "addressLine1": account.sender_street,
              "postalCode": account.sender_zip, "city": account.sender_city, "country": "DEU"}

    try:
        session = _login(account)
        if session.wallet_cents is not None and session.wallet_cents < product.price_cents:
            raise InternetmarkeError("Das Guthaben deiner Portokasse reicht nicht aus.")
        shop_order_id = internetmarke_service.create_cart(session)
    except InternetmarkeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    purchase = InternetmarkePurchase(
        tenant_id=current_user.tenant_id, order_id=order.id, shop_order_id=shop_order_id,
        product_code=product.code, price_cents=product.price_cents, status="PENDING",
        terms_accepted_at=datetime.now(timezone.utc))
    db.add(purchase)
    try:
        db.commit()  # on record before any money moves; a parallel click fails here
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Für diese Bestellung läuft bereits ein Kauf.")

    try:
        result = internetmarke_service.checkout(session, shop_order_id, product.code, product.price_cents,
                                                account.page_format_id, sender, receiver)
    except InternetmarkeOutcomeUnknown as exc:
        purchase.status, purchase.error = "UNKNOWN", str(exc)
        db.commit()
        raise HTTPException(status_code=502, detail=(
            f"{exc} Es ist unklar, ob bezahlt wurde. Bitte NICHT erneut kaufen, sondern in der Portokasse unter "
            f"Buchungen nach der Bestellnummer {shop_order_id} sehen."))
    except InternetmarkeError as exc:
        purchase.status, purchase.error = "FAILED", str(exc)
        db.commit()
        raise HTTPException(status_code=502, detail=str(exc))

    purchase.status = "BOUGHT"
    purchase.label_link = result.label_link
    purchase.voucher_id = result.voucher_id
    purchase.track_id = result.track_id
    if result.track_id and not order.tracking_number:
        order.tracking_number = result.track_id[:50]
    db.commit()
    return {**_fetch_label(request, db, purchase), "wallet_cents": result.wallet_cents}


@router.get("/purchases")
def list_purchases(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = (db.query(InternetmarkePurchase).filter(InternetmarkePurchase.tenant_id == current_user.tenant_id)
            .order_by(InternetmarkePurchase.created_at.desc()).limit(200).all())
    return [_purchase_out(p) for p in rows]


def _own_purchase(db: Session, tenant_id, purchase_id) -> InternetmarkePurchase:
    purchase = db.query(InternetmarkePurchase).filter(
        InternetmarkePurchase.id == purchase_id, InternetmarkePurchase.tenant_id == tenant_id).first()
    if not purchase:
        raise HTTPException(status_code=404, detail="Kauf nicht gefunden.")
    return purchase


@router.post("/purchases/{purchase_id}/fetch-label")
@limiter.limit("10/minute")
def fetch_label_again(request: Request, purchase_id: uuid.UUID,
                      current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    purchase = _own_purchase(db, current_user.tenant_id, purchase_id)
    if purchase.status != "BOUGHT" or purchase.label_pdf is not None or not purchase.label_link:
        raise HTTPException(status_code=400, detail="Für diesen Kauf gibt es nichts nachzuladen.")
    return _fetch_label(request, db, purchase)


@router.post("/purchases/{purchase_id}/not-charged")
def confirm_not_charged(purchase_id: uuid.UUID,
                        current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """The seller checked the Portokasse and nothing was booked: the order may be bought again."""
    purchase = _own_purchase(db, current_user.tenant_id, purchase_id)
    age = datetime.now(timezone.utc) - purchase.created_at
    stuck = purchase.status == "PENDING" and age.total_seconds() > STUCK_AFTER_SECONDS
    if purchase.status != "UNKNOWN" and not stuck:
        raise HTTPException(status_code=400, detail="Nur ein unklarer Kauf kann freigegeben werden.")
    purchase.status = "FAILED"
    purchase.error = "Vom Verkäufer als nicht abgebucht bestätigt."
    db.commit()
    return _purchase_out(purchase)


@router.get("/purchases/{purchase_id}/label")
def get_label(purchase_id: uuid.UUID, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    purchase = _own_purchase(db, current_user.tenant_id, purchase_id)
    if purchase.label_pdf is None:
        raise HTTPException(status_code=404, detail="Label not found")
    return Response(content=purchase.label_pdf, media_type="application/pdf", headers={
        "Content-Disposition": 'attachment; filename="internetmarke.pdf"', "Cache-Control": "no-store"})
