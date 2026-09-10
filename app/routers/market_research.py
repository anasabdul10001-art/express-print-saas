from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.deps import get_current_user
from app.models.user import User
from app.schemas.market_research import MarketResearchItem
from app.services.ebay_browse_service import search_active_listings

router = APIRouter(prefix="/market-research", tags=["market-research"])


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
