"""Production web service (Railway): upload an Excel -> run in background -> report + WhatsApp.

    GET  /health                 no auth (Railway healthcheck)
    GET  /                       upload page + live progress
    POST /api/runs               multipart: file=<xlsx|csv>, mode=auto|fast|browser (optional)
    GET  /api/runs               recent runs
    GET  /api/runs/{id}          one run (+ live progress while running)
    GET  /api/runs/{id}/report   the Excel report

Auth: set STOCKWATCH_API_KEY. Browser asks for a password (user: anything, password: the key);
scripts can send header  X-Api-Key: <key>.  The server REFUSES to start without a key.

Durability: one job at a time (a FIFO queue). Jobs live in SQLite on the Volume; if the
container restarts/redeploys mid-run, unfinished jobs are re-queued with resume=True and
continue from the last flushed batch. An optional scheduler (STOCKWATCH_SCHEDULE_HOURS) re-runs
the most recent input file every N hours.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import logging
import re
import secrets
import time
from argparse import Namespace
from pathlib import Path

from aiohttp import web

from . import runner
from .config import Config
from .excel_io import read_items
from .store import Store

log = logging.getLogger("stockwatch.server")

CFG = web.AppKey("cfg", Config)
JOBS = web.AppKey("jobs", object)

_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MODES = ("auto", "fast", "browser")


# ---------------------------------------------------------------- auth ---
def _auth_ok(request: web.Request, key: str) -> bool:
    hdr = request.headers.get("X-Api-Key")
    if hdr is not None:
        return hmac.compare_digest(hdr.encode(), key.encode())
    basic = request.headers.get("Authorization", "")
    if basic.startswith("Basic "):
        try:
            pw = base64.b64decode(basic[6:]).decode().partition(":")[2]
        except Exception:
            return False
        return hmac.compare_digest(pw.encode(), key.encode())
    return False


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.path == "/health":
        return await handler(request)
    if not _auth_ok(request, request.app[CFG].api_key):
        return web.Response(status=401, text="unauthorized",
                            headers={"WWW-Authenticate": 'Basic realm="stockwatch"'})
    return await handler(request)


# ------------------------------------------------------------- the worker ---
class JobManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = Store(cfg.db_path)
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.current: str | None = None
        self._task: asyncio.Task | None = None
        self._sched: asyncio.Task | None = None
        Path(cfg.data_dir, "inputs").mkdir(parents=True, exist_ok=True)

    def start(self) -> None:
        for j in self.store.jobs_unfinished():  # survive restarts/redeploys
            if Path(j["input_path"]).exists():
                log.warning("re-queueing interrupted run %s (will resume)", j["run_id"])
                self.queue.put_nowait(j["run_id"])
            else:
                self.store.job_finish(j["run_id"], "failed", error="input file missing after restart")
        self._task = asyncio.create_task(self._worker())
        if self.cfg.schedule_hours > 0:
            self._sched = asyncio.create_task(self._scheduler())

    async def stop(self) -> None:
        for t in (self._task, self._sched):
            if t:
                t.cancel()
        await asyncio.gather(*(t for t in (self._task, self._sched) if t), return_exceptions=True)
        self.store.close()

    def submit(self, input_path: str, mode: str, run_id: str | None = None) -> str:
        run_id = run_id or f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
        self.store.job_create(run_id, input_path, mode)
        self.queue.put_nowait(run_id)
        return run_id

    async def _worker(self) -> None:
        while True:
            run_id = await self.queue.get()
            job = self.store.job_get(run_id)
            if not job or job["status"] in ("done", "done_with_errors", "failed"):
                continue
            self.current = run_id
            self.store.job_resume(run_id)  # -> status 'running'
            args = Namespace(input=job["input_path"], mode=job["mode"], run_id=run_id, resume=True, limit=None,
                             no_alert=False, always_notify=False, headed=False)
            try:
                summary = await runner.run(self.cfg, args)
                self.store.job_finish(run_id, "done" if summary["errors"] == 0 else "done_with_errors", summary)
            except asyncio.CancelledError:
                raise  # shutdown: leave status 'running' so the next start resumes it
            except Exception as e:
                log.exception("run %s failed", run_id)
                self.store.job_finish(run_id, "failed", error=f"{type(e).__name__}: {e}"[:500])
            finally:
                self.current = None

    async def _scheduler(self) -> None:
        period = self.cfg.schedule_hours * 3600
        while True:
            await asyncio.sleep(60)
            if self.current or not self.queue.empty():
                continue
            last = self.store.jobs_recent(1)
            if not last or time.time() - last[0]["created_at"] < period:
                continue
            if Path(last[0]["input_path"]).exists():
                rid = self.submit(last[0]["input_path"], last[0]["mode"])
                log.info("scheduler: started %s (every %.1fh)", rid, self.cfg.schedule_hours)


# --------------------------------------------------------------- handlers ---
async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def create_run(request: web.Request) -> web.Response:
    cfg: Config = request.app[CFG]
    jm: JobManager = request.app[JOBS]
    if not request.content_type.startswith("multipart/"):
        return web.json_response({"error": "send multipart/form-data with a 'file' field"}, status=400)

    mode, path = cfg.default_mode, None
    reader = await request.multipart()
    limit = cfg.max_upload_mb * 1024 * 1024
    while (part := await reader.next()) is not None:
        if part.name == "mode":
            mode = (await part.text()).strip() or mode
        elif part.name == "file":
            ext = Path(part.filename or "").suffix.lower()
            if ext not in (".xlsx", ".xlsm", ".csv", ".txt", ".tsv"):
                return web.json_response({"error": "upload .xlsx / .csv"}, status=400)
            path = Path(cfg.data_dir, "inputs", f"{secrets.token_hex(6)}{ext}")
            size = 0
            with open(path, "wb") as f:
                while chunk := await part.read_chunk(1 << 20):
                    size += len(chunk)
                    if size > limit:
                        f.close()
                        path.unlink(missing_ok=True)
                        return web.json_response({"error": f"file > {cfg.max_upload_mb} MB"}, status=413)
                    f.write(chunk)
    if path is None:
        return web.json_response({"error": "missing 'file'"}, status=400)
    if mode not in MODES:
        path.unlink(missing_ok=True)
        return web.json_response({"error": f"mode must be one of {MODES}"}, status=400)

    try:
        items, skipped = await asyncio.to_thread(read_items, path)  # validate before queueing
    except Exception as e:
        path.unlink(missing_ok=True)
        return web.json_response({"error": f"cannot read file: {type(e).__name__}"}, status=400)
    if not items:
        path.unlink(missing_ok=True)
        return web.json_response({"error": "no valid style IDs found"}, status=400)

    run_id = jm.submit(str(path), mode)
    return web.json_response({"run_id": run_id, "styles": len(items), "invalid_rows": skipped, "mode": mode},
                             status=202)


def _with_progress(request: web.Request, job: dict) -> dict:
    jm: JobManager = request.app[JOBS]
    if jm.current == job["run_id"]:
        job["progress"] = {k: v for k, v in runner.PROGRESS.items() if k != "run_id"}
    return job


async def list_runs(request: web.Request) -> web.Response:
    jm: JobManager = request.app[JOBS]
    return web.json_response({"runs": [_with_progress(request, j) for j in jm.store.jobs_recent(20)]})


async def get_run(request: web.Request) -> web.Response:
    run_id = request.match_info["run_id"]
    job = _RUN_ID.match(run_id) and request.app[JOBS].store.job_get(run_id)
    if not job:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(_with_progress(request, job))


async def get_results(request: web.Request) -> web.Response:
    run_id = request.match_info["run_id"]
    if not _RUN_ID.match(run_id):
        return web.json_response({"error": "invalid run id"}, status=400)
    rows = list(request.app[JOBS].store.iter_results(run_id))
    return web.json_response({"results": rows})


async def get_report(request: web.Request) -> web.StreamResponse:
    run_id = request.match_info["run_id"]
    f = Path(request.app[CFG].out_dir) / f"report_{run_id}.xlsx"
    if not _RUN_ID.match(run_id) or not f.exists():
        return web.json_response({"error": "report not ready"}, status=404)
    return web.FileResponse(f, headers={"Content-Disposition": f'attachment; filename="report_{run_id}.xlsx"'})


async def index(request: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML, content_type="text/html")


def create_app(cfg: Config | None = None) -> web.Application:
    cfg = cfg or Config()
    if not cfg.api_key:
        raise SystemExit("STOCKWATCH_API_KEY is not set - refusing to start an unauthenticated public service")
    Path(cfg.out_dir).mkdir(parents=True, exist_ok=True)

    app = web.Application(middlewares=[auth_middleware], client_max_size=cfg.max_upload_mb * 1024 * 1024 + 4096)
    app[CFG] = cfg
    app.add_routes([
        web.get("/health", health), web.get("/", index),
        web.post("/api/runs", create_run), web.get("/api/runs", list_runs),
        web.get("/api/runs/{run_id}", get_run), web.get("/api/runs/{run_id}/results", get_results),
        web.get("/api/runs/{run_id}/report", get_report),
    ])

    async def on_startup(app: web.Application) -> None:
        app[JOBS] = JobManager(cfg)
        app[JOBS].start()

    async def on_cleanup(app: web.Application) -> None:
        await app[JOBS].stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def serve(cfg: Config, host: str, port: int) -> None:
    web.run_app(create_app(cfg), host=host, port=port, access_log=None, shutdown_timeout=20)


# ------------------------------------------------------------------- UI ---
INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Myntra Stock Monitor</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#121a2d;--panel2:#18233a;--line:#283653;--text:#f2f5ff;--muted:#9ba9c5;--pink:#ff3f6c;--cyan:#65d9e8;--green:#53d6a0;--amber:#ffc76b;--red:#ff777e}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 12% 0%,#1b2a4c 0,transparent 38%),var(--bg);color:var(--text);font:15px/1.5 Inter,system-ui,Segoe UI,sans-serif}
main{max-width:1180px;margin:auto;padding:30px 22px 60px}.top{display:flex;justify-content:space-between;align-items:center;gap:18px;margin-bottom:26px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:13px}.logo{height:46px;width:46px;border-radius:15px;display:grid;place-items:center;background:linear-gradient(135deg,var(--pink),#9b5cff);font-weight:900;font-size:20px;box-shadow:0 8px 30px #ff3f6c35}
h1{font-size:clamp(23px,3vw,32px);letter-spacing:-.8px;margin:0}.sub{color:var(--muted);font-size:13px}.live{border:1px solid #275b55;background:#12332f;color:var(--green);padding:7px 12px;border-radius:30px;font-size:12px;font-weight:700}.live.paused{border-color:#66512b;background:#302817;color:var(--amber)}.friendly-error{font-size:12px;line-height:1.55;color:#ffd9a0;background:#302817;border:1px solid #66512b;border-radius:10px;padding:10px 12px;margin-top:10px}.details{font-size:11px;color:var(--muted);margin-top:8px;overflow-wrap:anywhere}
.layout{display:grid;grid-template-columns:minmax(290px,370px) 1fr;gap:18px;align-items:start}.panel{background:linear-gradient(145deg,#141d32,#101729);border:1px solid var(--line);border-radius:20px;padding:21px;box-shadow:0 18px 55px #0002}
h2{font-size:16px;margin:0 0 5px}.hint{color:var(--muted);font-size:13px;margin:0 0 18px}
label{display:block;font-size:12px;color:#c1cce2;font-weight:700;margin:16px 0 7px}
input[type=file],select{width:100%;background:#0b1325;border:1px solid #30405e;color:var(--text);border-radius:11px;padding:12px;font:inherit}
button{border:0;border-radius:11px;padding:12px 16px;background:linear-gradient(100deg,var(--pink),#f65e9b);color:#fff;font-weight:800;cursor:pointer;box-shadow:0 7px 20px #ff3f6c26}
button:disabled{opacity:.55;cursor:wait}.full{width:100%;margin-top:17px}.note{font-size:12px;color:var(--muted);margin-top:13px}
.statgrid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-bottom:16px}.stat{border:1px solid var(--line);border-radius:14px;padding:14px;background:#111a2c}.num{font-size:25px;font-weight:850;letter-spacing:-.5px}.statlabel{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.7px}
.run{border:1px solid var(--line);border-radius:16px;padding:16px;background:#10182a;margin-bottom:12px}.runhead{display:flex;justify-content:space-between;gap:10px;align-items:center;flex-wrap:wrap}.runid{font-weight:800;overflow-wrap:anywhere}.pill{font-size:11px;font-weight:800;padding:5px 9px;border-radius:30px;background:#26334c;color:#cbd7f0;text-transform:uppercase}.pill.done,.pill.done_with_errors{background:#2e281b;color:var(--amber)}.pill.running{background:#15353a;color:var(--cyan)}.pill.failed{background:#3d202b;color:var(--red)}
.progress{height:7px;background:#27334b;border-radius:20px;overflow:hidden;margin:14px 0 8px}.progress i{display:block;height:100%;background:linear-gradient(90deg,var(--pink),#9b5cff);border-radius:20px}
.metrics{font-size:12px;color:var(--muted);margin:9px 0 12px}.result{display:grid;grid-template-columns:1fr auto;gap:10px;align-items:start;padding:13px 0;border-top:1px solid #26334a}.prodname{font-weight:750}.sid{font-size:12px;color:var(--muted)}.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:9px}.chip{font-size:11px;padding:4px 8px;border-radius:7px;background:#173a32;color:var(--green);border:1px solid #255b4d}.chip.oos{background:#40202c;color:#ff9aa9;border-color:#703447}.chip.neutral{background:#253149;color:#c5d2e9;border-color:#354663}
a{color:var(--cyan);font-weight:700;text-decoration:none}a:hover{text-decoration:underline}.empty{padding:34px 12px;text-align:center;color:var(--muted);border:1px dashed #34425d;border-radius:14px}
.statusnote{font-size:12px;color:var(--muted);margin:14px 0 0;padding:12px;background:#10182a;border-radius:10px}
@media(max-width:820px){main{padding:18px 12px 40px}.layout{grid-template-columns:1fr}.statgrid{grid-template-columns:repeat(2,minmax(0,1fr))}.panel{padding:16px}}
</style></head><body><main>
<header class="top"><div class="brand"><div class="logo">M</div><div><h1>Myntra Stock Monitor</h1><div class="sub">Product sizes • stock status • low-stock alerts</div></div></div><div class="live" id="refreshBadge">● AUTO UPDATE · 15 SEC</div></header>
<div class="layout"><section class="panel"><h2>Start a stock check</h2><p class="hint">Upload your product list to check style IDs and size availability.</p>
<form id="f"><label for="file">PRODUCT LIST · EXCEL / CSV</label><input id="file" type="file" accept=".xlsx,.xlsm,.csv,.txt,.tsv" required>
<label for="mode">CHECK METHOD</label><select id="mode"><option value="auto">Recommended · Auto with fallback</option><option value="browser">Browser · product page</option><option value="fast">Fast · HTTP only</option></select>
<button id="go" class="full">▶ &nbsp; Start stock check</button><div id="msg" class="note">Excel should include a style_id column; sizes column is optional.</div></form>
<div class="statusnote">Important: a blocked or unreadable product page is shown as <b>ERROR / unverified</b>, not as out of stock. A product can show individual size chips such as <span class="chip oos">M · OUT</span> and <span class="chip">L · IN</span> when the source returns size data.</div></section>
<section><div class="statgrid" id="stats"><div class="stat"><div class="num">—</div><div class="statlabel">Recent runs</div></div><div class="stat"><div class="num">—</div><div class="statlabel">Checked</div></div><div class="stat"><div class="num">—</div><div class="statlabel">Out of stock</div></div><div class="stat"><div class="num">—</div><div class="statlabel">Errors</div></div></div>
<div class="panel"><h2>Monitoring activity</h2><p class="hint">Live progress and the latest size status returned by Myntra.</p><div id="runs"><div class="empty">Loading monitoring history…</div></div></div></section></div>
</main><script>
const $=id=>document.getElementById(id);const fmt=n=>Number(n||0).toLocaleString();let lastSignature='';let requestInFlight=false;
function node(t,c,txt){const e=document.createElement(t);if(c)e.className=c;if(txt!==undefined)e.textContent=txt;return e}
$('f').onsubmit=async ev=>{ev.preventDefault();if(!$('file').files.length)return;$('go').disabled=true;$('msg').textContent='Uploading file…';
const fd=new FormData();fd.append('mode',$('mode').value);fd.append('file',$('file').files[0]);
try{const r=await fetch('/api/runs',{method:'POST',body:fd});const j=await r.json();$('msg').textContent=r.ok?`Queued ${j.styles} style IDs${j.invalid_rows?` · ${j.invalid_rows} invalid rows skipped`:''}`:(j.error||'Upload failed');$('msg').style.color=r.ok?'var(--green)':'var(--red)';await load()}catch(e){$('msg').textContent='Could not connect to the server. Please refresh and try again.';$('msg').style.color='var(--red)'}finally{$('go').disabled=false}};
async function render(runs){const signature=JSON.stringify(runs);if(signature===lastSignature)return;lastSignature=signature;const box=$('runs');box.replaceChildren();let checked=0,oos=0,errors=0;
for(const j of runs){if(j.summary){checked+=Number(j.summary.total_checked||0);oos+=Number(j.summary.partial_oos||0)+Number(j.summary.all_oos||0);errors+=Number(j.summary.errors||0)}}
const nums=[runs.length,checked,oos,errors];$('stats').replaceChildren(...nums.map((n,i)=>{const d=node('div','stat');d.append(node('div','num',fmt(n)),node('div','statlabel',['Recent runs','Checked styles','OOS styles','Errors'][i]));return d}));
if(!runs.length){box.append(node('div','empty','No runs yet. Upload an Excel file to begin.'));return}
for(const j of runs){const card=node('article','run'),head=node('div','runhead');head.append(node('div','runid',j.run_id));const pill=node('span','pill '+j.status,j.status.replaceAll('_',' '));head.append(pill);card.append(head);
if(j.status==='running'&&j.progress&&j.progress.total){const p=j.progress,pc=Math.min(100,100*Number(p.done||0)/Number(p.total||1)),bar=node('div','progress'),i=node('i');i.style.width=pc+'%';bar.append(i);card.append(bar,node('div','metrics',`${p.phase||'Checking'} · ${p.done||0} / ${p.total} · ${pc.toFixed(0)}% · ETA ${p.eta_s||0}s`))}
if(j.summary){const s=j.summary;card.append(node('div','metrics',`Checked ${s.total_checked||0} · OK ${s.ok||0} · Partial OOS ${s.partial_oos||0} · All OOS ${s.all_oos||0} · Not found ${s.not_found||0} · Errors ${s.errors||0} · Alerts sent ${s.alerts_sent||0}`));
const link=node('a','','Download Excel report ↗');link.href='/api/runs/'+encodeURIComponent(j.run_id)+'/report';card.append(link);
try{const rr=await fetch('/api/runs/'+encodeURIComponent(j.run_id)+'/results');if(rr.ok){const data=(await rr.json()).results||[];for(const x of data){const row=node('div','result'),left=node('div',''),right=node('div','');
left.append(node('div','prodname',x.name||x.brand||'Myntra product'),node('div','sid','Style ID · '+x.style_id));
const chips=node('div','chips');const ins=(x.in_sizes||'').split(',').filter(Boolean),outs=(x.oos_sizes||'').split(',').filter(Boolean);
if(x.status==='ERROR'){chips.append(node('span','chip neutral','STOCK UNVERIFIED'))}
else if(ins.length||outs.length){ins.forEach(v=>chips.append(node('span','chip',v+' · IN')));outs.forEach(v=>chips.append(node('span','chip oos',v+' · OUT')))}
else chips.append(node('span','chip neutral',x.status));
left.append(chips);right.append(node('span','pill '+(x.status==='OK'?'running':x.status==='ERROR'?'failed':'done'),x.status.replaceAll('_',' ')));row.append(left,right);card.append(row);
if(x.error){const raw=String(x.error);if(/site maintenance|oops! something went wrong|please contact your administrator/i.test(raw)){card.append(node('div','friendly-error','Myntra is showing its Site Maintenance page. Stock cannot be verified right now. This is not an out-of-stock result; try again later.'));}else if(/blocked|captcha|access denied|challenge/i.test(raw)){card.append(node('div','friendly-error','Myntra has blocked or challenged this request. Stock is unverified; do not treat it as out of stock.'));}card.append(node('div','details','Technical details: '+raw))}}
}}catch(e){}}
if(j.error)card.append(node('div','metrics',j.error));box.append(card)}}
async function load(){if(requestInFlight)return;requestInFlight=true;try{const r=await fetch('/api/runs',{cache:'no-store'});if(r.ok){const payload=await r.json();await render(payload.runs||[]);$('refreshBadge').textContent='● AUTO UPDATE · 15 SEC';$('refreshBadge').classList.remove('paused')}}catch(e){$('refreshBadge').textContent='● CONNECTION ISSUE';$('refreshBadge').classList.add('paused')}finally{requestInFlight=false}}
load();setInterval(load,15000);
</script></body></html>
"""
