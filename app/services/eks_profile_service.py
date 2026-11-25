"""Checking, encrypting and reading the saved details of the Anlage EKS (sections A, B, D)."""

import json
import re
from datetime import date, datetime

from cryptography.fernet import InvalidToken
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.core.crypto import decrypt_token, encrypt_token
from app.models.eks_profile import EksProfile

# our name -> the field in the official form
FORM_FIELDS = {
    "applicant_first_name": "txtfPersonVorname",
    "applicant_last_name": "txtfPersonNachname",
    "applicant_birth_date": "datePersonGebDatum",
    "community_number": "txtfBGNr",
    "self_employed_first_name": "txtfDritteVorname",
    "self_employed_last_name": "txtfDritteNachname",
    "self_employed_birth_date": "dateDritteGebDatum",
    "company_name": "txtfTaetigkeitFirmenname",
    "street": "txtfTaetigkeitStr",
    "house_number": "txtfTaetigkeitHausnr",
    "postal_code": "txtfTaetigkeitPlz",
    "city": "txtfTaetigkeitOrt",
    "activity": "txtfTaetigkeitGewerbe",
    "activity_start": "dateTaetigkeitBeginn",
    "activity_end": "dateTaetigkeitEnde",
    "legal_form": "txtfTaetigkeitRechtsform",
}


class EksDetails(BaseModel):
    """Everything is optional; what is empty stays empty in the form."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)

    applicant_first_name: str = Field("", max_length=60)
    applicant_last_name: str = Field("", max_length=60)
    applicant_birth_date: str = Field("", max_length=10)
    community_number: str = Field("", max_length=40)
    self_employed_first_name: str = Field("", max_length=60)
    self_employed_last_name: str = Field("", max_length=60)
    self_employed_birth_date: str = Field("", max_length=10)
    company_name: str = Field("", max_length=100)
    street: str = Field("", max_length=40)
    house_number: str = Field("", max_length=12)
    postal_code: str = Field("", max_length=10)
    city: str = Field("", max_length=40)
    activity: str = Field("", max_length=100)
    activity_start: str = Field("", max_length=7)
    activity_end: str = Field("", max_length=7)
    legal_form: str = Field("", max_length=100)

    @field_validator("applicant_birth_date", "self_employed_birth_date")
    @classmethod
    def full_date(cls, value: str) -> str:
        if not value:
            return value
        if not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value):
            raise ValueError("Datum bitte als TT.MM.JJJJ eingeben.")
        try:
            day = datetime.strptime(value, "%d.%m.%Y").date()
        except ValueError:
            raise ValueError("Dieses Datum gibt es nicht.")
        if not date(1900, 1, 1) <= day <= date.today():
            raise ValueError("Bitte das Geburtsdatum prüfen.")
        return value

    @field_validator("activity_start", "activity_end")
    @classmethod
    def month_and_year(cls, value: str) -> str:
        if value and not re.fullmatch(r"(0[1-9]|1[0-2])\.(19|20)\d{2}", value):
            raise ValueError("Bitte als MM.JJJJ eingeben.")
        return value

    def is_empty(self) -> bool:
        return not any(self.model_dump().values())


class UnreadableProfile(Exception):
    """The saved data can't be decrypted (e.g. the key was changed)."""


def load(db: Session, tenant_id) -> EksDetails | None:
    row = db.get(EksProfile, tenant_id)
    if row is None:
        return None
    try:
        return EksDetails.model_validate(json.loads(decrypt_token(row.data_encrypted)))
    except (InvalidToken, ValueError):
        raise UnreadableProfile


def save(db: Session, tenant_id, details: EksDetails) -> None:
    encrypted = encrypt_token(json.dumps(details.model_dump(), ensure_ascii=False))
    row = db.get(EksProfile, tenant_id)
    if row is None:
        db.add(EksProfile(tenant_id=tenant_id, data_encrypted=encrypted))
    else:
        row.data_encrypted = encrypted
    db.commit()


def delete(db: Session, tenant_id) -> None:
    db.query(EksProfile).filter(EksProfile.tenant_id == tenant_id).delete()
    db.commit()


def form_values(details: EksDetails) -> dict:
    return {FORM_FIELDS[name]: value for name, value in details.model_dump().items() if value}
