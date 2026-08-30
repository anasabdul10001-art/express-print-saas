from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload

from app.core.deps import get_current_user, get_db
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User
from app.schemas.order import OrderItemOut, OrderOut

router = APIRouter(prefix="/orders", tags=["orders"])


def _serialize_order(order: Order) -> OrderOut:
    """
    Builds OrderItemOut manually rather than relying on automatic ORM->schema
    mapping, because image_url/shelf_location intentionally come from the
    linked Product (live data), not straight off the OrderItem row -
    see the note in models/order.py.
    """
    items_out = []
    for item in order.items:
        product = item.product
        items_out.append(
            OrderItemOut(
                id=item.id,
                product_title=product.title if product else "Produkt nicht mehr vorhanden",
                quantity=item.quantity,
                unit_price=item.unit_price,
                image_url=product.image_url if product else None,
                # Prefer the product's CURRENT shelf location; fall back to
                # the order_item's snapshot only if the product was removed.
                shelf_location=(product.shelf_location if product else None) or item.shelf_location,
            )
        )
    return OrderOut(
        id=order.id,
        external_order_ref=order.external_order_ref,
        status=order.status,
        items=items_out,
    )


@router.get("", response_model=list[OrderOut])
def list_orders(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Feeds the dashboard's Orders table: thumbnail, name, shelf location, status."""
    orders = (
        db.query(Order)
        .filter(Order.tenant_id == current_user.tenant_id)
        .options(joinedload(Order.items).joinedload(OrderItem.product))
        .order_by(Order.created_at.desc())
        .all()
    )
    return [_serialize_order(o) for o in orders]


@router.post("/test", response_model=OrderOut, status_code=201)
def create_test_order(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """
    Seeds one fake order with two items, so the dashboard/print-mode flow
    can be tested end-to-end before the real eBay Orders integration exists.
    """
    product_a = Product(
        tenant_id=current_user.tenant_id,
        sku="TEST-001",
        title="USB-C Ladekabel 2m",
        image_url="https://placehold.co/80x80?text=USB-C",
        shelf_location="A3-14",
    )
    product_b = Product(
        tenant_id=current_user.tenant_id,
        sku="TEST-002",
        title="Handyhülle Silikon Schwarz",
        image_url="https://placehold.co/80x80?text=Hülle",
        shelf_location="B1-02",
    )
    db.add_all([product_a, product_b])
    db.flush()

    order = Order(tenant_id=current_user.tenant_id, external_order_ref="TEST-ORDER", status="new")
    db.add(order)
    db.flush()

    db.add_all([
        OrderItem(order_id=order.id, product_id=product_a.id, quantity=1,
                  unit_price=6.99, shelf_location=product_a.shelf_location),
        OrderItem(order_id=order.id, product_id=product_b.id, quantity=2,
                  unit_price=4.50, shelf_location=product_b.shelf_location),
    ])
    db.commit()
    db.refresh(order)

    order = (
        db.query(Order)
        .options(joinedload(Order.items).joinedload(OrderItem.product))
        .filter(Order.id == order.id)
        .first()
    )
    return _serialize_order(order)
