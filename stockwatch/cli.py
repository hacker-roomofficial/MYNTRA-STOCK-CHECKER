from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import Config


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stockwatch", description="Myntra size-wise stock checker -> n8n -> WhatsApp")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="check all style IDs from an Excel/CSV file")
    r.add_argument("--input", "-i", required=True, help="Excel/CSV with style_id (+ optional sizes) columns")
    r.add_argument("--mode", choices=["auto", "fast", "browser"], default="auto",
                   help="auto = fast HTTP + search-bar fallback (default) | fast = HTTP only | browser = search bar only")
    r.add_argument("--run-id", help="name of the run (needed with --resume)")
    r.add_argument("--resume", action="store_true", help="skip styles already finished in this run-id")
    r.add_argument("--limit", type=int, help="only the first N styles (test)")
    r.add_argument("--rps", type=float, help="starting requests/second (fast mode)")
    r.add_argument("--max-rps", type=float, help="ceiling for auto speed-up")
    r.add_argument("--concurrency", type=int, help="parallel workers (fast mode)")
    r.add_argument("--tabs", type=int, help="parallel Chrome tabs (search-bar mode)")
    r.add_argument("--headed", action="store_true", help="show the Chrome window (browser mode)")
    r.add_argument("--no-alert", action="store_true", help="do not call the n8n webhook")
    r.add_argument("--always-notify", action="store_true", help="send a WhatsApp summary even if nothing is OOS")
    r.add_argument("--webhook", help="n8n webhook URL (else N8N_WEBHOOK_URL)")
    r.add_argument("--api-base", help="override API host (testing)")
    r.add_argument("--no-warm", action="store_true", help="skip the Chrome session warm-up (testing)")

    sv = sub.add_parser("serve", help="run the web service (upload page + API) - this is what Railway runs")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=None, help="default: $PORT or 8080")

    l = sub.add_parser("login", help="open a VISIBLE Chrome once (solve any captcha / log in); the session is reused")
    l.add_argument("--url", default=None)

    s = sub.add_parser("sample", help="write a sample input Excel")
    s.add_argument("--out", default="styles_sample.xlsx")
    s.add_argument("--count", type=int, default=100)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = Config()

    if args.cmd == "sample":
        from .excel_io import write_sample

        write_sample(args.out, args.count)
        print(f"wrote {args.out}")
        return 0

    if args.cmd == "login":
        return asyncio.run(_login(cfg, args.url))

    if args.cmd == "serve":
        import os

        from .server import serve

        serve(cfg, args.host, args.port or int(os.environ.get("PORT", "8080")))
        return 0

    # ---- run ----
    if args.rps:
        cfg.rps = args.rps
    if args.max_rps:
        cfg.max_rps = args.max_rps
    if args.concurrency:
        cfg.concurrency = args.concurrency
    if args.tabs:
        cfg.browser_tabs = args.tabs
    if args.webhook:
        cfg.webhook_url = args.webhook
    if args.api_base:
        cfg.api_base = args.api_base
    if args.no_warm:
        cfg.warm_session = False

    from .runner import run

    try:
        summary = asyncio.run(run(cfg, args))
    except KeyboardInterrupt:
        print("\ninterrupted - progress is saved. Re-run with --resume --run-id <same id>")
        return 130
    return 0 if summary["errors"] == 0 else 2


async def _login(cfg: Config, url: str | None) -> int:
    from .fetcher_browser import BrowserSession

    b = BrowserSession(cfg, headless=False)
    await b.start()
    page = b.ctx.pages[0] if b.ctx.pages else await b.ctx.new_page()
    await page.goto(url or cfg.site_base)
    print("Chrome is open. Solve any captcha / log in if you want, then press ENTER here to save & close.")
    await asyncio.get_running_loop().run_in_executor(None, sys.stdin.readline)
    await b.close()
    return 0
