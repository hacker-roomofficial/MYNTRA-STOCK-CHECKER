"""Pipeline: Excel -> (skip already done) -> check -> SQLite -> Excel report -> n8n alerts."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from pathlib import Path
from typing import Awaitable, Callable

from .config import Config
from .excel_io import read_items, write_report
from .fetcher_http import HttpFetcher
from .models import Item, Status, StyleResult
from .notifier import send_alerts
from .store import Store

log = logging.getLogger("stockwatch")

FetchFn = Callable[[Item], Awaitable[StyleResult]]

# Live progress of the run in flight (read by the web service for its progress bar).
PROGRESS: dict = {}


async def check_all(items: list[Item], fetch: FetchFn, store: Store, run_id: str, workers: int, label: str) -> Counter:
    """Worker pool. Results are buffered and flushed to SQLite in batches (crash-safe resume)."""
    total = len(items)
    counts: Counter = Counter()
    if not total:
        return counts

    q: asyncio.Queue[Item] = asyncio.Queue()
    for it in items:
        q.put_nowait(it)

    buf: list[StyleResult] = []
    state = {"done": 0}
    t0 = time.monotonic()

    def flush() -> None:
        store.save_many(run_id, buf)
        buf.clear()

    async def worker() -> None:
        while True:
            try:
                it = q.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                res = await fetch(it)
            except Exception as e:  # never let one style kill the run
                res = StyleResult(it.style_id, Status.ERROR, error=f"{type(e).__name__}: {e}")
            buf.append(res)
            counts[res.status.value] += 1
            state["done"] += 1
            if len(buf) >= 500:
                flush()

    async def progress() -> None:
        tick = 0
        while True:
            await asyncio.sleep(1)
            tick += 1
            d = state["done"]
            el = max(time.monotonic() - t0, 1e-6)
            rate = d / el
            eta = (total - d) / rate if rate else 0
            PROGRESS.update(phase=label, done=d, total=total, rate=round(rate, 1), eta_s=int(eta), counts=dict(counts))
            if tick % 5 == 0:
                log.info(
                    "[%s] %d/%d (%.0f/s) ETA %ds | OK %d  PARTIAL %d  ALL_OOS %d  404 %d  ERR %d",
                    label, d, total, rate, eta, counts["OK"], counts["PARTIAL_OOS"],
                    counts["ALL_OOS"], counts["NOT_FOUND"], counts["ERROR"],
                )

    prog = asyncio.create_task(progress())
    try:
        await asyncio.gather(*(worker() for _ in range(min(workers, total))))
    finally:
        prog.cancel()
        flush()
    el = time.monotonic() - t0
    log.info("[%s] finished %d styles in %.1fs (%.0f/s)", label, total, el, total / max(el, 1e-6))
    return counts


async def run(cfg: Config, args) -> dict:
    PROGRESS.clear()
    PROGRESS.update(phase="loading", done=0, total=0, run_id=args.run_id)
    items, skipped = read_items(args.input)
    log.info("loaded %d unique style IDs from %s (%d invalid rows skipped)", len(items), args.input, skipped)
    if args.limit:
        items = items[: args.limit]

    store = Store(cfg.db_path)
    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S")
    if args.resume:
        done = store.done_ids(run_id)
        items = [i for i in items if i.style_id not in done]
        log.info("resume: %d already done, %d to go (run_id=%s)", len(done), len(items), run_id)

    t_start = time.monotonic()
    browser = None
    try:
        # ---------------- fast engine ----------------
        if args.mode in ("fast", "auto"):
            cookies, ua, refresher = {}, None, None
            if cfg.warm_session:
                from .fetcher_browser import BrowserSession

                browser = BrowserSession(cfg)
                try:
                    await browser.start()
                    cookies, ua = await browser.warm()
                    refresher = browser.warm
                except Exception as e:
                    # Warm-up is only an optimisation. If Chrome/Myntra is unreachable, carry on
                    # with plain HTTP rather than failing the whole run.
                    log.warning("browser warm-up failed (%s) -> continuing without session cookies",
                                str(e).splitlines()[0][:150])
                    try:
                        await browser.close()
                    except Exception:
                        pass
                    browser, cookies, ua, refresher = None, {}, None, None
            http = HttpFetcher(cfg, cookies, ua, refresher)
            await http.start()
            try:
                await check_all(items, http.fetch, store, run_id, cfg.concurrency, "fast")
            finally:
                await http.close()
            log.info("http stats: %s final_rps=%.0f", http.stats, http.throttle.rate)

            if args.mode == "auto":
                retry_ids = store.error_ids(run_id)
                retry = [i for i in items if i.style_id in retry_ids]
                if retry and cfg.warm_session and browser is not None:
                    log.info("auto: retrying %d failed styles through the search bar", len(retry))
                    check = await browser.make_check(cfg.browser_tabs)
                    await check_all(retry, check, store, run_id, cfg.browser_tabs, "search-bar")

        # ---------------- browser engine ----------------
        elif args.mode == "browser":
            from .fetcher_browser import BrowserSession

            browser = BrowserSession(cfg, headless=False if args.headed else None)
            await browser.start()
            check = await browser.make_check(cfg.browser_tabs)
            await check_all(items, check, store, run_id, cfg.browser_tabs, "search-bar")
    finally:
        if browser is not None:
            await browser.close()

    # ---------------- report ----------------
    counts = store.counts(run_id)
    Path(cfg.out_dir).mkdir(parents=True, exist_ok=True)
    report = Path(cfg.out_dir) / f"report_{run_id}.xlsx"
    n = write_report(report, store.iter_results(run_id))
    elapsed = time.monotonic() - t_start

    summary = {
        "total_checked": sum(counts.values()),
        "ok": counts.get("OK", 0),
        "partial_oos": counts.get("PARTIAL_OOS", 0),
        "all_oos": counts.get("ALL_OOS", 0),
        "not_found": counts.get("NOT_FOUND", 0),
        "errors": counts.get("ERROR", 0),
        "seconds": round(elapsed, 1),
        "report": str(report),
    }

    # ---------------- alerts ----------------
    sent = 0
    if not args.no_alert:
        alerts = store.pending_alerts(run_id, cfg.realert_hours)
        summary["new_alerts"] = len(alerts)
        delivered = await send_alerts(
            cfg, run_id, summary, alerts, always_notify=args.always_notify or cfg.always_notify)
        store.mark_alerted(delivered)
        store.clear_recovered(run_id)
        sent = len(delivered)
    summary["alerts_sent"] = sent
    store.close()

    log.info("DONE %s", summary)
    return summary
