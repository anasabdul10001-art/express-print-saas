"""The official Anlage EKS filled with the figures."""
import io

import pytest
from pypdf import PdfReader

from app.services import eks_pdf_service
from tests.test_eks import HALF, _fill

PAST = {"start": "2026-07-01", "end": "2026-10-31"}


def _fields(content: bytes) -> dict:
    return {name: field.get("/V") for name, field in PdfReader(io.BytesIO(content)).get_fields().items()}


def _form(client, headers, **params):
    return client.get("/accounting/eks/form.pdf", headers=headers, params=params)


def test_every_line_has_a_field_that_exists_in_the_official_form():
    existing = set(PdfReader(eks_pdf_service.TEMPLATE).get_fields())
    from app.services.eks_service import COST_PARTS, INCOME_LINES
    lines = [c for c, _, _ in INCOME_LINES] + [c for part in COST_PARTS for c, _, _ in part]
    assert set(lines) == set(eks_pdf_service.LINE_FIELDS)
    targets = list(eks_pdf_service.LINE_FIELDS.values()) + [t for v in eks_pdf_service.SUM_FIELDS.values() for t in v]
    assert len(targets) == len(set(targets))
    for table, number in targets:
        for column in range(2, 8):
            assert f"numf{table}_Z{number}_S{column}" in existing
    assert set(eks_pdf_service.REMARK_BOXES) <= existing


def test_the_amounts_are_written_into_the_right_boxes(client, register):
    headers, _ = register()
    _fill(client, headers)
    r = _form(client, headers, **HALF)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf" and r.headers["x-remarks-complete"] == "1"
    f = _fields(r.content)
    assert (f["dateBewilligungszeitraumVon"], f["dateBewilligungszeitraumBis"]) == ("01.07.2026", "31.12.2026")
    assert f["txtfTab_Z1_S2"] == "Juli 2026" and f["txtfTab_Z1_S7"] == "Dezember 2026"
    assert f["numfTabA_Z2_S2"] == "100,00"                      # A1 July
    assert f["numfTabB1_Z2_S3"] == "20,00"                      # B1 August
    assert f["numfTabB2_Z14_S2"] == "4,00" and f["numfTabB2_Z14_S4"] == "2,00"   # B10
    assert f["numfTabB3_Z3_S4"] == "30,00"                      # B11
    assert f["numfTabB3_Z10_S2"] == "16,00"                     # B14.5 July: fees + refund
    assert f["numfTabB1_Z10_S4"] == f["numfTabB2_Z2_S4"] == "15,00"  # subtotal and its carry-over
    assert f["numfTabB3_Z16_S2"] == "80,00" and f["numfTabB3_Z16_S3"] == "-20,00"
    assert "Verpackung 7,00 €" in f["txtAreaTabB3Anmerkungen"]
    assert "Werbung 3,00 €" in f["txtAreaTabB1Anmerkungen"]
    # nothing invented: lines without data, personal data and signatures stay empty
    for name in ("numfTabA_Z3_S2", "numfTabB2_Z3_S2", "txtfPersonVorname", "txtfBGNr", "dateUnterschriftPerson"):
        assert not f.get(name), name
    assert f.get("chbxAngabenZeitAbschl") in (None, "/Off")


def test_without_sums_and_marked_as_final(client, register):
    headers, _ = register()
    _fill(client, headers)
    f = _fields(_form(client, headers, **PAST, final=True, sums=False).content)
    assert f["chbxAngabenZeitAbschl"] == "/selektiert" == f["chbxTaetigkeitAngabenAbschl"]
    assert f.get("chbxAngabenZeitVorl") in (None, "/Off")
    assert not f.get("numfTabB3_Z16_S2") and not f.get("numfTabA_Z9_S2")
    assert f["numfTabA_Z2_S2"] == "100,00" and not f.get("txtfTab_Z1_S6")


@pytest.mark.parametrize("params, word", [
    ({"start": "2026-01-01", "end": "2026-07-31"}, "6 Monate"),
    ({"start": "2026-07-15", "end": "2026-12-31"}, "ganze Kalendermonate"),
    ({"start": "2026-07-01", "end": "2026-12-30"}, "ganze Kalendermonate"),
    ({"start": "2099-01-01", "end": "2099-06-30", "final": True}, "vorbei"),
])
def test_periods_the_form_cannot_take_are_refused(client, register, params, word):
    headers, _ = register()
    r = _form(client, headers, **params)
    assert r.status_code == 400 and word in r.json()["detail"]


def test_login_is_needed(client):
    assert _form(client, {}, **HALF).status_code in (401, 403, 422)


def test_remarks_that_do_not_fit_are_reported_not_silently_cut():
    data = {"remarks": [{"line": "B14.5", "what": f"Kategorie {n} " + "x" * 60, "amount": "1.00"} for n in range(30)]}
    texts, complete = eks_pdf_service._remarks(data)
    assert not complete and texts["txtAreaTabB3Anmerkungen"].endswith("Aufstellung.")
    assert len(texts["txtAreaTabB3Anmerkungen"].split("\n")) == 4


def test_amounts_are_written_the_german_way():
    assert eks_pdf_service._german("1234567.50") == "1.234.567,50"
    assert eks_pdf_service._german("-0.05") == "-0,05"
