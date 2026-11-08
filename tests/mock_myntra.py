"""Local fake Myntra (JSON gateway + product HTML) and fake n8n webhook for testing."""
from __future__ import annotations

from aiohttp import web


def style_payload(sid: int) -> dict | None:
    """Deterministic fake catalogue:
    sid % 211 == 0  -> 404 (delisted)
    sid % 97  == 0  -> every size out of stock
    sid % 10  == 0  -> size M out of stock
    """
    if sid % 211 == 0:
        return None
    avail = lambda label, ok: {"label": label, "available": ok, "skuId": sid * 10,
                               "sizeSellerData": [{"availableCount": 5 if ok else 0}]}
    all_out = sid % 97 == 0
    return {"style": {
        "id": sid, "name": f"Test Shirt {sid}", "brand": {"name": "TestBrand"},
        "sizes": [avail("S", not all_out), avail("M", not all_out and sid % 10 != 0),
                  avail("L", not all_out), avail("XL", not all_out)],
    }}


def make_myntra_app(flaky: bool = True) -> web.Application:
    seen: dict[int, int] = {}

    async def product(request: web.Request) -> web.Response:
        sid = int(request.match_info["sid"])
        seen[sid] = seen.get(sid, 0) + 1
        if flaky and sid % 503 == 0 and seen[sid] == 1:
            return web.Response(status=429, headers={"Retry-After": "1"})  # throttled once
        payload = style_payload(sid)
        if payload is None:
            return web.Response(status=404)
        return web.json_response(payload)

    app = web.Application()
    app.router.add_get("/gateway/v2/product/{sid}", product)
    return app


def make_webhook_app(sink: list) -> web.Application:
    async def hook(request: web.Request) -> web.Response:
        if request.headers.get("X-Api-Key") != "secret":
            return web.Response(status=401)
        sink.append(await request.json())
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/webhook/myntra-stock", hook)
    return app
