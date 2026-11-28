"""
Sends transactional email via Resend's HTTP API (https://resend.com).
Plain httpx call rather than a dedicated SDK - already a dependency, and
Resend's API is a single simple POST, not worth adding another package for.
"""

import base64
import html

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
            "subject": "Passwort zurücksetzen — NettoDesk",
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


def send_affiliate_password_email(to_email: str, set_password_link: str, is_initial_setup: bool) -> None:
    """
    Sent both when an admin first creates an affiliate with an email (initial
    setup) and for the affiliate portal's own "forgot password" flow - both
    land on the same set-password page/token, only the wording differs.
    Raises RuntimeError on failure; see send_password_reset_email for why
    callers treat that as non-fatal.
    """
    if not settings.resend_api_key:
        raise RuntimeError("RESEND_API_KEY ist nicht konfiguriert.")

    intro = (
        "du wurdest als NettoDesk-Partner (Affiliate) angelegt. Klicke auf den folgenden Link, "
        "um dein Passwort für das Partner-Portal festzulegen:"
        if is_initial_setup
        else "klicke auf den folgenden Link, um dein Passwort für das Partner-Portal zurückzusetzen:"
    )

    response = httpx.post(
        RESEND_API_URL,
        headers={
            "Authorization": f"Bearer {settings.resend_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": settings.resend_from_email,
            "to": [to_email],
            "subject": "Partner-Portal — NettoDesk",
            "html": (
                f"<p>{intro}</p>"
                f'<p><a href="{set_password_link}">{set_password_link}</a></p>'
                f"<p>Dieser Link ist 24 Stunden gültig. Falls du das nicht erwartet hast, "
                f"kannst du diese E-Mail ignorieren.</p>"
            ),
        },
        timeout=15.0,
    )

    if response.status_code not in (200, 201):
        raise RuntimeError(f"resend_send_failed: {response.text}")


def send_welcome_email(to_email: str, customer_name: str, plan_name: str | None,
                       company_name: str | None, app_url: str) -> None:
    """Sent once, with the first paid subscription. Raises RuntimeError on failure."""
    if not settings.resend_api_key:
        raise RuntimeError("RESEND_API_KEY ist nicht konfiguriert.")

    sender_name = company_name or "NettoDesk"
    base = app_url.rstrip("/")
    plan = f" im Tarif <strong>{html.escape(plan_name)}</strong>" if plan_name else ""

    response = httpx.post(
        RESEND_API_URL,
        headers={
            "Authorization": f"Bearer {settings.resend_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": settings.resend_from_email,
            "to": [to_email],
            "subject": "Willkommen bei NettoDesk",
            "html": (
                f"<p>Hallo {html.escape(customer_name)},</p>"
                f"<p>willkommen bei NettoDesk! Dein Abonnement{plan} ist aktiv.</p>"
                f'<p><a href="{base}/login.html">Hier anmelden</a></p>'
                f"<p>Die Rechnung zu deiner Zahlung bekommst du in einer eigenen E-Mail als PDF. "
                f"Alle Rechnungen findest du außerdem in deinem Konto unter "
                f'<a href="{base}/app/billing.html#rechnungen">Rechnungen</a>.</p>'
                f"<p>Viele Grüße<br>{html.escape(sender_name)}</p>"
            ),
        },
        timeout=15.0,
    )

    if response.status_code not in (200, 201):
        raise RuntimeError(f"resend_send_failed: {response.text}")


def send_invoice_email(to_email: str, invoice_number: str, pdf_bytes: bytes, company_name: str | None,
                       cancellation: bool = False) -> None:
    """
    Raises RuntimeError on failure - the caller (routers/admin.py) decides
    how to surface that (the Invoice row itself is already committed by
    then, so a failed send here never loses the invoice number).
    """
    if not settings.resend_api_key:
        raise RuntimeError("RESEND_API_KEY ist nicht konfiguriert.")

    sender_name = company_name or "NettoDesk"

    response = httpx.post(
        RESEND_API_URL,
        headers={
            "Authorization": f"Bearer {settings.resend_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": settings.resend_from_email,
            "to": [to_email],
            "subject": f"{'Stornorechnung' if cancellation else 'Deine Rechnung'} {invoice_number} — {sender_name}",
            "html": (
                (f"<p>Im Anhang findest du die Stornorechnung <strong>{invoice_number}</strong>.</p>"
                 if cancellation else
                 f"<p>Vielen Dank für deine Zahlung.</p>"
                 f"<p>Im Anhang findest du deine Rechnung <strong>{invoice_number}</strong> als PDF.</p>")
                + "<p>Bitte bewahre sie für deine Buchhaltung auf.</p>"
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
