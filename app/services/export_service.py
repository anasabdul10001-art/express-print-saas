"""
One ZIP for the tax advisor or the archive: every EÜR booking of a period, the
uploaded supplier invoices and expense receipts behind them, and the invoices
issued to buyers in that period. It is a copy for handing over, not an archive
that proves nothing was changed.

The ZIP is written to a temporary file and the documents are read one at a time,
so a large export does not have to fit into memory.
"""

import csv
import io
import re
import tempfile
import zipfile
from datetime import date

from sqlalchemy.orm import Session

from app.models.expense import ExpenseReceipt
from app.models.purchase import Purchase
from app.models.sales_invoice import SalesInvoice
from app.services import euer_service

MAX_DOCUMENT_BYTES = 150 * 1024 * 1024
EXTENSIONS = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg"}


class TooLarge(Exception):
    pass


def safe_cell(text: str) -> str:
    """A spreadsheet must show typed text, never run it as a formula."""
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _file_name(day: date, number: int, name: str, content_type: str) -> str:
    """Only letters, digits, dot, dash and underscore: a typed name can never leave its folder."""
    stem = name.rsplit(".", 1)[0]
    stem = re.sub(r"[^\w.-]+", "_", stem).strip("._")[:60] or "beleg"
    return f"{day.isoformat()}_{number:04d}_{stem}{EXTENSIONS.get(content_type, '')}"


def _amount(value) -> str:
    return f"{value:.2f}".replace(".", ",")


def bookings_csv(rows: list[dict], documents: dict | None = None) -> str:
    """`documents` maps a booking's source to its files in the ZIP; None leaves the column out."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    header = ["Zahlungsdatum", "Art", "Position", "Betrag (€)", "Beleg / Bezug"]
    writer.writerow(header + (["Belegdatei"] if documents is not None else []))
    for row in rows:
        income = row["kind"] in euer_service.INCOME
        line = [row["day"].strftime("%d.%m.%Y"), "Einnahme" if income else "Ausgabe",
                euer_service.LABELS[row["kind"]], _amount(row["amount"]), safe_cell(row["text"])]
        if documents is not None:
            line.append(", ".join(documents.get(row["source"], [])))
        writer.writerow(line)
    return "﻿" + buffer.getvalue()


def _overview_csv(report: dict) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    columns = report["periods"] + [{"label": "Summe", **report["total"]}]
    writer.writerow(["Position"] + [c["label"] for c in columns])
    names = dict(report["labels"], income="Betriebseinnahmen", costs="Betriebsausgaben", surplus="Überschuss / Verlust")
    for key in report["income_keys"] + ["income"] + report["cost_keys"] + ["costs", "surplus"]:
        writer.writerow([names[key]] + [c[key].replace(".", ",") for c in columns])
    return "﻿" + buffer.getvalue()


def build(db: Session, tenant_id, start: date, end: date, vat_mode: str):
    """Returns an open temporary file positioned at the start of the finished ZIP."""
    rows, _ = euer_service.entries(db, tenant_id, start, end)
    report = euer_service.report(db, tenant_id, start, end, "quarter", vat_mode)
    target = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    size = 0
    documents: dict = {}
    missing: list[str] = []
    counter = 0

    try:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            def put(path: str, data: bytes) -> None:
                nonlocal size
                size += len(data)
                if size > MAX_DOCUMENT_BYTES:
                    raise TooLarge
                archive.writestr(path, data)

            def attach(source, folder, day, name, content_type, data):
                nonlocal counter
                counter += 1
                path = f"{folder}/{_file_name(day, counter, name, content_type)}"
                put(path, data)
                documents.setdefault(source, []).append(path)

            for row in rows:
                source = row["source"]
                if source is None or source in documents:
                    continue
                kind, ident = source
                if kind == "purchase":
                    found = db.query(Purchase.file_name, Purchase.content_type, Purchase.file_data).filter(
                        Purchase.id == ident, Purchase.tenant_id == tenant_id).one()
                    attach(source, "belege/einkauf", row["day"], *found)
                    continue
                ids = [i for (i,) in db.query(ExpenseReceipt.id).filter(
                    ExpenseReceipt.expense_id == ident, ExpenseReceipt.tenant_id == tenant_id)
                    .order_by(ExpenseReceipt.created_at)]
                for receipt_id in ids:
                    found = db.query(ExpenseReceipt.file_name, ExpenseReceipt.content_type,
                                     ExpenseReceipt.file_data).filter(ExpenseReceipt.id == receipt_id).one()
                    attach(source, "belege/ausgaben", row["day"], *found)
                if not ids:
                    missing.append(f"{row['day'].strftime('%d.%m.%Y')}  {_amount(row['amount'])} €  {row['text']}")

            issued = [i for (i,) in db.query(SalesInvoice.id).filter(
                SalesInvoice.tenant_id == tenant_id, SalesInvoice.issue_date >= start, SalesInvoice.issue_date <= end)
                .order_by(SalesInvoice.issue_date, SalesInvoice.number)]
            for invoice_id in issued:
                number, pdf, xml = db.query(SalesInvoice.number, SalesInvoice.pdf, SalesInvoice.xml).filter(
                    SalesInvoice.id == invoice_id).one()
                stem = re.sub(r"[^\w.-]+", "_", number).strip("._") or "rechnung"
                put(f"ausgangsrechnungen/{stem}.pdf", pdf)
                if xml:
                    put(f"ausgangsrechnungen/{stem}.xml", xml)

            archive.writestr("euer-buchungen.csv", bookings_csv(rows, documents))
            archive.writestr("euer-uebersicht.csv", _overview_csv(report))
            notes = [f"Export {start.strftime('%d.%m.%Y')} bis {end.strftime('%d.%m.%Y')} (nach Zahlungsdatum)",
                     "Vorbereitung, keine Steuererklärung.", "",
                     f"Buchungen: {len(rows)}",
                     f"Ausgangsrechnungen (nach Ausstellungsdatum): {len(issued)}", "", "Hinweise:"]
            notes += [f"- {w}" for w in report["warnings"]] or ["- keine"]
            notes += ["", "Selbst erfasste Ausgaben ohne hochgeladenen Beleg (dazu auch die von eBay "
                          "importierten Gebühren: deren Belege sind die eBay-Rechnungen und liegen nicht bei):"]
            notes += [f"- {m}" for m in missing] or ["- keine"]
            archive.writestr("LIESMICH.txt", "﻿" + "\r\n".join(notes) + "\r\n")
    except BaseException:
        target.close()
        raise
    target.seek(0)
    return target
