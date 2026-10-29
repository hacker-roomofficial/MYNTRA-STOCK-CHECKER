"""Browser engine: real Chrome (background/headless) that types the style ID into Myntra's
search bar - exactly what a person would do - and reads the product data from the page.

Two jobs:
  1. warm()        -> open Myntra once, return cookies + user-agent for the fast HTTP engine
  2. make_check()  -> slow-but-sure per-style check through the search bar (used for
                      --mode browser, and as the fallback for styles the fast engine could not read)

A persistent profile (cfg.profile_dir) keeps cookies/login between runs, so any one-time
challenge solved with `python main.py login` (visible window) stays solved.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Awaitable, Callable

from playwright.async_api import TimeoutError as PlaywrightTimeout

from .config import Config
from .models import Item, Status, StyleResult
from .parser import parse_html, parse_pdp

log = logging.getLogger("stockwatch.browser")

_JS_PDP = "() => (window.__myx && window.__myx.pdpData) || null"
_JS_MATCH = "(sid) => !!(window.__myx && window.__myx.pdpData && String(window.__myx.pdpData.id) === sid)"


class BrowserSession:
    def __init__(self, cfg: Config, headless: bool | None = None):
        self.cfg = cfg
        self.headless = cfg.headless if headless is None else headless
        self._pw = None
        self.ctx = None
        self._pages: asyncio.Queue | None = None

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        kwargs = dict(
            user_data_dir=self.cfg.profile_dir,
            headless=self.headless,
            viewport={"width": 1366, "height": 850},
            locale="en-IN",
            args=["--no-first-run", "--no-default-browser-check",
                  "--disable-dev-shm-usage"]  # containers have a tiny /dev/shm -> Chromium would crash
                 + ([] if self.headless else ["--start-minimized"]),
        )
        try:  # prefer the user's installed Google Chrome
            self.ctx = await self._pw.chromium.launch_persistent_context(channel="chrome", **kwargs)
        except Exception as e:
            log.warning("Google Chrome not found (%s) -> using Playwright Chromium", str(e).splitlines()[0])
            self.ctx = await self._pw.chromium.launch_persistent_context(**kwargs)

    async def close(self) -> None:
        try:
            if self.ctx:
                await self.ctx.close()
        finally:
            if self._pw:
                await self._pw.stop()

    # ---- 1. session warm-up ------------------------------------------------
    async def warm(self) -> tuple[dict[str, str], str]:
        page = self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()
        await page.goto(self.cfg.site_base, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2500)  # let the site set its cookies / run its sensors
        ua = await page.evaluate("() => navigator.userAgent")
        cookies = {c["name"]: c["value"] for c in await self.ctx.cookies(self.cfg.site_base)}
        log.info("session warmed: %d cookies", len(cookies))
        return cookies, ua

    # ---- 2. search-bar check -----------------------------------------------
    async def make_check(self, tabs: int) -> Callable[[Item], Awaitable[StyleResult]]:
        self._pages = asyncio.Queue()
        for _ in range(tabs):
            page = await self.ctx.new_page()
            await self._ensure_home(page)
            self._pages.put_nowait(page)

        async def check(item: Item) -> StyleResult:
            page = await self._pages.get()
            try:
                return await self._search(page, item)
            except Exception as e:
                try:  # recover a wedged tab
                    await self._ensure_home(page, force=True)
                except Exception:
                    pass
                return StyleResult(item.style_id, Status.ERROR, error=f"browser: {type(e).__name__}: {str(e)[:120]}")
            finally:
                self._pages.put_nowait(page)

        return check

    async def _ensure_home(self, page, force: bool = False) -> None:
        if force or "myntra" not in page.url:
            await page.goto(self.cfg.site_base, wait_until="domcontentloaded", timeout=45000)

    async def _search(self, page, item: Item) -> StyleResult:
        """Open the requested product page directly; changing search selectors are fragile.

        A timeout/challenge is an ERROR (unknown stock), never NOT_FOUND/OOS.
        """
        sid = item.style_id
        target = f"{self.cfg.site_base.rstrip('/')}/{sid}"
        try:
            response = await page.goto(target, wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_function(
                    "(sid) => !!(window.__myx && window.__myx.pdpData && "
                    "String(window.__myx.pdpData.id) === sid)",
                    arg=sid, timeout=12000,
                )
                pdp = await page.evaluate(_JS_PDP)
                if pdp:
                    return parse_pdp(item, pdp) or StyleResult(
                        sid, Status.ERROR, error="Product page loaded but stock data could not be parsed"
                    )
            except PlaywrightTimeout:
                pass

            title = (await page.title())[:160]
            body = (await page.locator("body").inner_text(timeout=4000))[:500]
            low = f"{title} {body}".lower()
            if any(x in low for x in ("site maintenance", "oops! something went wrong", "please contact your administrator")):
                return StyleResult(
                    sid, Status.ERROR,
                    error="Myntra is showing its Site Maintenance page; stock not verified. Retry later."
                )
            if any(x in low for x in ("access denied", "captcha", "verify you are", "unusual traffic",
                                      "temporarily blocked", "request blocked", "robot")):
                return StyleResult(sid, Status.ERROR,
                                   error=f"Myntra challenge/block page detected (title={title!r}); stock not verified")
            parsed = parse_html(item, await page.content())
            if parsed:
                return parsed
            if response and response.status == 404:
                return StyleResult(sid, Status.NOT_FOUND, error="Myntra returned HTTP 404")
            return StyleResult(
                sid, Status.ERROR,
                error=f"Product stock data not found; URL={page.url!r}; title={title!r}; page={body[:180]!r}"
            )
        except PlaywrightTimeout as e:
            return StyleResult(sid, Status.ERROR, error=f"Product page navigation timed out: {str(e)[:100]}")
