"""Tiny fake Myntra website for the browser (search-bar) engine.

 /                    home page with the search bar
 /search?q=<id>       ids ending in 1,3,5,7,9 -> 302 straight to the product page
                      ids ending in 0,2,4,6,8 -> a results grid with a tile linking to it
                      ids divisible by 211    -> results grid with no matching tile (not found)
 /shirts/b/n/<id>/buy product page; header search bar present; data in window.__myx
"""
from __future__ import annotations

import json

from aiohttp import web

from tests.mock_myntra import style_payload

HEADER = """<header><input class="desktop-searchBar" placeholder="Search for products, brands and more"
  onkeydown="if(event.key==='Enter'){location.href='/search?q='+encodeURIComponent(this.value)}"></header>"""


def make_site_app() -> web.Application:
    async def home(_):
        return web.Response(text=f"<html><body>{HEADER}<h1>home</h1></body></html>", content_type="text/html")

    async def search(req: web.Request):
        q = req.query.get("q", "")
        sid = int(q) if q.isdigit() else -1
        if sid % 211 == 0 or sid < 0:
            return web.Response(text=f"<html><body>{HEADER}<p>No results</p></body></html>", content_type="text/html")
        if sid % 2 == 1:
            raise web.HTTPFound(f"/shirts/b/n/{sid}/buy")
        grid = f'<a href="/shirts/b/n/{sid}/buy">tile {sid}</a>'
        return web.Response(text=f"<html><body>{HEADER}{grid}</body></html>", content_type="text/html")

    async def product(req: web.Request):
        sid = int(req.match_info["sid"])
        pay = style_payload(sid)
        if pay is None:
            raise web.HTTPNotFound()
        blob = json.dumps({"pdpData": pay["style"]})
        html = f"<html><body>{HEADER}<h1>{sid}</h1><script>window.__myx = {blob}</script></body></html>"
        return web.Response(text=html, content_type="text/html")

    app = web.Application()
    app.router.add_get("/", home)
    app.router.add_get("/search", search)
    app.router.add_get("/shirts/b/n/{sid}/buy", product)
    return app
