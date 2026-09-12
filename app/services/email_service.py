"""
Sends transactional email via Resend's HTTP API (https://resend.com).
Plain httpx call rather than a dedicated SDK - already a dependency, and
Resend's API is a single simple POST, not worth adding another package for.
"""

import base64

import httpx

from app.config import settings

RESEND_API_URL = "https://api.resend.com/emails"


def send_password_reset_email(to_email: str, reset_link: str) -> None:
    """
    Raises RuntimeError on failure. Caller decides how to handle that -
    see the note in routers/auth.py about never revealing to the caller
    whether the email address exists, only whether sending itself worked.
    """
    if not settings.resend_api_key:
        raise RuntimeError("RESEND_API_KEY ist nicht konfiguriert.")

    response = httpx.post(
        RESEND_API_URL,
        headers={
            "Authorization": f"Bearer {settings.resend_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": settings.resend_from_email,
            "to": [to_email],
            "subject": "Passwort zurücksetzen — ShipSync",
            "html": (
                f"<p>Klicke auf den folgenden Link, um dein Passwort zurückzusetzen:</p>"
                f'<p><a href="{reset_link}">{reset_link}</a></p>'
                f"<p>Dieser Link ist 1 Stunde gültig. Falls du das nicht angefordert hast, "
                f"kannst du diese E-Mail ignorieren.</p>"
            ),
        },
        timeout=15.0,
    )

    if response.status_code not in (200, 201):
        raise RuntimeError(f"resend_send_failed: {response.text}")


def send_invoice_email(to_email: str, invoice_number: str, pdf_bytes: bytes, company_name: str | None) -> None:
    """
    Raises RuntimeError on failure - the caller (routers/admin.py) decides
    how to surface that (the Invoice row itself is already committed by
    then, so a failed send here never loses the invoice number).
    """
    if not settings.resend_api_key:
        raise RuntimeError("RESEND_API_KEY ist nicht konfiguriert.")

    sender_name = company_name or "ShipSync"

    response = httpx.post(
        RESEND_API_URL,
        headers={
            "Authorization": f"Bearer {settings.resend_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": settings.resend_from_email,
            "to": [to_email],
            "subject": f"Deine Rechnung {invoice_number} — {sender_name}",
            "html": (
                f"<p>Vielen Dank für deine Zahlung.</p>"
                f"<p>Im Anhang findest du deine Rechnung <strong>{invoice_number}</strong>.</p>"
            ),
            "attachments": [
                {
                    "filename": f"Rechnung-{invoice_number}.pdf",
                    "content": base64.b64encode(pdf_bytes).decode("ascii"),
                }
            ],
        },
        timeout=20.0,
    )

    if response.status_code not in (200, 201):
        raise RuntimeError(f"resend_send_failed: {response.text}")
