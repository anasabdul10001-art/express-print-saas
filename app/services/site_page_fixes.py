"""
Corrections to public pages whose text lives in the database (SitePage), so a
change of the defaults in routers/plans.py would never reach them.

A legal page is replaced only while it is still the unfinished draft or still
carries the known wrong passage; a page the admin has completed in their own
words is left alone. Running it again changes nothing.
"""

import re

from sqlalchemy.orm import Session

from app.models.site_page import SitePage
from app.services import legal_texts

DRAFT = "[PLATZHALTER"

# a made-up customer quote that was published as a placeholder
INVENTED_QUOTE = re.compile(r"mehrere\s+Stunden\s+manuellen\s+Etikettendruck\s+erspart")

CUSTOMERS_PAGE = (
    "<h1>Erfolgsgeschichten</h1>\n"
    "<p>Hier veröffentlichen wir Erfahrungsberichte, sobald es echte gibt.</p>\n"
    '<p>Du nutzt NettoDesk und möchtest berichten? <a href="contact.html">Schreib uns</a>.</p>'
)

# the withdrawal page no longer applies, so the FAQ must not point to it
FAQ_WITHDRAWAL = re.compile(r"(AGB\s*(?:</a>)?)\s+und\s+im\s+(?:<a[^>]*>)?\s*Widerrufsrecht\s*(?:</a>)?")


def _replacement(slug: str, content: str) -> str | None:
    """The new full text for a page that must be rewritten, or None."""
    draft = DRAFT in content
    if slug == "impressum" and (draft or "consumers/odr" in content):
        return legal_texts.IMPRESSUM
    if slug == "contact" and draft:
        return legal_texts.CONTACT
    if slug == "policies" and draft:
        return legal_texts.policies()
    if slug == "withdrawal" and (draft or "Muster-Widerrufsformular" in content):
        return legal_texts.WITHDRAWAL
    if slug == "cookies" and ("Sitzungs-Token" in content or "insbesondere" in content):
        return legal_texts.COOKIES
    if slug == "status" and "per E-Mail an betroffene" in " ".join(content.split()):
        return legal_texts.STATUS
    if slug == "customers" and INVENTED_QUOTE.search(content):
        return CUSTOMERS_PAGE
    return None


def corrected(slug: str, content: str) -> str:
    replaced = _replacement(slug, content)
    if replaced is not None:
        return replaced
    # the product's former names
    content = content.replace("Express-Print-", "NettoDesk-").replace("Express Print", "NettoDesk")
    content = content.replace("ShipSync", "NettoDesk")
    if slug == "faq":
        content = FAQ_WITHDRAWAL.sub(r"\1", content)
    return content


def apply(db: Session) -> int:
    pages = db.query(SitePage).all()
    # The contact address already published stays usable after the Impressum is rewritten.
    old_email = next((re.search(r"mailto:([^\"'?\s<>]+@[^\"'?\s<>]+)", p.content or "")
                      for p in pages if p.slug == "impressum"), None)
    changed = 0
    for page in pages:
        new = corrected(page.slug, page.content or "")
        if new != page.content:
            if page.slug == "impressum" and old_email:
                address = old_email.group(1)
                new = new.replace("E-Mail: {{operator_email}}",
                                  f'E-Mail: <a href="mailto:{address}">{address}</a>')
            page.content = new
            changed += 1
    if changed:
        db.commit()
    return changed
