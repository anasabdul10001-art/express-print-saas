from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from app.config import settings
from app.core.deps import get_current_user, get_db
from app.models.market_research import MarketSnapshot, TrackedSearch
from app.models.user import User
from app.schemas.market_research import (
    MarketResearchItem,
    MarketSnapshotOut,
    TrackedSearchCreate,
    TrackedSearchOut,
)
from app.services.ebay_browse_service import search_active_listings

router = APIRouter(prefix="/market-research", tags=["market-research"])

# The daily snapshot job searches many tracked terms in one run, so it uses
# a smaller per-term limit than an interactive search - keeps total runtime
# and eBay API usage reasonable regardless of how many tenants are tracking.
SNAPSHOT_SEARCH_LIMIT = 15


@router.get("/search", response_model=list[MarketResearchItem])
def search(
    q: str = Query(..., min_length=1, description="Suchbegriff, z.B. Produktname"),
    limit: int = Query(30, ge=1, le=50),
    current_user: User = Depends(get_current_user),
):
    """
    Searches eBay's current active listings for `q`. Requires login (any
    tenant), but does NOT require that tenant to have connected their own
    eBay account - this uses an app-level token, not a seller's.
    """
    try:
        return search_active_listings(q, limit)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"eBay-Suche fehlgeschlagen: {exc}")


@router.post("/tracked", response_model=TrackedSearchOut, status_code=201)
def create_tracked_search(
    payload: TrackedSearchCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    existing = (
        db.query(TrackedSearch)
        .filter(TrackedSearch.tenant_id == current_user.tenant_id, TrackedSearch.query == payload.query)
        .first()
    )
    if existing:
        return existing

    tracked = TrackedSearch(tenant_id=current_user.tenant_id, query=payload.query)
    db.add(tracked)
    db.commit()
    db.refresh(tracked)
    return tracked


@router.get("/tracked", response_model=list[TrackedSearchOut])
def list_tracked_searches(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return (
        db.query(TrackedSearch)
        .filter(TrackedSearch.tenant_id == current_user.tenant_id)
        .order_by(TrackedSearch.created_at.desc())
        .all()
    )


@router.delete("/tracked/{tracked_id}", status_code=204)
def delete_tracked_search(
    tracked_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tracked = (
        db.query(TrackedSearch)
        .filter(TrackedSearch.id == tracked_id, TrackedSearch.tenant_id == current_user.tenant_id)
        .first()
    )
    if not tracked:
        raise HTTPException(status_code=404, detail="Verfolgter Suchbegriff nicht gefunden")
    db.delete(tracked)
    db.commit()


@router.get("/tracked/{tracked_id}/history", response_model=list[MarketSnapshotOut])
def get_tracked_search_history(
    tracked_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tracked = (
        db.query(TrackedSearch)
        .filter(TrackedSearch.id == tracked_id, TrackedSearch.tenant_id == current_user.tenant_id)
        .first()
    )
    if not tracked:
        raise HTTPException(status_code=404, detail="Verfolgter Suchbegriff nicht gefunden")

    return (
        db.query(MarketSnapshot)
        .filter(MarketSnapshot.tracked_search_id == tracked_id)
        .order_by(MarketSnapshot.snapshot_date.asc())
        .all()
    )


@router.post("/tracked/run-snapshots")
def run_snapshots(
    x_cron_secret: str = Header(None),
    db: Session = Depends(get_db),
):
    """
    Takes today's snapshot for every tenant's tracked search. Meant to be
    called once a day by an external scheduler (see
    .github/workflows/market-research-snapshot.yml) - not by the frontend.
    Idempotent: re-running the same day just skips searches that already
    have today's snapshot, so a retry or a duplicate trigger is harmless.
    """
    if not settings.cron_secret or x_cron_secret != settings.cron_secret:
        raise HTTPException(status_code=403, detail="Invalid cron secret")

    today = datetime.now(timezone.utc).date()
    all_tracked = db.query(TrackedSearch).all()

    created, skipped, failed = 0, 0, 0
    for tracked in all_tracked:
        already_done = (
            db.query(MarketSnapshot)
            .filter(MarketSnapshot.tracked_search_id == tracked.id, MarketSnapshot.snapshot_date == today)
            .first()
        )
        if already_done:
            skipped += 1
            continue

        try:
            results = search_active_listings(tracked.query, limit=SNAPSHOT_SEARCH_LIMIT)
        except ValueError:
            failed += 1
            continue

        sold_values = [r["estimated_sold_quantity"] for r in results if r.get("estimated_sold_quantity") is not None]
        price_values = [Decimal(str(r["price"])) for r in results if r.get("price") is not None]

        db.add(MarketSnapshot(
            tracked_search_id=tracked.id,
            snapshot_date=today,
            item_count=len(results),
            total_estimated_sold=sum(sold_values) if sold_values else None,
            average_price=(sum(price_values) / len(price_values)) if price_values else None,
        ))
        db.commit()
        created += 1

    return {"created": created, "skipped": skipped, "failed": failed, "total_tracked": len(all_tracked)}
