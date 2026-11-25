"""
Who runs this installation. The name and address are typed once (OPERATOR_*
settings on the server, not in this public repository) and appear on the
Impressum, the privacy page, the terms and the processing agreement.
"""

import html
import re

from sqlalchemy.orm import Session

from app.config import settings
from app.models.site_page import SitePage

MISSING = "[wird noch ergänzt]"
REQUIRED = ("name", "street", "city")


def details(db: Session) -> dict:
    email = settings.operator_email.strip()
    if not email:  # the address the admin already published on the Impressum
        page = db.query(SitePage.content).filter(SitePage.slug == "impressum").scalar() or ""
        found = re.search(r"mailto:([^\"'?\s<>]+@[^\"'?\s<>]+)", page)
        email = found.group(1) if found else ""
    return {
        "name": settings.operator_name.strip(),
        "street": settings.operator_street.strip(),
        "city": settings.operator_city.strip(),
        "country": settings.operator_country.strip(),
        "email": email,
        "phone": settings.operator_phone.strip(),
        "vat_id": settings.operator_vat_id.strip(),
    }


def complete(values: dict) -> bool:
    return all(values[key] for key in REQUIRED + ("email",))


def render(content: str, values: dict) -> str:
    """Fills {{operator_*}} in a stored page. Lines marked as optional disappear when empty."""
    for key in ("phone", "vat_id"):
        block = re.compile(r"\{\{#%s\}\}(.*?)\{\{/%s\}\}" % (key, key), re.S)
        content = block.sub(lambda m: m.group(1) if values[key] else "", content)
    for key, value in values.items():
        content = content.replace("{{operator_%s}}" % key, html.escape(value) if value else MISSING)
    return content
