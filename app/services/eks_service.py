"""
Figures for the Jobcenter form "Anlage EKS" (Bundesagentur für Arbeit, version
10/2026): income and costs per month of the approval period, under the form's own
line numbers. Built from the same dated bookings as the EÜR.

What the official filling notes say, and what follows from it here:
- income counts in the month it actually arrives; tax rules do not apply and
  there is no depreciation. The notes point to the bank statement - this report
  only knows the day the buyer paid, which is said to the user.
- sellers without VAT leave out all VAT lines. For VAT-liable sellers the form
  wants net amounts and the real VAT, which we only could estimate, so they are
  not supported.

The form has no line for refunds to buyers or marketplace fees; they go into
B14.5 and are listed in the remarks. Lines we have no data for stay empty.
"""

import csv
import io
from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.orm import Session

from app.services import euer_service
from app.services.export_service import safe_cell

FORM_VERSION = "Jobcenter-EKS 10/2026"
MAX_MONTHS = 12
ZERO = Decimal("0")

# (line, text as on the form, filled by us)
INCOME_LINES = [
    ("A1", "Betriebseinnahmen", True),
    ("A2", "Privatentnahmen von Waren", False),
    ("A3", "sonstige betriebliche Einnahmen", True),
    ("A4", "Zuwendungen von Dritten", False),
]
COST_PARTS = [
    [("B1", "Wareneinkauf", True), ("B2.1", "Personalkosten – Vollzeitbeschäftigte", False),
     ("B2.2", "Personalkosten – Teilzeitbeschäftigte", False),
     ("B2.3", "Personalkosten – geringfügig Beschäftigte", False),
     ("B2.4", "Personalkosten – mithelfende Familienangehörige", False),
     ("B3", "Betriebliche Raumkosten (inkl. Neben- und Energiekosten)", True),
     ("B4", "Betriebliche Versicherungen/Beiträge", False), ("B5", "Kosten für Werbung", True)],
    [("B6.1", "betriebliches Kfz – Steuern", False), ("B6.2", "betriebliches Kfz – Versicherung", False),
     ("B6.3", "betriebliches Kfz – laufende Betriebskosten", False),
     ("B6.4", "betriebliches Kfz – Reparaturkosten", False),
     ("B6.5", "privates Kfz – betriebliche Fahrten (0,10 € je km)", False),
     ("B7.1", "Reisekosten – Übernachtung", False), ("B7.2", "Reisekosten – Nebenkosten", False),
     ("B7.3", "Reisekosten – öffentliche Verkehrsmittel", False), ("B8", "Investitionen", False),
     ("B9", "Investitionen aus Zuwendungen Dritter", False), ("B10", "Büromaterial einschließlich Porto", True)],
    [("B11", "Telefonkosten", True), ("B12", "Beratungskosten", False), ("B13", "Fortbildungskosten", False),
     ("B14.1", "Reparaturkosten Anlagevermögen", False), ("B14.2", "Miete Einrichtung", False),
     ("B14.3", "Nebenkosten des Geldverkehrs", False), ("B14.4", "Betriebliche Abfallbeseitigung", False),
     ("B14.5", "weitere Betriebsausgaben", True), ("B15", "Schuldzinsen aus Anlagevermögen", False),
     ("B16", "Tilgung betrieblicher Darlehen", False)],
]

# Which form line each of our expense categories goes into; everything else is B14.5.
CATEGORY_LINES = {
    "Versand & Porto": "B10", "Büro & Material": "B10", "Strom": "B3",
    "Internet & Telefon": "B11", "Werbung": "B5",
}
BOOKING_LINES = {
    "sales": "A1", "fee_credits": "A3", "goods": "B1", "labels": "B10", "return_shipping": "B10",
    "refunds": "B14.5", "sale_fees": "B14.5",
}


def months_between(start: date, end: date) -> list[tuple[int, int]]:
    months, year, month = [], start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def report(db: Session, tenant_id, start: date, end: date) -> dict:
    rows, checks = euer_service.entries(db, tenant_id, start, end)
    months = months_between(start, end)
    index = {m: i for i, m in enumerate(months)}

    values: dict[str, list[Decimal]] = defaultdict(lambda: [ZERO] * len(months))
    parts: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)  # what a line is made of
    categories = set()
    for row in rows:
        category = row.get("category")
        if row["kind"] == "expenses":
            line = CATEGORY_LINES.get(category, "B14.5")
            what = category
            categories.add(category)
        else:
            line = BOOKING_LINES[row["kind"]]
            what = euer_service.LABELS[row["kind"]]
        values[line][index[(row["day"].year, row["day"].month)]] += row["amount"]
        parts[(line, what)] += row["amount"]

    def total_of(codes):
        return [sum((values[c][i] for c in codes if c in values), ZERO) for i in range(len(months))]

    def pack(code, name, amounts, kind):
        return {"line": code, "name": name, "kind": kind,
                "months": None if amounts is None else [f"{a:.2f}" for a in amounts],
                "total": None if amounts is None else f"{sum(amounts, ZERO):.2f}"}

    table = []

    def section(lines):
        for code, name, ours in lines:
            table.append(pack(code, name, values[code] if ours else None, "line"))

    section(INCOME_LINES)
    income = total_of([c for c, _, _ in INCOME_LINES])
    table.append(pack("", "Summe der Betriebseinnahmen", income, "sum"))
    seen: list[str] = []
    for number, lines in enumerate(COST_PARTS, 1):
        section(lines)
        seen += [c for c, _, _ in lines]
        if number < len(COST_PARTS):
            table.append(pack("", f"Zwischensumme Tabelle B – Teil {number}", total_of(seen), "subtotal"))
    costs = total_of(seen)
    profit = [i - c for i, c in zip(income, costs)]
    table.append(pack("", "Summe Betriebsausgaben", costs, "sum"))
    table.append(pack("", "Gewinn", profit, "profit"))

    average = (sum(profit, ZERO) / len(months)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {
        "form": FORM_VERSION, "start": start.isoformat(), "end": end.isoformat(),
        "months": [f"{m:02d}/{y}" for y, m in months],
        "rows": table,
        "profit_total": f"{sum(profit, ZERO):.2f}",
        "profit_monthly_average": f"{average:.2f}",
        "remarks": [{"line": line, "what": what, "amount": f"{amount:.2f}"}
                    for (line, what), amount in sorted(parts.items())],
        "warnings": _warnings(checks, categories, parts),
    }


def _warnings(checks: dict, categories: set, parts: dict) -> list[str]:
    out = ["Das Jobcenter zählt Einnahmen in dem Monat, in dem sie tatsächlich zufließen (Nachweis: Kontoauszug). "
           "Hier zählt der Tag der Zahlung des Käufers. Die eBay-Auszahlung auf das Konto kann in einem anderen "
           "Monat liegen und ist um Gebühren gekürzt. Bitte mit dem Kontoauszug vergleichen und beim Jobcenter klären."]
    has_sales = any(line == "A1" for line, _ in parts)
    if has_sales and not any(line == "B1" for line, _ in parts):
        out.append("Verkäufe ohne Wareneinkauf: Bitte die bezahlten Lieferantenrechnungen unter „Einkäufe“ erfassen.")
    if checks["purchases_without_paid_date"]:
        out.append(f"{checks['purchases_without_paid_date']} Einkauf/Einkäufe ohne „Bezahlt am“: "
                   "es wurde das Rechnungsdatum verwendet.")
    if checks["orders_with_estimated_dates"]:
        out.append(f"Bei {checks['orders_with_estimated_dates']} Bestellung(en) zählt für einzelne Beträge das "
                   "Bestelldatum, weil das Zahlungsdatum fehlt.")
    if checks["sales_without_fees"]:
        out.append(f"Bei {checks['sales_without_fees']} eBay-Verkauf/Verkäufen sind die Gebühren noch unbekannt.")
    if checks["expenses_without_receipt"]:
        out.append(f"{checks['expenses_without_receipt']} Ausgabe(n) ohne hochgeladenen Beleg. "
                   "Das Jobcenter verlangt Nachweise.")
    if "Internet & Telefon" in categories:
        out.append("B11: Nur betriebliche Telefonkosten angeben. Ohne getrennten Anschluss können laut "
                   "Ausfüllhinweisen 50 % anerkannt werden. Hier steht der erfasste Betrag ungekürzt; "
                   "Internetkosten bitte selbst prüfen.")
    if "Strom" in categories:
        out.append("B3: Bei Nutzung der Wohnung nur den betrieblichen Anteil angeben. Hier steht der erfasste "
                   "Betrag ungekürzt.")
    if "Fahrtkosten" in categories:
        out.append("Fahrtkosten stehen in B14.5. Bitte selbst in B6 oder B7 verschieben (Fahrtenbuch nötig).")
    if checks["large_expenses"]:
        out.append("Anschaffungen von Geräten gehören in B8 (Investitionen); ungeplante größere Ausgaben sollen "
                   "dem Jobcenter sofort gemeldet werden. Bitte prüfen: " + "; ".join(checks["large_expenses"][:10]))
    out.append("Leere Zeilen kennt NettoDesk nicht (z. B. A2, A4, Kfz, Reisen): bitte selbst ausfüllen oder 0 "
               "eintragen. Summen und Gewinn ändern sich dann.")
    return out


def to_csv(data: dict) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow([f"Anlage EKS – Hilfsrechnung ({data['form']})", f"{data['start']} bis {data['end']}"])
    writer.writerow(["Zeile", "Bezeichnung"] + data["months"] + ["Summe"])
    for row in data["rows"]:
        cells = row["months"] + [row["total"]] if row["months"] is not None else [""] * (len(data["months"]) + 1)
        writer.writerow([row["line"], row["name"]] + [c.replace(".", ",") for c in cells])
    writer.writerow([])
    writer.writerow(["Anmerkungen: Zusammensetzung der Zeilen"])
    for remark in data["remarks"]:
        writer.writerow([remark["line"], safe_cell(remark["what"]), remark["amount"].replace(".", ",")])
    writer.writerow([])
    writer.writerow(["Hinweise"])
    for warning in data["warnings"]:
        writer.writerow([safe_cell(warning)])
    return "﻿" + buffer.getvalue()
