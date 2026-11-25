"""
Writes the figures of eks_service into the Bundesagentur's own form
(app/assets/anlage_eks_2026-10.pdf, version Jobcenter-EKS 10/2026).

Filled: the approval period, the month headings, the tables A and B and the
remark boxes, plus sections A, B and D if the seller saved those details. The
other questions, table C and the signatures stay empty for the person to fill in,
and the file remains editable. Nothing is sent.
"""

import io
import textwrap
from datetime import date
from pathlib import Path

from pypdf import PdfReader, PdfWriter

TEMPLATE = Path(__file__).resolve().parent.parent / "assets" / "anlage_eks_2026-10.pdf"
MONTH_COLUMNS = 6
MONTH_NAMES = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September",
               "Oktober", "November", "Dezember"]
CHECKED = "/selektiert"

# form line -> (field table, row number in that table)
LINE_FIELDS = {
    "A1": ("TabA", 2), "A2": ("TabA", 3), "A3": ("TabA", 4), "A4": ("TabA", 5),
    "B1": ("TabB1", 2), "B2.1": ("TabB1", 3), "B2.2": ("TabB1", 4), "B2.3": ("TabB1", 5),
    "B2.4": ("TabB1", 6), "B3": ("TabB1", 7), "B4": ("TabB1", 8), "B5": ("TabB1", 9),
    "B6.1": ("TabB2", 3), "B6.2": ("TabB2", 4), "B6.3": ("TabB2", 5), "B6.4": ("TabB2", 6),
    "B6.5": ("TabB2", 8), "B7.1": ("TabB2", 9), "B7.2": ("TabB2", 10), "B7.3": ("TabB2", 11),
    "B8": ("TabB2", 12), "B9": ("TabB2", 13), "B10": ("TabB2", 14),
    "B11": ("TabB3", 3), "B12": ("TabB3", 4), "B13": ("TabB3", 5), "B14.1": ("TabB3", 6),
    "B14.2": ("TabB3", 7), "B14.3": ("TabB3", 8), "B14.4": ("TabB3", 9), "B14.5": ("TabB3", 10),
    "B15": ("TabB3", 11), "B16": ("TabB3", 12),
}
# the unnamed total rows, by the name eks_service gives them
SUM_FIELDS = {
    "Summe der Betriebseinnahmen": [("TabA", 9)],
    # each subtotal is also carried over to the top of the next page
    "Zwischensumme Tabelle B – Teil 1": [("TabB1", 10), ("TabB2", 2)],
    "Zwischensumme Tabelle B – Teil 2": [("TabB2", 15), ("TabB3", 2)],
    "Summe Betriebsausgaben": [("TabB3", 15)],
    "Gewinn": [("TabB3", 16)],
}
# remark box -> (the lines it explains, characters per line, lines that fit)
REMARK_BOXES = {
    # the official file really has a carriage return at the end of this field name
    "txtAreaTabaAnmerkungen\r": (("A",), 150, 7),
    "txtAreaTabB1Anmerkungen": (("B1", "B2", "B3", "B4", "B5"), 150, 9),
    "txtAreaTabB3Anmerkungen": (("B10", "B11", "B14"), 150, 4),
}


class FormPeriodError(ValueError):
    pass


def _german(amount: str) -> str:
    whole, cents = amount.split(".")
    sign = "-" if whole.startswith("-") else ""
    return f"{sign}{int(whole.lstrip('-')):,}".replace(",", ".") + "," + cents


def _line_of(code: str, boxes_prefixes: tuple) -> bool:
    main = code.split(".")[0]
    return code[0] == "A" and "A" in boxes_prefixes or main in boxes_prefixes


def _remarks(data: dict) -> tuple[dict, bool]:
    """Text per remark box, and whether everything fitted."""
    texts, complete = {}, True
    for field, (prefixes, width, height) in REMARK_BOXES.items():
        grouped: dict[str, list[str]] = {}
        for remark in data["remarks"]:
            if _line_of(remark["line"], prefixes):
                what = " ".join(str(remark["what"]).split())
                grouped.setdefault(remark["line"], []).append(f"{what} {_german(remark['amount'])} €")
        lines = []
        for code, parts in grouped.items():
            lines += textwrap.wrap(f"{code} (Summe im Zeitraum): " + "; ".join(parts), width)
        if len(lines) > height:
            complete = False
            lines = lines[:height - 1] + ["Weitere Einzelheiten siehe beigefügte Aufstellung."]
        if lines:
            texts[field] = "\n".join(lines)
    return texts, complete


def values_for(data: dict, final: bool, with_sums: bool) -> tuple[dict, bool]:
    start, end = date.fromisoformat(data["start"]), date.fromisoformat(data["end"])
    if len(data["months"]) > MONTH_COLUMNS:
        raise FormPeriodError("Das Formular hat 6 Monatsspalten. Bitte höchstens 6 Monate wählen.")
    values = {
        "dateBewilligungszeitraumVon": start.strftime("%d.%m.%Y"),
        "dateBewilligungszeitraumBis": end.strftime("%d.%m.%Y"),
    }
    for column, label in enumerate(data["months"], 2):
        month, year = label.split("/")
        values[f"txtfTab_Z1_S{column}"] = f"{MONTH_NAMES[int(month) - 1]} {year}"
    for row in data["rows"]:
        if row["months"] is None:
            continue
        if row["line"]:
            targets = [LINE_FIELDS[row["line"]]]
        else:
            targets = SUM_FIELDS[row["name"]] if with_sums else []
        for table, number in targets:
            for column, amount in enumerate(row["months"], 2):
                values[f"numf{table}_Z{number}_S{column}"] = _german(amount)
    texts, complete = _remarks(data)
    values.update(texts)
    if final:
        values["chbxAngabenZeitAbschl"] = CHECKED
        values["chbxTaetigkeitAngabenAbschl"] = CHECKED
    return values, complete


def fill(data: dict, final: bool, with_sums: bool, details: dict | None = None) -> tuple[bytes, bool]:
    """The filled form, and False if the remarks did not fit into their boxes."""
    values, complete = values_for(data, final, with_sums)
    values.update(details or {})  # sections A, B and D, if the seller saved them
    writer = PdfWriter(clone_from=PdfReader(TEMPLATE))
    for page in writer.pages:
        writer.update_page_form_field_values(page, values, auto_regenerate=False)
    writer.set_need_appearances_writer(True)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue(), complete
