"""Listing editor: pictures and per-variation price / quantity are sent to eBay in one call."""

from unittest.mock import patch

import pytest

from app.models.ebay_account import EbayAccount
from app.services import ebay_listing_editor as editor
from app.services import ebay_listing_service

OLD = "https://i.ebayimg.com/images/g/old/s-l1600.jpg"
NEW = "https://x.supabase.co/storage/v1/object/public/product-images/new.jpg"
HOSTED = "https://i.ebayimg.com/images/g/new/s-l1600.jpg"
ITEM = "920105301438"


def _info():
    return {
        "item_id": ITEM, "quantity": "9", "sold": "2", "price": "3.00",
        "pictures": [OLD], "picture_name": None, "picture_sets": {},
        "variations": [
            {"sku": None, "specifics": {"Farbe": "Rot"}, "price": "3.00", "quantity": "5", "sold": "2"},
            {"sku": "S-2", "specifics": {"Farbe": "Blau"}, "price": "4.00", "quantity": "4", "sold": "0"},
            {"sku": None, "specifics": {"Farbe": "Weiß & Co"}, "price": "5.00", "quantity": "1", "sold": "1"},
        ],
    }


class Resp:
    def __init__(self, status=200, text="", data=None, headers=None):
        self.status_code, self.text, self._data, self.headers = status, text, data, headers or {}

    def json(self):
        if self._data is None:
            raise ValueError("no body")
        return self._data


OK = '<r xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack></r>'


def _fake_ebay(monkeypatch, answer=OK):
    calls = []

    def post(url, **kw):
        calls.append((url, kw))
        if "create_image_from_url" in url:
            return Resp(201, data={"imageUrl": HOSTED})
        return Resp(200, answer)

    monkeypatch.setattr(editor.httpx, "post", post)
    monkeypatch.setattr(editor.ebay_oauth_service, "get_valid_access_token", lambda a, d: "tok")
    return calls


def _account():
    return EbayAccount(ebay_user_id="me", environment="PRODUCTION")


def test_quantity_is_sent_as_available_without_adding_sold(monkeypatch):
    calls = _fake_ebay(monkeypatch)
    editor.apply(_account(), None, ITEM, _info(), [
        {"sku": None, "specifics": {"Farbe": "Rot"}, "price": None, "quantity": 7, "quantity_seen": 3}],
        None, None, None)
    xml = calls[0][1]["content"].decode()
    assert "<StartPrice>3.00</StartPrice><Quantity>7</Quantity>" in xml
    assert "<Value>Rot</Value>" in xml and "Blau" not in xml  # only the changed variation
    assert "<PictureDetails>" not in xml and "<Pictures>" not in xml


def test_price_only_keeps_quantity_and_never_sends_zero_for_sold_out():
    todo = editor.plan(_info(), [
        {"sku": "S-2", "specifics": {"Farbe": "Blau"}, "price": 4.5},
        {"sku": None, "specifics": {"Farbe": "Weiß & Co"}, "price": 6},
    ], None, None, None)
    xml = editor.build_xml(ITEM, todo, None, None, None)
    assert "<SKU>S-2</SKU><StartPrice>4.5</StartPrice><Quantity>4</Quantity>" in xml
    assert "<StartPrice>6</StartPrice><VariationSpecifics>" in xml  # sold out: no quantity
    assert "Weiß &amp; Co" in xml


def test_new_pictures_are_copied_to_ebay_once(monkeypatch):
    calls = _fake_ebay(monkeypatch)
    sent = editor.apply(_account(), None, ITEM, _info(), [], [NEW, OLD], "Farbe", {"Rot": [NEW], "Blau": []})
    uploads = [kw["json"] for url, kw in calls if "create_image_from_url" in url]
    assert uploads == [{"imageUrl": NEW}]
    xml = calls[-1][1]["content"].decode()
    assert f"<PictureDetails><PictureURL>{HOSTED}</PictureURL><PictureURL>{OLD}</PictureURL></PictureDetails>" in xml
    assert "<VariationSpecificName>Farbe</VariationSpecificName>" in xml
    assert "<VariationSpecificValue>Rot</VariationSpecificValue>" in xml and "Blau" not in xml
    assert sent["pictures"] == [HOSTED, OLD] and NEW not in xml


def test_picture_address_from_location_header(monkeypatch):
    base = editor.MEDIA_URL["PRODUCTION"]
    monkeypatch.setattr(editor.httpx, "post", lambda url, **kw: Resp(201, headers={"Location": base + "/abc"}))
    monkeypatch.setattr(editor.httpx, "get", lambda url, **kw: Resp(200, data={"imageUrl": HOSTED}))
    assert editor.host_on_ebay("PRODUCTION", "tok", NEW) == HOSTED
    monkeypatch.setattr(editor.httpx, "post", lambda url, **kw: Resp(201, headers={"Location": "https://evil.test/a"}))
    with pytest.raises(ValueError):
        editor.host_on_ebay("PRODUCTION", "tok", NEW)


@pytest.mark.parametrize("changes, pictures, name, sets", [
    ([{"sku": None, "specifics": {"Farbe": "Gelb"}, "price": 3}], None, None, None),  # unknown variation
    ([{"sku": None, "specifics": {"Farbe": "Rot"}, "quantity": 5, "quantity_seen": 4}], None, None, None),  # sold meanwhile
    ([{"sku": None, "specifics": {"Farbe": "Rot"}, "price": 0}], None, None, None),
    ([{"sku": None, "specifics": {"Farbe": "Rot"}, "quantity": -1}], None, None, None),
    ([], [], None, None),
    ([], [OLD] * 25, None, None),
    ([], None, "Farbe", {"Gelb": [OLD]}),
    ([], None, "Farbe", {"Rot": [OLD] * 13}),
    ([], None, "Farbe", {"Rot": []}),
])
def test_bad_changes_are_refused(changes, pictures, name, sets):
    with pytest.raises(ValueError):
        editor.plan(_info(), changes, pictures, name, sets)


def test_totals_after_changes():
    info = _info()
    todo = editor.plan(info, [{"sku": None, "specifics": {"Farbe": "Rot"}, "price": 6, "quantity": 10}],
                       None, None, None)
    quantity, lowest = editor.totals(info, todo)
    assert quantity == 14 and str(lowest) == "4.00"  # 10 + 4 + 0


def test_get_item_reads_pictures(monkeypatch):
    answer = (
        '<r xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack><Item><Title>T</Title>'
        f"<PictureDetails><PictureURL>{OLD}</PictureURL><PictureURL>{HOSTED}</PictureURL></PictureDetails>"
        "<Variations><Variation><StartPrice>3.0</StartPrice><Quantity>2</Quantity><VariationSpecifics>"
        "<NameValueList><Name>Farbe</Name><Value>Rot</Value></NameValueList></VariationSpecifics></Variation>"
        "<Pictures><VariationSpecificName>Farbe</VariationSpecificName><VariationSpecificPictureSet>"
        f"<VariationSpecificValue>Rot</VariationSpecificValue><PictureURL>{OLD}</PictureURL>"
        "</VariationSpecificPictureSet></Pictures></Variations></Item></r>")
    monkeypatch.setattr(ebay_listing_service.ebay_oauth_service, "get_valid_access_token", lambda a, d: "tok")
    monkeypatch.setattr(ebay_listing_service.httpx, "post", lambda url, **kw: Resp(200, answer))
    info = ebay_listing_service.get_item_variations(_account(), None, ITEM)
    assert info["pictures"] == [OLD, HOSTED]
    assert info["picture_name"] == "Farbe" and info["picture_sets"] == {"Rot": [OLD]}
    assert editor.listing_view(info)["specific_values"] == {"Farbe": ["Rot"]}


def test_different_variation_prices_are_not_flattened(monkeypatch):
    monkeypatch.setattr(ebay_listing_service, "get_item_variations", lambda a, d, i: _info())
    monkeypatch.setattr(ebay_listing_service.httpx, "post", lambda *a, **kw: pytest.fail("nothing may be sent"))
    message = ebay_listing_service._revise_variation_listing(_account(), None, "tok", (ITEM, 3, None))
    assert "unterschiedliche Preise" in message


def _product(client, headers):
    return client.post("/products", headers=headers, json={
        "sku": ITEM, "title": "x", "selling_price": 3, "stock_quantity": 7}).json()["id"]


def _stored(client, headers, pid):
    return client.get(f"/products/{pid}", headers=headers).json()


def test_save_updates_nettodesk_only_after_ebay_accepted(client, register, monkeypatch):
    headers, _ = register()
    pid = _product(client, headers)
    body = {"item_id": ITEM, "pictures": [NEW],
            "variations": [{"specifics": {"Farbe": "Rot"}, "price": "2.50", "quantity": 8, "quantity_seen": 3}]}
    monkeypatch.setattr(ebay_listing_service, "get_item_variations", lambda a, d, i: _info())
    with patch("app.routers.ebay._connected_accounts") as accounts:
        accounts.return_value.all.return_value = [_account()]

        _fake_ebay(monkeypatch, '<r xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Failure</Ack>'
                                "<Errors><LongMessage>nein</LongMessage></Errors></r>")
        refused = client.post(f"/ebay/listing-editor/{pid}", headers=headers, json=body)
        assert refused.status_code == 502 and "nein" in refused.json()["detail"]
        assert _stored(client, headers, pid)["stock_quantity"] == 7

        _fake_ebay(monkeypatch)
        saved = client.post(f"/ebay/listing-editor/{pid}", headers=headers, json=body)
        assert saved.status_code == 200, saved.text
    product = _stored(client, headers, pid)
    assert product["stock_quantity"] == 12 and float(product["selling_price"]) == 2.5
    assert product["image_url"] == HOSTED


def test_editor_refuses_foreign_item_and_foreign_product(client, register, monkeypatch):
    headers, _ = register()
    pid = _product(client, headers)
    other, _ = register()
    calls = _fake_ebay(monkeypatch)
    with patch("app.routers.ebay._connected_accounts") as accounts:
        accounts.return_value.all.return_value = [_account()]
        assert client.get(f"/ebay/listing-editor/{pid}", headers=other).status_code == 404
        wrong = client.post(f"/ebay/listing-editor/{pid}", headers=headers,
                            json={"item_id": "111111111111", "pictures": [OLD]})
        assert wrong.status_code == 502
    assert not calls
    assert client.get(f"/ebay/listing-editor/{pid}").status_code == 401
