"""Fast engine: async HTTP against the JSON endpoint the product page itself uses.

Design
------
* One asyncio event loop, N worker coroutines, one aiohttp keep-alive connection pool.
  (aiohttp, not httpx: measured 8,300 vs 495 req/s on the same box - httpx's per-request
  overhead is the bottleneck once you go past a few hundred req/s.)
* AdaptiveThrottle (AIMD): on 429/403/5xx the global rate is halved (once per cooldown) and
  everyone pauses; after a streak of successes the rate creeps back up. You set a *ceiling*;
  the tool finds the speed the site actually tolerates instead of getting your IP blocked.
* Session cookies come from a real Chrome (see fetcher_browser.warm); when blocks pile up
  the session is refreshed automatically (single-flight, with a cooldown).
* Optional proxy pool: requests are spread round-robin over cfg.proxies.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Awaitable, Callable

import aiohttp
from yarl import URL

from .config import Config
from .models import Item, Status, StyleResult
from .parser import parse_gateway_json, parse_html

log = logging.getLogger("stockwatch.http")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

Refresher = Callable[[], Awaitable[tuple[dict[str, str], str]]]


class AdaptiveThrottle:
    """Global request pacer with additive-increase / multiplicative-decrease."""

    def __init__(self, rate: float, min_rate: float, max_rate: float):
        self.rate = max(min_rate, min(rate, max_rate))
        self.min_rate, self.max_rate = min_rate, max_rate
        self._next = 0.0
        self._pause_until = 0.0
        self._last_cut = 0.0
        self._streak = 0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            start = max(now, self._next, self._pause_until)
            self._next = start + 1.0 / self.rate
        delay = start - now
        if delay > 0:
            await asyncio.sleep(delay)

    def on_success(self) -> None:
        self._streak += 1
        # roughly "every ~1s of healthy traffic" -> +10%, so recovery works at low rates too
        if self._streak >= max(10, min(100, int(self.rate))):
            self.rate = min(self.max_rate, self.rate * 1.10)
            self._streak = 0

    def on_blocked(self, retry_after: float | None = None) -> None:
        now = time.monotonic()
        # Hundreds of requests are in flight at once, so one block shows up as a burst of
        # 429s. Cut the rate ONCE per cooldown window, not once per response, or a single
        # hiccup would collapse the rate straight to the floor.
        if now - self._last_cut > 2.0:
            self.rate = max(self.min_rate, self.rate * 0.5)
            self._last_cut = now
        self._streak = 0
        pause = retry_after if retry_after else random.uniform(2, 5)
        self._pause_until = max(self._pause_until, now + pause)


class HttpFetcher:
    def __init__(
        self,
        cfg: Config,
        cookies: dict[str, str] | None = None,
        user_agent: str | None = None,
        refresher: Refresher | None = None,
    ):
        self.cfg = cfg
        self.cookies = cookies or {}
        self.ua = user_agent or DEFAULT_UA
        self.refresher = refresher
        self.throttle = AdaptiveThrottle(cfg.rps, cfg.min_rps, cfg.max_rps)
        self._session: aiohttp.ClientSession | None = None
        self._headers = self._make_headers()
        self._rr = 0
        self._blocks_since_refresh = 0
        self._last_refresh = 0.0
        self._refresh_lock = asyncio.Lock()
        self.stats = {"requests": 0, "blocked": 0, "refreshes": 0}

    # ---- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        conn = aiohttp.TCPConnector(limit=self.cfg.concurrency + 20, ttl_dns_cache=300)
        self._session = aiohttp.ClientSession(
            connector=conn,
            timeout=aiohttp.ClientTimeout(total=self.cfg.timeout),
            cookie_jar=aiohttp.CookieJar(unsafe=True),  # unsafe=True: also works for IP hosts / local tests
        )
        self._load_cookies()

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    def _make_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-IN,en;q=0.9",
            "Referer": self.cfg.site_base + "/",
        }

    def _load_cookies(self) -> None:
        jar = self._session.cookie_jar
        jar.clear()
        if self.cookies:
            jar.update_cookies(self.cookies, URL(self.cfg.site_base))

    def _proxy(self) -> str | None:
        if not self.cfg.proxies:
            return None
        self._rr = (self._rr + 1) % len(self.cfg.proxies)
        return self.cfg.proxies[self._rr]

    # ---- session refresh -----------------------------------------------------
    async def _maybe_refresh(self) -> None:
        if not self.refresher or self._blocks_since_refresh < 25:
            return
        async with self._refresh_lock:
            if self._blocks_since_refresh < 25 or time.monotonic() - self._last_refresh < 60:
                return
            log.warning("many blocks -> refreshing browser session")
            try:
                cookies, ua = await self.refresher()
            except Exception as e:  # keep running with old cookies
                log.error("session refresh failed: %s", e)
                self._last_refresh = time.monotonic()
                return
            self.cookies, self.ua = cookies, ua or self.ua
            self._headers = self._make_headers()
            self._load_cookies()
            self._blocks_since_refresh = 0
            self._last_refresh = time.monotonic()
            self.stats["refreshes"] += 1

    # ---- the check -----------------------------------------------------------
    async def fetch(self, item: Item) -> StyleResult:
        url = self.cfg.api_url(item.style_id)
        last_err = ""
        for attempt in range(self.cfg.retries + 1):
            await self.throttle.acquire()
            self.stats["requests"] += 1
            try:
                async with self._session.get(url, headers=self._headers, proxy=self._proxy()) as r:
                    code = r.status
                    if code == 200:
                        res = await self._parse(item, r)
                        if res is not None:
                            self.throttle.on_success()
                            return res
                        last_err = "200 but unrecognised/challenge page"
                        self._on_block(None)
                    elif code in (404, 410):
                        await r.read()
                        self.throttle.on_success()
                        return StyleResult(item.style_id, Status.NOT_FOUND, error=f"HTTP {code}")
                    elif code in (403, 429) or code >= 500:
                        last_err = f"HTTP {code}"
                        ra = r.headers.get("Retry-After", "")
                        self._on_block(float(ra) if ra.isdigit() else None)
                    else:
                        last_err = f"HTTP {code}"
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_err = type(e).__name__
                await asyncio.sleep(min(8, 0.5 * 2 ** attempt) + random.random() * 0.3)
                continue

            await self._maybe_refresh()
            await asyncio.sleep(min(10, 0.7 * 2 ** attempt) + random.random() * 0.5)

        return StyleResult(item.style_id, Status.ERROR, error=last_err or "unknown")

    def _on_block(self, retry_after: float | None) -> None:
        self.stats["blocked"] += 1
        self._blocks_since_refresh += 1
        self.throttle.on_blocked(retry_after)

    @staticmethod
    async def _parse(item: Item, r: aiohttp.ClientResponse) -> StyleResult | None:
        if "json" in (r.content_type or ""):
            try:
                return parse_gateway_json(item, await r.json(content_type=None))
            except ValueError:
                return None
        return parse_html(item, await r.text(errors="replace"))  # product page HTML with window.__myx
