"""Send out-of-stock alerts to the n8n webhook (n8n then formats + sends WhatsApp).

Alerts are chunked (cfg.chunk_size styles per call) so each WhatsApp message stays well
under WhatsApp's 4096-char limit, and capped (cfg.max_alert_styles) so a catastrophic run
(e.g. the whole catalogue OOS) cannot flood your phone - the full list is in the Excel report.
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp

from .config import Config

log = logging.getLogger("stockwatch.notify")


async def send_alerts(
    cfg: Config, run_id: str, summary: dict, alerts: list[dict], always_notify: bool = False
) -> list[dict]:
    """Returns the alerts that were delivered (so only those are marked as alerted)."""
    if not cfg.webhook_url:
        log.warning("N8N_WEBHOOK_URL not set - skipping alerts")
        return []

    capped = alerts[: cfg.max_alert_styles]
    more = len(alerts) - len(capped)
    chunks = [capped[i : i + cfg.chunk_size] for i in range(0, len(capped), cfg.chunk_size)]
    if not chunks and always_notify:
        chunks = [[]]

    delivered: list[dict] = []
    headers = {"X-Api-Key": cfg.webhook_key}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        for n, chunk in enumerate(chunks, 1):
            body = {
                "run_id": run_id,
                "part": n,
                "parts": len(chunks),
                "summary": summary,
                "more_not_shown": more if n == len(chunks) else 0,
                "alerts": [{k: v for k, v in a.items() if not k.startswith("_")} for a in chunk],
            }
            ok = False
            for attempt in range(4):
                try:
                    async with session.post(cfg.webhook_url, json=body, headers=headers) as r:
                        if r.status < 300:
                            ok = True
                            break
                        log.warning("webhook HTTP %s (attempt %d)", r.status, attempt + 1)
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    log.warning("webhook error %s (attempt %d)", type(e).__name__, attempt + 1)
                await asyncio.sleep(2 ** attempt)
            if not ok:
                log.error("alert part %d/%d NOT delivered - will retry next run", n, len(chunks))
                break  # keep order; undelivered alerts stay un-marked
            delivered.extend(chunk)
            await asyncio.sleep(1.0)  # be gentle with the WhatsApp API
    return delivered
