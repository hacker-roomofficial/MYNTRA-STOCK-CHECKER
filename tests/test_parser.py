import json

from stockwatch.models import Item, Status
from stockwatch.parser import parse_gateway_json, parse_html, parse_pdp

SIZES = [
    {"label": "S", "available": True, "sizeSellerData": [{"availableCount": 3}]},
    {"label": "M", "available": False, "sizeSellerData": [{"availableCount": 0}]},
    {"label": "L", "available": True},
    {"label": "XL", "available": False},
]


def test_gateway_all_sizes_watched():
    r = parse_gateway_json(Item("1"), {"style": {"id": 1, "name": "Shirt", "brand": {"name": "B"}, "sizes": SIZES}})
    assert r.status == Status.PARTIAL_OOS
    assert r.oos_sizes == ["M", "XL"] and r.in_sizes == ["S", "L"]
    assert r.brand == "B" and r.sizes[0].qty == 3


def test_watch_list_only_cares_about_chosen_sizes():
    pay = {"style": {"id": 1, "sizes": SIZES}}
    assert parse_gateway_json(Item("1", ("S", "L")), pay).status == Status.OK
    assert parse_gateway_json(Item("1", ("m",)), pay).status == Status.ALL_OOS  # case-insensitive
    r = parse_gateway_json(Item("1", ("S", "M")), pay)
    assert r.status == Status.PARTIAL_OOS and r.oos_sizes == ["M"]


def test_watched_size_missing_from_page_counts_as_oos():
    r = parse_gateway_json(Item("1", ("XXL",)), {"style": {"id": 1, "sizes": SIZES}})
    assert r.status == Status.ALL_OOS and r.oos_sizes == ["XXL"]


def test_page_without_any_sizes_is_all_oos():
    r = parse_gateway_json(Item("1"), {"style": {"id": 1, "sizes": []}})
    assert r.status == Status.ALL_OOS


def test_html_window_myx():
    blob = json.dumps({"pdpData": {"id": 7, "name": "N", "brand": {"name": "B"}, "sizes": SIZES}})
    html = f"<html><script>window.__myx = {blob}</script><body>x</body></html>"
    r = parse_html(Item("7"), html)
    assert r.status == Status.PARTIAL_OOS and r.name == "N"


def test_garbage_is_none_not_crash():
    assert parse_html(Item("1"), "<html>Access Denied</html>") is None
    assert parse_gateway_json(Item("1"), "nope") is None
    assert parse_pdp(Item("1"), {}) is None
