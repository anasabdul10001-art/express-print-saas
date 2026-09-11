import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session, joinedload

from app.core.deps import get_current_user, get_db, get_print_agent_from_api_key
from app.models.dhl_account import DhlAccount
from app.models.order import Order, OrderItem
from app.models.print_agent import PrintAgent
from app.models.print_job import PrintJob
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.print_job import PrintJobStatusUpdate, TestPrintJobCreate
from app.services import dhl_service
from app.services.dhl_service import DhlShipmentError

router = APIRouter(prefix="/print-jobs", tags=["print-jobs"])


def _resolve_package(order: Order, tenant: Tenant) -> tuple[Decimal, int, int, int]:
    """
    MVP simplification (same spirit as the single-agent routing below):
    doesn't do real bin-packing for a multi-box order. Weight is only taken
    from the order's own items if EVERY item has a known weight - a partial
    sum would silently understate the real weight, which is worse than
    falling back to the tenant's default. Dimensions come from the first
    item that has its own set, or the tenant default otherwise.
    """
    total_weight = Decimal("0")
    all_weights_known = True
    dims: tuple[int, int, int] | None = None

    for item in order.items:
        product = item.product
        if product and product.weight_kg:
            total_weight += product.weight_kg * item.quantity
        else:
            all_weights_known = False
        if dims is None and product and product.length_cm and product.width_cm and product.height_cm:
            dims = (product.length_cm, product.width_cm, product.height_cm)

    if not all_weights_known or total_weight <= 0:
        total_weight = tenant.default_package_weight_kg
    if dims is None:
        dims = (tenant.default_package_length_cm, tenant.default_package_width_cm, tenant.default_package_height_cm)

    return (total_weight, *dims)


@router.post("/test", status_code=status.HTTP_201_CREATED)
def create_test_job(
    payload: TestPrintJobCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Manually enqueues a print job for a specific agent, without needing a
    real order. This is what lets you test end-to-end printing before the
    Orders module (Module A) exists. Point label_pdf_url at any publicly
    reachable PDF for now.
    """
    agent = (
        db.query(PrintAgent)
        .filter(PrintAgent.id == payload.print_agent_id, PrintAgent.tenant_id == current_user.tenant_id)
        .first()
    )
    if not agent:
        raise HTTPException(status_code=404, detail="Print agent not found for this account")

    job = PrintJob(
        tenant_id=current_user.tenant_id,
        print_agent_id=agent.id,
        label_pdf_url=str(payload.label_pdf_url),
        rotation_degrees=payload.rotation_degrees,
        label_format=payload.label_format,
        status="pending",
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    return {"id": str(job.id), "status": job.status}


@router.post("/order/{order_id}", status_code=status.HTTP_201_CREATED)
def trigger_print_for_order(
    order_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Manual trigger for the DASHBOARD_MANUAL workflow's "Print Label" button.
    Also usable as an override in INSTANT mode (e.g. a reprint), and as the
    entry point for INTEGRATED mode - see the TODO below for what's still
    missing there.
    """
    order = (
        db.query(Order)
        .options(joinedload(Order.items).joinedload(OrderItem.product))
        .filter(Order.id == order_id, Order.tenant_id == current_user.tenant_id)
        .first()
    )
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    tenant = db.query(Tenant).filter(Tenant.id == current_user.tenant_id).first()

    # MVP simplification: assign to the tenant's most-recently-active agent.
    # Once multi-printer/multi-warehouse routing matters (Business tier),
    # this needs a real rule - e.g. "route by store_settings" or "let the
    # user pick a target agent per order."
    agent = (
        db.query(PrintAgent)
        .filter(PrintAgent.tenant_id == current_user.tenant_id, PrintAgent.status != "disabled")
        .order_by(PrintAgent.last_seen_at.desc().nullslast())
        .first()
    )
    if not agent:
        raise HTTPException(status_code=400, detail="No active print agent found for this account")

    if tenant.print_mode == "INTEGRATED":
        # TODO: this mode needs a PDF-composition step that doesn't exist
        # yet - merging the courier's shipping-label PDF with a generated
        # picking slip (product photo + name + shelf_location per item)
        # into a single PDF. That's a separate piece of work (likely
        # reportlab/pypdf, run server-side, producing the label_pdf_url
        # below instead of a raw courier URL). Flagging honestly rather
        # than faking it - this endpoint intentionally rejects for now
        # so we don't print an incomplete label by accident.
        raise HTTPException(
            status_code=501,
            detail="INTEGRATED print mode is configured but the combined "
                   "label+picking-slip PDF generator isn't built yet.",
        )

    # INSTANT and DASHBOARD_MANUAL both end up here: a normal shipping-label
    # print job. If this tenant has connected their own DHL account, buy a
    # real label from DHL now; otherwise fall back to the placeholder URL,
    # same as before this integration existed - connecting DHL is optional,
    # and everyone who hasn't shouldn't lose the ability to print at all.
    dhl_account = (
        db.query(DhlAccount)
        .filter(DhlAccount.tenant_id == current_user.tenant_id, DhlAccount.status == "CONNECTED")
        .first()
    )

    job_id = uuid.uuid4()
    label_pdf_url = f"https://placeholder.example.com/labels/{order.id}.pdf"
    label_pdf_data = None

    if dhl_account:
        if not order.recipient_street1 or not order.recipient_city or not order.recipient_zip:
            raise HTTPException(
                status_code=400,
                detail="Für diese Bestellung fehlt eine vollständige Lieferadresse - "
                       "DHL-Versandetikett kann nicht erstellt werden.",
            )

        tenant_row = db.query(Tenant).filter(Tenant.id == current_user.tenant_id).first()
        weight_kg, length_cm, width_cm, height_cm = _resolve_package(order, tenant_row)
        try:
            result = dhl_service.create_shipment_label(dhl_account, order, weight_kg, length_cm, width_cm, height_cm)
        except DhlShipmentError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

        label_pdf_data = result.label_pdf
        label_pdf_url = f"{str(request.base_url).rstrip('/')}/print-jobs/{job_id}/label"
        order.tracking_number = result.tracking_number

    job = PrintJob(
        id=job_id,
        tenant_id=current_user.tenant_id,
        print_agent_id=agent.id,
        order_id=order.id,
        label_pdf_url=label_pdf_url,
        label_pdf_data=label_pdf_data,
        rotation_degrees=90,
        label_format="4x6",
        status="pending",
    )
    db.add(job)
    order.status = "ready_to_print"
    db.commit()
    db.refresh(job)

    return {"id": str(job.id), "status": job.status, "order_id": str(order.id)}


@router.get("/{job_id}/label")
def get_print_job_label(job_id: str, db: Session = Depends(get_db)):
    """
    Serves a DHL-generated label's raw PDF bytes. Deliberately unauthenticated:
    the Print Agent's downloader (print-agent/agent_core.py) does a plain
    `requests.get(job["label_pdf_url"])` with no Authorization header, and a
    print job's id is an unguessable UUID that's never exposed publicly -
    only to the tenant's own authenticated dashboard and their own print
    agent (which learns it from the equally-authenticated next-job poll) -
    so knowing it is treated as sufficient to fetch the one label it points to.
    """
    job = db.query(PrintJob).filter(PrintJob.id == job_id).first()
    if not job or not job.label_pdf_data:
        raise HTTPException(status_code=404, detail="Label not found")
    return Response(content=job.label_pdf_data, media_type="application/pdf")


@router.patch("/{job_id}/status")
def update_job_status(
    job_id: str,
    payload: PrintJobStatusUpdate,
    agent: PrintAgent = Depends(get_print_agent_from_api_key),
    db: Session = Depends(get_db),
):
    """Called by the Print Agent to report a job as printed or failed."""
    job = db.query(PrintJob).filter(PrintJob.id == job_id, PrintJob.print_agent_id == agent.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Print job not found for this agent")

    if payload.status not in ("printed", "failed"):
        raise HTTPException(status_code=400, detail="status must be 'printed' or 'failed'")

    job.status = payload.status
    job.error_message = payload.error_message
    db.commit()

    return {"id": str(job.id), "status": job.status}
