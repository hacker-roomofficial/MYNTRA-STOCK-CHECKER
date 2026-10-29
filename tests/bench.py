"""Engine speed benchmark against the local mock (no Myntra involved).

    python -m tests.bench 100000

Shows the ceiling of the Python engine itself. Real-world speed against myntra.com is set
by how many requests/sec the site (and your IP / proxies) tolerate, not by this number.
"""
import asyncio
import sys
import time

from aiohttp import web

from stockwatch.config import Config
from stockwatch.fetcher_http import HttpFetcher
from stockwatch.models import Item
from tests.mock_myntra import make_myntra_app


async def main(n: int) -> None:
    runner = web.AppRunner(make_myntra_app(flaky=False), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    cfg = Config()
    cfg.api_base = cfg.site_base = base
    cfg.concurrency, cfg.rps, cfg.max_rps = 300, 20000, 20000
    http = HttpFetcher(cfg)
    await http.start()

    q = asyncio.Queue()
    for i in range(n):
        q.put_nowait(Item(str(5_000_000 + i)))

    async def worker():
        while True:
            try:
                it = q.get_nowait()
            except asyncio.QueueEmpty:
                return
            await http.fetch(it)

    t = time.monotonic()
    await asyncio.gather(*(worker() for _ in range(cfg.concurrency)))
    el = time.monotonic() - t
    print(f"{n} styles in {el:.1f}s = {n/el:.0f} styles/sec  ({n/el*60:.0f}/min)")
    await http.close()
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 50000))
