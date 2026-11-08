"""Regression test for Myntra size chips with disabled state on wrapper elements."""
import asyncio
import pytest
from aiohttp import web

pytest.importorskip("playwright")
from stockwatch.config import Config
from stockwatch.fetcher_browser import BrowserSession
from stockwatch.models import Item, Status


def test_direct_product_url_detects_oos_on_wrapper(tmp_path):
    async def go():
        async def product(req):
            sid = req.match_info['sid']
            html = '''<html><head><title>Test product</title></head><body>
              <h1>Test Shirt</h1>
              <div class="size-buttons-size-button-container">
                <div class="size-buttons-size-button-disabled"><button class="size-buttons-size-button">S</button></div>
                <div><button class="size-buttons-size-button">M</button></div>
                <div><button class="size-buttons-size-button">L</button></div>
              </div></body></html>'''
            return web.Response(text=html, content_type='text/html')
        app = web.Application()
        app.router.add_get('/{sid}', product)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        cfg = Config()
        cfg.site_base = base
        cfg.profile_dir = str(tmp_path / 'profile')
        bs = BrowserSession(cfg, headless=True)
        try:
            await bs.start()
            check = await bs.make_check(1)
            result = await check(Item('123456', ['S', 'M', 'L']))
            assert result.status == Status.PARTIAL_OOS
            assert result.oos_sizes == ['S']
            assert result.in_sizes == ['M', 'L']
        finally:
            await bs.close()
            await runner.cleanup()
    asyncio.run(go())
