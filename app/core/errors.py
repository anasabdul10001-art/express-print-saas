"""Input errors as one readable German sentence.

FastAPI's default puts a list of objects into `detail`, which the pages showed as
"[object Object]" - and it repeats the submitted values, a password included.
"""

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

FIELDS = {
    "email": "E-Mail", "password": "Passwort", "new_password": "Neues Passwort", "company_name": "Firmenname",
    "amount": "Betrag", "description": "Beschreibung", "expense_date": "Datum", "supplier": "Lieferant",
    "invoice_number": "Rechnungsnummer", "invoice_date": "Rechnungsdatum", "paid_date": "Bezahlt am",
    "delivery_date": "Lieferdatum", "title": "Titel", "sku": "SKU", "purchase_price": "Einkaufspreis",
    "selling_price": "Verkaufspreis", "shipping_cost": "Versandkosten", "stock_quantity": "Lagerbestand",
    "quantity": "Menge", "line_total": "Warenwert", "extra_costs": "Nebenkosten", "vat_amount": "Umsatzsteuer",
    "start": "Von", "end": "Bis", "legal_name": "Name / Firma", "address": "Anschrift",
}

SIMPLE = {
    "missing": "fehlt",
    "greater_than": "muss größer als {gt} sein",
    "greater_than_equal": "muss mindestens {ge} sein",
    "less_than": "muss kleiner als {lt} sein",
    "less_than_equal": "darf höchstens {le} sein",
    "string_too_short": "ist zu kurz (mindestens {min_length} Zeichen)",
    "string_too_long": "ist zu lang (höchstens {max_length} Zeichen)",
    "too_short": "enthält zu wenige Einträge",
    "too_long": "enthält zu viele Einträge",
    "decimal_max_places": "hat zu viele Nachkommastellen (höchstens {decimal_places})",
    "int_parsing": "muss eine ganze Zahl sein",
    "int_from_float": "muss eine ganze Zahl sein",
    "float_parsing": "muss eine Zahl sein",
    "decimal_parsing": "muss eine Zahl sein",
    "date_parsing": "ist kein gültiges Datum",
    "date_from_datetime_parsing": "ist kein gültiges Datum",
}

OWN_PREFIX = "Value error, "


def _sentence(error: dict) -> str:
    message = str(error.get("msg", ""))
    # e.g. ("body", "lines", 0, "quantity") -> "quantity"
    names = [part for part in error.get("loc", ())[1:] if isinstance(part, str)]
    name = names[-1] if names else ""
    label = FIELDS.get(name, name)
    if "valid email address" in message:
        return "Bitte eine gültige E-Mail-Adresse eingeben."
    if error.get("type") == "value_error" and message.startswith(OWN_PREFIX):
        return message[len(OWN_PREFIX):]  # a message written in this app
    if error.get("type") == "json_invalid":
        return "Die Anfrage konnte nicht gelesen werden."
    try:
        text = SIMPLE[error.get("type")].format(**(error.get("ctx") or {}))
    except KeyError:
        text = "ist ungültig"
    return f"{label}: {text}." if label else f"Eingabe {text}."


def readable(errors) -> str:
    """All problems, each once, in the order they were found."""
    return " ".join(dict.fromkeys(_sentence(e) for e in errors)) or "Bitte die Eingaben prüfen."


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": readable(exc.errors())})
