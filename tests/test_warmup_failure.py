"""If the Chrome warm-up cannot reach the site, the run must still complete over plain HTTP."""
import asyncio
from argparse import Namespace

from aiohttp import web
from openpyxl import Workbook

from stockwatch.config import Config
from stockwatch.runner import run
from tests.mock_myntra import make_myntra_app


def test_run_survives_warmup_failure(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.append(["style_id"])
    for i in range(9_000_000, 9_000_050):
        ws.append([i])
    xl = tmp_path / "in.xlsx"
    wb.save(xl)

    async def go():
        r = web.AppRunner(make_myntra_app(flaky=False), access_log=None)
        await r.setup()
        s = web.TCPSite(r, "127.0.0.1", 0)
        await s.start()
        api = f"http://127.0.0.1:{s._server.sockets[0].getsockname()[1]}"
        cfg = Config()
        cfg.api_base = api                      # API works ...
        cfg.site_base = "http://127.0.0.1:9"    # ... but the page the browser must open does not
        cfg.warm_session = True
        cfg.profile_dir = str(tmp_path / "p")
        cfg.out_dir, cfg.db_path = str(tmp_path / "out"), str(tmp_path / "out" / "w.db")
        cfg.rps = cfg.max_rps = 1000
        try:
            args = Namespace(input=str(xl), mode="auto", run_id="w", resume=False, limit=None,
                             no_alert=True, always_notify=False, headed=False)
            return await run(cfg, args)
        finally:
            await r.cleanup()

    s = asyncio.run(go())
    assert s["total_checked"] == 50 and s["errors"] == 0
