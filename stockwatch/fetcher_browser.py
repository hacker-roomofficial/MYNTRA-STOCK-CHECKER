"""Browser engine for Myntra public product pages.

Browser mode navigates directly to /<style_id> (no Myntra product API), waits for
PDP hydration, then reads the rendered size selector. Availability is determined
from the size control AND its surrounding DOM because Myntra has moved its
unavailable/disabled classes between the button and wrapper in different builds.
Unknown states are never guessed as out of stock.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from playwright.async_api import TimeoutError as PlaywrightTimeout

from .config import Config
from .models import Item, Status, StyleResult, SizeInfo, norm_size
from .parser import build_result

log = logging.getLogger("stockwatch.browser")


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
            args=["--no-first-run", "--no-default-browser-check", "--disable-dev-shm-usage"]
                 + ([] if self.headless else ["--start-minimized"]),
        )
        try:
            self.ctx = await self._pw.chromium.launch_persistent_context(channel="chrome", **kwargs)
        except Exception as e:
            log.warning("Google Chrome unavailable (%s); using Playwright Chromium", str(e).splitlines()[0])
            self.ctx = await self._pw.chromium.launch_persistent_context(**kwargs)

    async def close(self) -> None:
        try:
            if self.ctx:
                await self.ctx.close()
        finally:
            if self._pw:
                await self._pw.stop()

    async def warm(self) -> tuple[dict[str, str], str]:
        page = self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()
        await page.goto(self.cfg.site_base, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2500)
        ua = await page.evaluate("() => navigator.userAgent")
        cookies = {c["name"]: c["value"] for c in await self.ctx.cookies(self.cfg.site_base)}
        log.info("session warmed: %d cookies", len(cookies))
        return cookies, ua

    async def make_check(self, tabs: int) -> Callable[[Item], Awaitable[StyleResult]]:
        self._pages = asyncio.Queue()
        for _ in range(max(1, tabs)):
            self._pages.put_nowait(await self.ctx.new_page())

        async def check(item: Item) -> StyleResult:
            page = await self._pages.get()
            try:
                return await self._open_product(page, item)
            except Exception as e:
                log.exception("Browser check failed for style %s", item.style_id)
                return StyleResult(item.style_id, Status.ERROR,
                                   error=f"browser: {type(e).__name__}: {str(e)[:180]}")
            finally:
                self._pages.put_nowait(page)

        return check

    async def _open_product(self, page, item: Item) -> StyleResult:
        sid = str(item.style_id).strip()
        target = f"{self.cfg.site_base.rstrip('/')}/{sid}"
        response = await page.goto(target, wait_until="domcontentloaded", timeout=60000)
        # Product details are hydrated client-side; wait for size controls or a clear page state.
        try:
            await page.wait_for_function(
                """() => document.querySelector(
                  '.size-buttons-size-button, [class*="size-buttons-size-button"], [data-testid*="size"]'
                ) || /not found|page not found|access denied|captcha|temporarily blocked/i.test(document.body?.innerText || '')""",
                timeout=18000,
            )
        except PlaywrightTimeout:
            # Continue to diagnostics below; never turn an unreadable page into OOS.
            pass
        await page.wait_for_timeout(1200)

        title = (await page.title())[:180]
        body = (await page.locator("body").inner_text(timeout=8000))[:1200]
        low = f"{title} {body}".lower()
        if any(x in low for x in ("access denied", "captcha", "verify you are", "unusual traffic",
                                  "temporarily blocked", "request blocked", "site maintenance")):
            return StyleResult(sid, Status.ERROR, error="Myntra block/challenge page; stock was not verified.")
        if response and response.status == 404:
            return StyleResult(sid, Status.NOT_FOUND, error="Myntra product URL returned HTTP 404")
        if any(x in low for x in ("page not found", "product not found", "oops! something went wrong")):
            return StyleResult(sid, Status.NOT_FOUND, error="Myntra product page says product not found")

        dom = await page.evaluate(r"""() => {
          const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
          // Discover current and future size-control markup. Myntra's CSS class
          // names change; use size-related containers plus known PDP controls.
          const selectors = [
            '.size-buttons-size-button',
            '[class*="size-buttons-size-button"]',
            '[data-testid*="size"] button',
            '[data-testid*="size"] [role="button"]',
            '[class*="size"] button',
            '[class*="size"] [role="button"]',
            'button[aria-label*="size" i]',
            '[role="button"][aria-label*="size" i]'
          ];
          const candidates = new Set();
          for (const selector of selectors) {
            document.querySelectorAll(selector).forEach(el => { if (visible(el)) candidates.add(el); });
          }

          // Accept a broad, data-driven set of labels instead of a hardcoded
          // catalogue: XS/XXXS/7XL, 28/32/44, 2XL, 4XS, Free Size, One Size, etc.
          const compact = value => String(value || '').toUpperCase().replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
          const valid = value => {
            const c = compact(value).replace(/\s+/g, '');
            return /^(?:[SML]|X{1,20}[SL]|[1-9][0-9]?X[SL]|[0-9]{1,3}(?:UK|US|EU|IN)?|FREESIZE|ONESIZE|OS|STANDARD)$/.test(c);
          };
          const extract = raw => {
            const text = compact(raw);
            if (!text) return '';
            const candidates = [text, text.replace(/^SIZE\s*:?\s*/i, ''), text.replace(/\s+SIZE$/i, '')];
            for (const candidate of candidates) if (valid(candidate)) return candidate;
            // Accessibility labels sometimes include descriptive suffixes.
            const tokens = text.match(/(?:\b(?:X{1,20}[SL]|[1-9][0-9]?\s*X[SL]|[0-9]{1,3}(?:\s*(?:UK|US|EU|IN))?|FREE\s*SIZE|ONE\s*SIZE|OS|STANDARD|S|M|L)\b)/g) || [];
            return tokens.find(valid) || '';
          };
          const bad = /(unavailable|out[\s_-]*of[\s_-]*stock|sold[\s_-]*out|disabled|not[\s_-]*available|oos)/i;
          const sizes = [];
          for (const el of candidates) {
            const text = el.innerText || el.textContent || '';
            const aria = el.getAttribute('aria-label') || '';
            const title = el.getAttribute('title') || '';
            const label = extract(text) || extract(aria) || extract(title);
            if (!label) continue;

            let node = el;
            let unavailable = false;
            const evidence = [];
            for (let depth = 0; node && depth < 4; depth++, node = node.parentElement) {
              const cls = typeof node.className === 'string' ? node.className : '';
              const attrs = [cls, node.getAttribute?.('aria-label') || '', node.getAttribute?.('title') || '',
                             node.getAttribute?.('data-testid') || ''].join(' ');
              const st = getComputedStyle(node);
              if (bad.test(attrs)) { unavailable = true; evidence.push('class/attribute'); }
              if (node.disabled || node.hasAttribute?.('disabled') || node.getAttribute?.('aria-disabled') === 'true') {
                unavailable = true; evidence.push('disabled');
              }
              if (st.textDecorationLine.includes('line-through')) { unavailable = true; evidence.push('line-through'); }
              if (Number(st.opacity) > 0 && Number(st.opacity) <= 0.45) { unavailable = true; evidence.push('opacity'); }
            }
            if (getComputedStyle(el).pointerEvents === 'none') { unavailable = true; evidence.push('pointer-events'); }
            sizes.push({label, available: !unavailable, evidence});
          }
          // Myntra PDP convention: pdp-title is the brand; pdp-name is the product name.
          // Keep the h1 fallback for pages where the branded class is absent.
          const brand = document.querySelector('.pdp-title, [class*="pdp-title"]')?.innerText?.trim()
                        || document.querySelector('h1')?.innerText?.trim() || '';
          const name = document.querySelector('.pdp-name, [class*="pdp-name"]')?.innerText?.trim() || '';
          return {sizes, name, brand, url: location.href};
        }""")

        # Merge duplicate DOM nodes conservatively: any explicit OOS evidence wins.
        by_label: dict[str, bool] = {}
        for entry in dom.get("sizes") or []:
            label = norm_size(str(entry.get("label", "")))
            if not label:
                continue
            available = bool(entry.get("available"))
            by_label[label] = by_label.get(label, True) and available

        if not by_label:
            return StyleResult(sid, Status.ERROR, name=str(dom.get("name") or ""),
                               brand=str(dom.get("brand") or ""),
                               error=f"Product page opened but no readable size controls found; title={title!r}; URL={page.url}")

        sizes = [SizeInfo(label=k, available=v, qty=None) for k, v in by_label.items()]
        watched = {norm_size(str(x)) for x in item.watch_sizes if str(x).strip()}
        missing = watched - set(by_label)
        if missing:
            return StyleResult(sid, Status.ERROR, name=str(dom.get("name") or ""),
                               brand=str(dom.get("brand") or ""), sizes=sizes,
                               error="Watched size(s) not visible; cannot infer OOS: " + ", ".join(sorted(missing)))
        return build_result(item, str(dom.get("name") or ""), str(dom.get("brand") or ""), sizes)
