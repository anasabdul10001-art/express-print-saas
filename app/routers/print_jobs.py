from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db, get_print_agent_from_api_key
from app.models.order import Order
from app.models.print_agent import PrintAgent
from app.models.print_job import PrintJob
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.print_job import PrintJobStatusUpdate, TestPrintJobCreate

router = APIRouter(prefix="/print-jobs", tags=["print-jobs"])


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
    # print job. label_pdf_url would come from the courier/eBay label API
    # once that integration exists - using a placeholder until then.
    job = PrintJob(
        tenant_id=current_user.tenant_id,
        print_agent_id=agent.id,
        order_id=order.id,
        label_pdf_url=f"https://placeholder.example.com/labels/{order.id}.pdf",
        rotation_degrees=90,
        label_format="4x6",
        status="pending",
    )
    db.add(job)
    order.status = "ready_to_print"
    db.commit()
    db.refresh(job)

    return {"id": str(job.id), "status": job.status, "order_id": str(order.id)}
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
