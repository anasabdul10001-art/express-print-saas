"""
Print job creation, including the DHL branch in
app/routers/print_jobs.py::trigger_print_for_order - the DHL API call
itself is mocked (see app/services/dhl_service.py's own docstring: its
request/response shape is unverified against a live sandbox), so these
tests cover ShipSync's own branching logic, not DHL's actual API.
"""

from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import create_engine, text

from app.config import settings
from app.services.dhl_service import DhlShipmentError, DhlShipmentResult

DHL_CONNECT_PAYLOAD = {
    "billing_number": "33333333330101",
    "api_username": "user-valid",
    "api_password": "SandboxPasswort2023!",
    "sender_name": "Test Sender GmbH",
    "sender_street": "Teststraße 1",
    "sender_zip": "10115",
    "sender_city": "Berlin",
    "sender_country": "DE",
}


def _make_product(client, headers, **overrides):
    payload = {"sku": "PJ-SKU", "title": "Print Job Product", "selling_price": 20, "purchase_price": 10, "stock_quantity": 10}
    payload.update(overrides)
    return client.post("/products", headers=headers, json=payload).json()


def _make_agent(client, headers):
    return client.post("/print-agents", headers=headers, json={"name": "Test Agent"}).json()


def _make_order(client, headers, product, quantity=1):
    return client.post("/orders", headers=headers, json={"items": [{"product_id": product["id"], "quantity": quantity}]}).json()


def _set_recipient_address(order_id):
    engine = create_engine(settings.database_url)
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE orders SET recipient_name='Max Mustermann', recipient_street1='Musterstraße 12',
            recipient_city='Berlin', recipient_zip='10115', recipient_country_code='DE'
            WHERE id = :id
        """), {"id": order_id})


def test_print_without_dhl_connected_uses_placeholder(client, register):
    headers, _ = register()
    product = _make_product(client, headers)
    _make_agent(client, headers)
    order = _make_order(client, headers, product)

    response = client.post(f"/print-jobs/order/{order['id']}", headers=headers)
    assert response.status_code == 201
    assert response.json()["status"] == "pending"

    orders_after = client.get("/orders", headers=headers).json()
    assert orders_after[0]["tracking_number"] is None


def test_print_with_no_active_agent_fails(client, register):
    headers, _ = register()
    product = _make_product(client, headers)
    order = _make_order(client, headers, product)

    response = client.post(f"/print-jobs/order/{order['id']}", headers=headers)
    assert response.status_code == 400


def test_print_with_dhl_connected_but_no_address_returns_400(client, register):
    headers, _ = register()
    product = _make_product(client, headers)
    _make_agent(client, headers)
    order = _make_order(client, headers, product)
    client.post("/dhl/connect", headers=headers, json=DHL_CONNECT_PAYLOAD)

    response = client.post(f"/print-jobs/order/{order['id']}", headers=headers)
    assert response.status_code == 400
    assert "Lieferadresse" in response.json()["detail"]


def test_print_with_dhl_success_stores_label_and_tracking_number(client, register):
    headers, _ = register()
    product = _make_product(client, headers, weight_kg=0.5, length_cm=20, width_cm=15, height_cm=10)
    _make_agent(client, headers)
    order = _make_order(client, headers, product, quantity=3)
    _set_recipient_address(order["id"])
    client.post("/dhl/connect", headers=headers, json=DHL_CONNECT_PAYLOAD)

    fake_label = b"%PDF-1.4 fake label"
    with patch("app.routers.print_jobs.dhl_service.create_shipment_label") as mock_create:
        mock_create.return_value = DhlShipmentResult(label_pdf=fake_label, shipment_number="DHL999", tracking_number="DHL999")
        response = client.post(f"/print-jobs/order/{order['id']}", headers=headers)
        assert response.status_code == 201
        job_id = response.json()["id"]

        # weight = 3 * 0.5kg, dimensions from the product itself
        weight_arg = mock_create.call_args[0][2]
        dims_arg = mock_create.call_args[0][3:6]
        assert float(weight_arg) == 1.5
        assert dims_arg == (20, 15, 10)

    orders_after = client.get("/orders", headers=headers).json()
    assert orders_after[0]["tracking_number"] == "DHL999"

    label_response = client.get(f"/print-jobs/{job_id}/label")
    assert label_response.status_code == 200
    assert label_response.content == fake_label
    assert label_response.headers["content-type"] == "application/pdf"


def test_print_with_dhl_rejection_returns_502_and_leaves_order_untouched(client, register):
    headers, _ = register()
    product = _make_product(client, headers)
    _make_agent(client, headers)
    order = _make_order(client, headers, product)
    _set_recipient_address(order["id"])
    client.post("/dhl/connect", headers=headers, json=DHL_CONNECT_PAYLOAD)

    with patch("app.routers.print_jobs.dhl_service.create_shipment_label") as mock_create:
        mock_create.side_effect = DhlShipmentError("DHL hat die Anfrage abgelehnt (Status 400): invalid billing number")
        response = client.post(f"/print-jobs/order/{order['id']}", headers=headers)
        assert response.status_code == 502

    orders_after = client.get("/orders", headers=headers).json()
    assert orders_after[0]["status"] == "new"  # never got marked ready_to_print
    assert orders_after[0]["tracking_number"] is None


def test_missing_label_returns_404(client):
    response = client.get("/print-jobs/00000000-0000-0000-0000-000000000000/label")
    assert response.status_code == 404


def test_resolve_package_falls_back_to_tenant_default_when_weight_unknown():
    """Unit-level check of the fallback logic itself (see
    app/routers/print_jobs.py::_resolve_package): a partial sum would
    silently understate real weight, so ANY item missing a weight should
    fall back entirely to the tenant default, not just skip that item."""
    from types import SimpleNamespace
    from app.routers.print_jobs import _resolve_package

    tenant = SimpleNamespace(
        default_package_weight_kg=Decimal("1.0"),
        default_package_length_cm=20, default_package_width_cm=15, default_package_height_cm=10,
    )
    item_known = SimpleNamespace(product=SimpleNamespace(weight_kg=Decimal("0.5"), length_cm=None, width_cm=None, height_cm=None), quantity=2)
    item_unknown = SimpleNamespace(product=SimpleNamespace(weight_kg=None, length_cm=None, width_cm=None, height_cm=None), quantity=1)
    order = SimpleNamespace(items=[item_known, item_unknown])

    weight, length, width, height = _resolve_package(order, tenant)
    assert weight == Decimal("1.0")  # fell back, not 1.0kg (2*0.5) partial sum
    assert (length, width, height) == (20, 15, 10)
