from app.services import ebay_order_service


class _Response:
    status_code = 200

    def __init__(self, orders):
        self._orders = orders

    def json(self):
        return {"orders": self._orders}


def test_all_pages_of_orders_are_fetched(monkeypatch):
    everything = [{"orderId": str(n)} for n in range(450)]
    asked = []

    def fake_get(url, headers, params, timeout):
        asked.append(params["offset"])
        return _Response(everything[params["offset"]:params["offset"] + params["limit"]])

    monkeypatch.setattr(ebay_order_service.httpx, "get", fake_get)
    assert ebay_order_service._fetch_orders("https://example.test", "token") == everything
    assert asked == [0, 200, 400]
