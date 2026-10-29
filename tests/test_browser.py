"""Search-bar engine in a REAL (headless) Chromium against a local fake site.

This proves our own logic (typing, Enter, redirect/tile flows, stale-page guard, parallel tabs,
session warm-up). It cannot prove Myntra's live DOM - see README 'First-run checklist'.
"""
import asyncio
from argparse import Namespace

import pytest
from aiohttp import web

pytest.importorskip("playwright")

from stockwatch.config import Config  # noqa: E402
from stockwatch.fetcher_browser import BrowserSession  # noqa: E402
from stockwatch.models import Item, Status  # noqa: E402
from stockwatch.runner import check_all, run  # noqa: E402
from stockwatch.store import Store  # noqa: E402
from tests.mock_site import make_site_app  # noqa: E402


async def _serve(app):
    r = web.AppRunner(app, access_log=None)
    await r.setup()
    s = web.TCPSite(r, "127.0.0.1", 0)
    await s.start()
    return r, f"http://127.0.0.1:{s._server.sockets[0].getsockname()[1]}"


def _chromium_ok() -> bool:
    async def probe():
        from playwright.async_api import async_playwright
        p = await async_playwright().start()
        try:
            b = await p.chromium.launch()
            await b.close()
            return True
        except Exception:
            return False
        finally:
            await p.stop()
    return asyncio.run(probe())


pytestmark = pytest.mark.skipif(not _chromium_ok(), reason="no Chromium available")


def _expected(sid: int) -> Status:
    if sid % 211 == 0:
        return Status.NOT_FOUND
    if sid % 97 == 0:
        return Status.ALL_OOS
    if sid % 10 == 0:
        return Status.PARTIAL_OOS
    return Status.OK


def test_search_bar_engine_direct(tmp_path):
    # mix of: redirect-style (odd), tile-style (even), %10 partial, %97 all-oos, %211 not found
    ids = [1000001, 1000002, 1000010, 1000003, 1000004, 1000005, 1000006, 1000008, 1000007,
           970000 + 97 - (970000 % 97), 211 * 5000, 1000009, 1000012, 1000013, 1000014, 1000015]
    ids = list(dict.fromkeys(ids))

    async def go():
        runner, base = await _serve(make_site_app())
        cfg = Config()
        cfg.site_base = base
        cfg.profile_dir = str(tmp_path / "profile")
        cfg.db_path = str(tmp_path / "b.db")
        cfg.search_wait = 3.0
        bs = BrowserSession(cfg, headless=True)
        try:
            await bs.start()
            cookies, ua = await bs.warm()
            assert ua  # session warm-up returns a user-agent
            check = await bs.make_check(tabs=3)  # 3 parallel tabs
            store = Store(cfg.db_path)
            await check_all([Item(str(i)) for i in ids], check, store, "b", 3, "search-bar")
            got = {r["style_id"]: r for r in store.iter_results("b")}
            return got
        finally:
            await bs.close()
            await runner.cleanup()

    got = asyncio.run(go())
    assert len(got) == len(ids), "every style must produce a result - none lost or crossed between tabs"
    for sid in ids:
        r = got[str(sid)]
        assert r["status"] == _expected(sid).value, (sid, r)
        if _expected(sid) == Status.PARTIAL_OOS:
            assert r["oos_sizes"] == "M"
        if _expected(sid) in (Status.OK, Status.PARTIAL_OOS, Status.ALL_OOS):
            assert r["name"] == f"Test Shirt {sid}"  # data really came from THIS style's page


def test_browser_mode_through_runner(tmp_path):
    from openpyxl import Workbook

    ids = [2000001, 2000010, 2000003, 2000004]
    wb = Workbook()
    ws = wb.active
    ws.append(["style_id", "sizes"])
    for i in ids:
        ws.append([i, "M, L"])
    xl = tmp_path / "in.xlsx"
    wb.save(xl)

    async def go():
        runner, base = await _serve(make_site_app())
        cfg = Config()
        cfg.site_base = base
        cfg.profile_dir = str(tmp_path / "profile2")
        cfg.out_dir, cfg.db_path = str(tmp_path / "out"), str(tmp_path / "out" / "x.db")
        cfg.browser_tabs, cfg.search_wait = 2, 3.0
        try:
            args = Namespace(input=str(xl), mode="browser", run_id="b1", resume=False, limit=None,
                             no_alert=True, always_notify=False, headed=False)
            return await run(cfg, args)
        finally:
            await runner.cleanup()

    s = asyncio.run(go())
    assert s["total_checked"] == 4 and s["errors"] == 0
    # 2000010 has M out of stock and M is watched -> PARTIAL (L in stock)
    assert s["partial_oos"] == 1 and s["ok"] == 3


def test_auto_mode_falls_back_to_search_bar(tmp_path):
    """Default mode: fast HTTP first; styles the API keeps refusing (HTTP 500) are re-checked
    through the real search bar, so nothing is left as ERROR."""
    from openpyxl import Workbook
    from tests.mock_myntra import style_payload

    always_500 = lambda sid: sid % 301 == 0
    ids = [301 * 3000, 301 * 3001, 5000001, 5000002, 5000010, 5000003, 5000004]  # first two: API always 500
    assert always_500(ids[0]) and always_500(ids[1])

    wb = Workbook()
    ws = wb.active
    ws.append(["style_id"])
    for i in ids:
        ws.append([i])
    xl = tmp_path / "in.xlsx"
    wb.save(xl)

    async def api(req: web.Request):
        sid = int(req.match_info["sid"])
        if always_500(sid):
            return web.Response(status=500, headers={"Retry-After": "1"})
        p = style_payload(sid)
        return web.Response(status=404) if p is None else web.json_response(p)

    async def go():
        app = make_site_app()
        app.router.add_get("/gateway/v2/product/{sid}", api)
        runner, base = await _serve(app)
        cfg = Config()
        cfg.site_base = cfg.api_base = base
        cfg.profile_dir = str(tmp_path / "profile3")
        cfg.out_dir, cfg.db_path = str(tmp_path / "out"), str(tmp_path / "out" / "a.db")
        cfg.retries, cfg.browser_tabs, cfg.search_wait = 1, 2, 3.0
        cfg.rps = cfg.max_rps = 500
        try:
            args = Namespace(input=str(xl), mode="auto", run_id="a1", resume=False, limit=None,
                             no_alert=True, always_notify=False, headed=False)
            s = await run(cfg, args)
            st = Store(cfg.db_path)
            return s, {r["style_id"]: r for r in st.iter_results("a1")}
        finally:
            await runner.cleanup()

    s, got = asyncio.run(go())
    assert s["errors"] == 0, "browser fallback must resolve every style the API refused"
    assert s["total_checked"] == len(ids)
    for sid in ids:
        assert got[str(sid)]["status"] == _expected(sid).value, (sid, got[str(sid)])
