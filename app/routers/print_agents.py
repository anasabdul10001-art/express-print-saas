from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db, get_print_agent_from_api_key
from app.core.security import generate_api_key, hash_api_key
from app.models.print_agent import PrintAgent
from app.models.print_job import PrintJob
from app.models.user import User
from app.schemas.print_agent import PrintAgentCreate, PrintAgentCreatedResponse

router = APIRouter(prefix="/print-agents", tags=["print-agents"])


@router.post("", response_model=PrintAgentCreatedResponse, status_code=status.HTTP_201_CREATED)
def create_print_agent(
    payload: PrintAgentCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Registers a new Print Agent for the logged-in user's tenant and returns
    a fresh API key. This key is shown ONCE - only its hash is stored.
    Copy it straight into the Print Agent's config.json (agent_id + api_key).
    """
    raw_key = generate_api_key()

    agent = PrintAgent(
        tenant_id=current_user.tenant_id,
        name=payload.name,
        api_key_hash=hash_api_key(raw_key),
        status="offline",
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)

    return PrintAgentCreatedResponse(id=agent.id, name=agent.name, api_key=raw_key)


@router.get("/{agent_id}/next-job")
def get_next_job(
    agent_id: str,
    agent: PrintAgent = Depends(get_print_agent_from_api_key),
    db: Session = Depends(get_db),
):
    """
    Polled by the local Print Agent script every few seconds. Returns 204
    (no content) when there's nothing to print - this is the normal,
    expected case most of the time, not an error.
    """
    if str(agent.id) != agent_id:
        raise HTTPException(status_code=403, detail="API key does not match this agent_id")

    job = (
        db.query(PrintJob)
        .filter(PrintJob.print_agent_id == agent.id, PrintJob.status == "pending")
        .order_by(PrintJob.created_at.asc())
        .first()
    )

    if not job:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    job.status = "processing"
    db.commit()

    return {
        "id": str(job.id),
        "label_pdf_url": job.label_pdf_url,
        "rotation_degrees": job.rotation_degrees,
        "label_format": job.label_format,
    }
