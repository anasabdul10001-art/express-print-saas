"""Admin CRUD for plans, payment methods and site settings, and what the public pricing endpoints expose."""

from tests.test_site_pages import _make_superadmin


def _admin(client, register):
    headers, _ = register()
    _make_superadmin(headers, client)
    return headers


def test_catalog_endpoints_require_superadmin(client, register):
    headers, _ = register()
    for method, path in [("get", "/admin/plans"), ("get", "/admin/payment-methods"),
                         ("get", "/admin/settings"), ("get", "/admin/tenants"), ("get", "/admin/invoices")]:
        assert getattr(client, method)(path, headers=headers).status_code == 403, path
    assert client.get("/admin/plans").status_code in (401, 403)


def test_plan_lifecycle_and_public_visibility(client, register):
    headers = _admin(client, register)
    created = client.post("/admin/plans", headers=headers, json={"name": "Starter", "price_monthly": "9.00"})
    assert created.status_code == 201
    hidden = client.post("/admin/plans", headers=headers, json={"name": "Hidden", "is_active": False}).json()

    public_names = [p["name"] for p in client.get("/plans").json()]
    assert "Starter" in public_names and "Hidden" not in public_names
    assert {p["name"] for p in client.get("/admin/plans", headers=headers).json()} >= {"Starter", "Hidden"}

    plan_id = created.json()["id"]
    patched = client.patch(f"/admin/plans/{plan_id}", headers=headers, json={"price_monthly": "12.50"})
    assert patched.status_code == 200 and float(patched.json()["price_monthly"]) == 12.5

    assert client.delete(f"/admin/plans/{hidden['id']}", headers=headers).status_code == 204
    assert client.patch("/admin/plans/00000000-0000-0000-0000-000000000000", headers=headers,
                        json={"name": "x"}).status_code == 404


def test_payment_method_lifecycle_and_public_visibility(client, register):
    headers = _admin(client, register)
    shown = client.post("/admin/payment-methods", headers=headers, json={"name": "Bank", "details": "IBAN DE00"})
    assert shown.status_code == 201
    client.post("/admin/payment-methods", headers=headers, json={"name": "Old", "details": "x", "is_active": False})

    public_names = [m["name"] for m in client.get("/payment-methods").json()]
    assert "Bank" in public_names and "Old" not in public_names

    method_id = shown.json()["id"]
    assert client.patch(f"/admin/payment-methods/{method_id}", headers=headers,
                        json={"is_active": False}).status_code == 200
    assert "Bank" not in [m["name"] for m in client.get("/payment-methods").json()]
    assert client.delete(f"/admin/payment-methods/{method_id}", headers=headers).status_code == 204
    assert client.delete(f"/admin/payment-methods/{method_id}", headers=headers).status_code == 404


def test_site_settings_update_and_validation(client, register):
    headers = _admin(client, register)
    ok = client.patch("/admin/settings", headers=headers,
                      json={"company_legal_name": "LamarConnect", "logo_height": 40})
    assert ok.status_code == 200 and ok.json()["company_legal_name"] == "LamarConnect"
    assert client.get("/admin/settings", headers=headers).json()["logo_height"] == 40
    assert client.patch("/admin/settings", headers=headers, json={"logo_height": 5}).status_code == 422


def test_confirm_payment_without_amount_or_plan_is_rejected(client, register):
    headers = _admin(client, register)
    me = client.get("/auth/me", headers=headers).json()
    r = client.post(f"/admin/tenants/{me['tenant_id']}/confirm-payment", headers=headers, json={})
    assert r.status_code in (400, 422)
    assert client.post("/admin/tenants/00000000-0000-0000-0000-000000000000/confirm-payment",
                       headers=headers, json={"amount": "10"}).status_code == 404
