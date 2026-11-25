"""Saved personal details for the Anlage EKS: opt-in, encrypted, deletable."""
import io
import uuid

from pypdf import PdfReader

from app.database import SessionLocal
from app.models.eks_profile import EksProfile
from app.models.user import User
from app.services import eks_pdf_service, eks_profile_service

URL = "/accounting/eks/profile"
PERIOD = {"start": "2026-07-01", "end": "2026-10-31"}
DETAILS = {
    "applicant_first_name": "Jörg", "applicant_last_name": "Müller", "applicant_birth_date": "05.03.1990",
    "community_number": "12345//0001234", "self_employed_first_name": "Jörg", "self_employed_last_name": "Müller",
    "self_employed_birth_date": "05.03.1990", "company_name": "Testfirma", "street": "Beispielweg",
    "house_number": "7a", "postal_code": "01234", "city": "Musterstadt", "activity": "Onlinehandel",
    "activity_start": "02.2025", "activity_end": "", "legal_form": "Einzelunternehmen",
}


def _save(client, headers, consent=True, **changes):
    return client.put(URL, headers=headers, json={"consent": consent, "details": {**DETAILS, **changes}})


def _form(client, headers, **params):
    r = client.get("/accounting/eks/form.pdf", headers=headers, params={**PERIOD, **params})
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    return {k: v.get("/V") for k, v in PdfReader(io.BytesIO(r.content)).get_fields().items()}


def _rows():
    with SessionLocal() as db:
        return db.query(EksProfile).all()


def test_every_saved_detail_has_a_field_in_the_official_form():
    existing = set(PdfReader(eks_pdf_service.TEMPLATE).get_fields())
    assert set(eks_profile_service.FORM_FIELDS) == set(eks_profile_service.EksDetails.model_fields)
    assert set(eks_profile_service.FORM_FIELDS.values()) <= existing


def test_saved_once_it_appears_in_every_form_and_is_encrypted(client, register):
    headers, _ = register()
    assert client.get(URL, headers=headers).json()["saved"] is False
    assert _save(client, headers).status_code == 200
    got = client.get(URL, headers=headers)
    assert got.json() == {"saved": True, "details": DETAILS} and got.headers["cache-control"] == "no-store"
    stored = _rows()[0].data_encrypted
    for secret in ("Müller", "Muller", "05.03.1990", "12345//0001234", "Beispielweg"):
        assert secret not in stored
    f = _form(client, headers)
    for name, value in DETAILS.items():
        if value:
            assert f[eks_profile_service.FORM_FIELDS[name]] == value, name
    # never filled for the seller
    assert not f.get("dateUnterschriftPerson") and not f.get("dateUnterschriftBetreuer")
    assert not _form(client, headers, personal=False).get("txtfPersonVorname")


def test_nothing_is_stored_without_consent_or_without_content(client, register):
    headers, _ = register()
    assert _save(client, headers, consent=False).status_code == 400
    assert client.put(URL, headers=headers, json={"consent": True, "details": {}}).status_code == 400
    assert _rows() == []


def test_wrong_values_are_refused_and_nothing_is_stored(client, register):
    headers, _ = register()
    for change in ({"applicant_birth_date": "31.02.1990"}, {"applicant_birth_date": "1990-03-05"},
                   {"applicant_birth_date": "01.01.2999"}, {"activity_start": "13.2025"},
                   {"street": "x" * 41}, {"unknown_field": "geheim-123"}):
        r = _save(client, headers, **change)
        assert r.status_code == 422, change
    assert _rows() == []


def test_changing_and_deleting(client, register):
    headers, _ = register()
    _save(client, headers)
    _save(client, headers, city="Neustadt")
    assert len(_rows()) == 1 and client.get(URL, headers=headers).json()["details"]["city"] == "Neustadt"
    assert client.delete(URL, headers=headers).status_code == 204
    assert _rows() == [] and not _form(client, headers).get("txtfPersonVorname")


def test_only_the_own_owner_gets_them(client, register):
    owner, _ = register()
    _save(client, owner)
    other, _ = register()
    assert client.get(URL, headers=other).json()["saved"] is False
    assert not _form(client, other).get("txtfPersonVorname")
    client.delete(URL, headers=other)
    assert len(_rows()) == 1
    assert client.get(URL).status_code in (401, 403, 422)
    tenant_id = uuid.UUID(client.get("/auth/me", headers=owner).json()["tenant_id"])
    with SessionLocal() as db:
        db.query(User).filter(User.tenant_id == tenant_id).update({"role": "staff"})
        db.commit()
    assert client.get(URL, headers=owner).status_code == 403
    assert _save(client, owner).status_code == 403 and client.delete(URL, headers=owner).status_code == 403
    assert not _form(client, owner).get("txtfPersonVorname")


def test_unreadable_data_gives_a_clear_message_and_can_be_skipped_or_deleted(client, register):
    headers, _ = register()
    _save(client, headers)
    with SessionLocal() as db:
        db.query(EksProfile).update({"data_encrypted": "not-a-token"})
        db.commit()
    assert client.get(URL, headers=headers).status_code == 409
    assert client.get("/accounting/eks/form.pdf", headers=headers, params=PERIOD).status_code == 409
    _form(client, headers, personal=False)
    assert client.delete(URL, headers=headers).status_code == 204
    _form(client, headers)
