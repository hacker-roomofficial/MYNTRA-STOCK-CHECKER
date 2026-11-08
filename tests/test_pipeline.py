"""End-to-end: Excel -> fast engine -> SQLite -> report -> fake n8n webhook."""
import asyncio
import time
from argparse import Namespace

import pytest
from aiohttp import web
from openpyxl import Workbook, load_workbook

from stockwatch.config import Config
from stockwatch.excel_io import read_items
from stockwatch.runner import run
from stockwatch.store import Store
from tests.mock_myntra import make_myntra_app, make_webhook_app, style_payload


def _make_excel(path, ids, sizes=None):
    wb = Workbook()
    ws = wb.active
    ws.append(["Style ID", "Sizes"])
    for i in ids:
        ws.append([float(i), sizes or ""])  # Excel often stores ids as floats
    ws.append(["not-a-number", ""])
    ws.append([ids[0], "M"])  # duplicate id (blank row + "M" row must stay "watch all sizes")
    wb.save(path)


def _args(path, **kw):
    base = dict(input=str(path), mode="fast", run_id="t1", resume=False, limit=None,
                no_alert=False, always_notify=False, headed=False)
    base.update(kw)
    return Namespace(**base)


async def _serve(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


def _cfg(tmp_path, api, hook):
    c = Config()
    c.api_base = c.site_base = api
    c.warm_session = False
    c.concurrency, c.rps, c.max_rps = 200, 2000, 5000
    c.webhook_url, c.webhook_key = f"{hook}/webhook/myntra-stock", "secret"
    c.out_dir, c.db_path = str(tmp_path / "out"), str(tmp_path / "out" / "t.db")
    c.chunk_size, c.realert_hours = 40, 24
    return c


def test_excel_reader(tmp_path):
    p = tmp_path / "in.xlsx"
    _make_excel(p, [1001, 1002, 1003], "S, M")
    items, skipped = read_items(p)
    assert [i.style_id for i in items] == ["1001", "1002", "1003"]  # float ids fixed, dupe merged
    assert skipped == 1
    assert items[0].watch_sizes == ("M", "S")


def test_duplicate_blank_plus_sized_row_keeps_all_sizes(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("style_id,sizes\n555,\n555,M\n666,S\n666,M\n")
    items, _ = read_items(p)
    by = {i.style_id: i.watch_sizes for i in items}
    assert by["555"] == ()              # blank = all sizes, stays all
    assert by["666"] == ("M", "S")      # explicit lists are unioned


def test_headerless_file(tmp_path):
    p = tmp_path / "ids.csv"
    p.write_text("111\n222\n333\n")
    items, _ = read_items(p)
    assert [i.style_id for i in items] == ["111", "222", "333"]


def test_full_pipeline_20k_styles_and_alert_dedup(tmp_path):
    N = 20_000
    ids = list(range(1_000_000, 1_000_000 + N))
    xl = tmp_path / "styles.xlsx"
    _make_excel(xl, ids)

    async def go():
        sink: list = []
        r1, api = await _serve(make_myntra_app())
        r2, hook = await _serve(make_webhook_app(sink))
        cfg = _cfg(tmp_path, api, hook)
        try:
            t = time.monotonic()
            s1 = await run(cfg, _args(xl))
            took = time.monotonic() - t
            s2 = await run(cfg, _args(xl, run_id="t2"))  # same stock again
            return s1, s2, sink, took
        finally:
            await r1.cleanup()
            await r2.cleanup()

    s1, s2, sink, took = asyncio.run(go())

    # ground truth straight from the fake catalogue
    exp = {"OK": 0, "PARTIAL_OOS": 0, "ALL_OOS": 0, "NOT_FOUND": 0}
    for sid in ids:
        pay = style_payload(sid)
        if pay is None:
            exp["NOT_FOUND"] += 1
        elif sid % 97 == 0:
            exp["ALL_OOS"] += 1
        elif sid % 10 == 0:
            exp["PARTIAL_OOS"] += 1
        else:
            exp["OK"] += 1
    # the one duplicated id was merged with watch-size "M" -> still same status for that id
    assert s1["total_checked"] == N
    assert s1["errors"] == 0, "429 once-then-ok styles must be retried successfully"
    assert (s1["ok"], s1["partial_oos"], s1["all_oos"], s1["not_found"]) == (
        exp["OK"], exp["PARTIAL_OOS"], exp["ALL_OOS"], exp["NOT_FOUND"])
    print(f"\n{N} styles in {took:.1f}s -> {N/took:.0f} styles/sec")

    # alerts: run 1 sends everything OOS, run 2 (nothing changed) sends nothing
    sent_styles = {a["style_id"] for body in sink for a in body["alerts"]}
    assert s1["alerts_sent"] == min(300, exp["PARTIAL_OOS"] + exp["ALL_OOS"])  # capped
    # run 2: the 300 already-alerted styles are NOT re-sent; the next 300 of the backlog go out
    assert s2["alerts_sent"] == min(300, s2["new_alerts"])
    assert s2["new_alerts"] == (exp["PARTIAL_OOS"] + exp["ALL_OOS"]) - s1["alerts_sent"], \
        "already-alerted styles must not be re-sent"
    assert all(a["oos_sizes"] for body in sink for a in body["alerts"])
    assert len(sent_styles) == s1["alerts_sent"] + s2["alerts_sent"]

    # report workbook
    wb = load_workbook(s1["report"], read_only=True)
    assert set(wb.sheetnames) == {"Out_of_Stock", "Errors_NotFound", "All"}
    n_oos_rows = sum(1 for _ in wb["Out_of_Stock"].iter_rows(values_only=True)) - 1  # minus header
    assert n_oos_rows == exp["PARTIAL_OOS"] + exp["ALL_OOS"]


def test_resume_skips_finished(tmp_path):
    ids = list(range(2_000_000, 2_000_500))
    xl = tmp_path / "s.xlsx"
    _make_excel(xl, ids)

    async def go():
        r1, api = await _serve(make_myntra_app(flaky=False))
        cfg = _cfg(tmp_path, api, "http://127.0.0.1:9")
        try:
            await run(cfg, _args(xl, run_id="r", limit=200, no_alert=True))
            return await run(cfg, _args(xl, run_id="r", resume=True, no_alert=True))
        finally:
            await r1.cleanup()

    s = asyncio.run(go())
    assert s["total_checked"] == 500  # 200 earlier + 300 new, nothing re-fetched twice


def test_webhook_down_keeps_alerts_pending(tmp_path):
    ids = list(range(3_000_000, 3_000_100))
    xl = tmp_path / "s.xlsx"
    _make_excel(xl, ids)

    async def go():
        r1, api = await _serve(make_myntra_app(flaky=False))
        cfg = _cfg(tmp_path, api, "http://127.0.0.1:9")  # nothing listens here
        try:
            return await run(cfg, _args(xl, run_id="w"))
        finally:
            await r1.cleanup()

    s = asyncio.run(go())
    assert s["alerts_sent"] == 0 and s["new_alerts"] > 0
    st = Store(str(tmp_path / "out" / "t.db"))
    assert len(st.pending_alerts("w", 24)) == s["new_alerts"]  # still pending -> retried next run
