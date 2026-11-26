import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.config import settings
from app.core.crypto import encrypt_token
from app.core.deps import get_current_user, get_db
from app.core.rate_limit import limiter
from app.models.dhl_account import DhlAccount
from app.models.print_agent import PrintAgent
from app.models.print_job import PrintJob
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.dhl import DhlAccountConnect, DhlAccountOut
from app.services import dhl_service, dhl_test_service, label_paper_service

router = APIRouter(prefix="/dhl", tags=["dhl"])


@router.get("/status", response_model=DhlAccountOut | None)
def get_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Only returns the account if it's actually CONNECTED - same reasoning as
    GET /ebay/status: a DISCONNECTED row still exists after disconnect()
    (credentials cleared, not the row itself).
    """
    return (
        db.query(DhlAccount)
        .filter(DhlAccount.tenant_id == current_user.tenant_id, DhlAccount.status == "CONNECTED")
        .first()
    )


@router.post("/connect", response_model=DhlAccountOut)
def connect_dhl(
    payload: DhlAccountConnect,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Saves this tenant's own DHL Business Customer credentials - there's no
    OAuth consent screen for this API (unlike eBay), so the tenant types
    their DHL login in directly and it's encrypted at rest exactly like
    eBay's tokens are.

    Deliberately does NOT make a live call to DHL to verify these
    credentials yet - the exact request/response shape of the Parcel DE
    Shipping API needs confirming against a real sandbox call first, once
    a real DHL developer-portal API key is available (label purchase itself
    will live in a future app/services/dhl_service.py, wired into
    print_jobs.py in place of today's placeholder label URL). Wrong
    credentials will surface clearly the first time a label is actually
    purchased, rather than a "connection test" here giving false confidence
    about an unverified endpoint.
    """
    account = db.query(DhlAccount).filter(DhlAccount.tenant_id == current_user.tenant_id).first()
    if not account:
        account = DhlAccount(tenant_id=current_user.tenant_id)
        db.add(account)

    account.billing_number = payload.billing_number
    account.api_username_encrypted = encrypt_token(payload.api_username)
    account.api_password_encrypted = encrypt_token(payload.api_password)
    account.sender_name = payload.sender_name
    account.sender_street = payload.sender_street
    account.sender_zip = payload.sender_zip
    account.sender_city = payload.sender_city
    account.sender_country = payload.sender_country.upper()
    account.status = "CONNECTED"
    account.environment = settings.dhl_environment
    account.connected_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(account)
    return account


MAX_LABEL_BYTES = 2 * 1024 * 1024


def _test_account(db: Session, tenant_id) -> DhlAccount:
    account = db.query(DhlAccount).filter(DhlAccount.tenant_id == tenant_id, DhlAccount.status == "CONNECTED").first()
    if not account:
        raise HTTPException(status_code=400, detail="Bitte zuerst das DHL-Testkonto verbinden.")
    if account.environment != "SANDBOX":
        raise HTTPException(status_code=400, detail="Das Testetikett gibt es nur in der DHL-Testumgebung. "
                            "Dieses Konto ist für echte, kostenpflichtige Etiketten verbunden.")
    return account


@router.post("/test-label")
@limiter.limit("6/minute")
def test_label(request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Asks DHL's test environment for a label to a made-up address. No order is read or changed."""
    account = _test_account(db, current_user.tenant_id)
    tenant = db.query(Tenant).filter(Tenant.id == current_user.tenant_id).one()
    try:
        result = dhl_service.create_shipment_label(
            account, dhl_test_service.sample_order(), tenant.default_package_weight_kg,
            tenant.default_package_length_cm, tenant.default_package_width_cm, tenant.default_package_height_cm)
    except dhl_service.DhlShipmentError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    try:
        fitted, _ = label_paper_service.fit_label(result.label_pdf, tenant.label_paper)
        pdf = dhl_test_service.stamp(fitted)
    except Exception:  # never hand out a label that could not be marked as a test
        raise HTTPException(status_code=502, detail="DHL hat geantwortet, aber das Etikett ist keine lesbare PDF-Datei.")
    return Response(content=pdf, media_type="application/pdf", headers={
        "Content-Disposition": 'attachment; filename="dhl-testetikett.pdf"', "Cache-Control": "no-store"})


@router.post("/test-label/print", status_code=201)
async def print_test_label(request: Request, file: UploadFile = File(...),
                           current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Sends the test label the seller just looked at to their printer (stamped again, tied to no order)."""
    _test_account(db, current_user.tenant_id)
    content = await file.read(MAX_LABEL_BYTES + 1)
    if len(content) > MAX_LABEL_BYTES or not content.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="Bitte das erstellte Testetikett (PDF) senden.")
    try:
        content = dhl_test_service.stamp(content)
    except Exception:
        raise HTTPException(status_code=400, detail="Die PDF-Datei ist nicht lesbar.")
    agent = (db.query(PrintAgent)
             .filter(PrintAgent.tenant_id == current_user.tenant_id, PrintAgent.status != "disabled")
             .order_by(PrintAgent.last_seen_at.desc().nullslast()).first())
    if not agent:
        raise HTTPException(status_code=400, detail="Kein aktiver Print-Agent gefunden.")
    paper = db.query(Tenant.label_paper).filter(Tenant.id == current_user.tenant_id).scalar()
    job_id = uuid.uuid4()
    job = PrintJob(id=job_id, tenant_id=current_user.tenant_id, print_agent_id=agent.id, order_id=None,
                   label_pdf_url=f"{str(request.base_url).rstrip('/')}/print-jobs/{job_id}/label",
                   label_pdf_data=content,
                   rotation_degrees=0 if label_paper_service.PAPERS.get(paper) else 90, label_format="4x6", status="pending")
    db.add(job)
    db.commit()
    return {"id": str(job_id), "status": job.status}


@router.post("/disconnect")
def disconnect_dhl(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    account = db.query(DhlAccount).filter(DhlAccount.tenant_id == current_user.tenant_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Kein verbundenes DHL-Konto gefunden.")

    account.api_username_encrypted = None
    account.api_password_encrypted = None
    account.status = "DISCONNECTED"
    db.commit()

    return {"status": "disconnected"}
