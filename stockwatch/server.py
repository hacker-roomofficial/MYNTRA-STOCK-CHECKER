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
        web.get("/api/runs/{run_id}", get_run), web.get("/api/runs/{run_id}/report", get_report),
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
INDEX_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Stock Watch</title>
<style>
:root{--bg:#f6f7f9;--fg:#15181d;--mut:#667085;--card:#fff;--bd:#e4e7ec;--ac:#1d4ed8;--ok:#067647;--bad:#b42318}
@media(prefers-color-scheme:dark){:root{--bg:#0f1115;--fg:#e8eaee;--mut:#98a2b3;--card:#171a21;--bd:#2a2f3a;--ac:#6b9bff;--ok:#47cd89;--bad:#f97066}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:24px 16px}h1{font-size:20px;margin:0 0 16px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:16px;margin-bottom:16px}
label{display:block;font-size:13px;color:var(--mut);margin:10px 0 4px}
input[type=file],select{width:100%;padding:8px;border:1px solid var(--bd);border-radius:8px;background:var(--bg);color:var(--fg)}
button{margin-top:14px;padding:9px 16px;border:0;border-radius:8px;background:var(--ac);color:#fff;font-weight:600;cursor:pointer}
button:disabled{opacity:.5}.mut{color:var(--mut);font-size:13px}.row{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap}
.bar{height:8px;background:var(--bd);border-radius:99px;overflow:hidden;margin:8px 0}.bar>i{display:block;height:100%;background:var(--ac)}
a{color:var(--ac)}.ok{color:var(--ok)}.bad{color:var(--bad)}code{font-size:13px}
</style></head><body><main>
<h1>Myntra Stock Watch</h1>
<form class="card" id="f">
  <label for="file">Excel / CSV (column <code>style_id</code>, optional <code>sizes</code>)</label>
  <input id="file" type="file" accept=".xlsx,.xlsm,.csv,.txt,.tsv" required>
  <label for="mode">Mode</label>
  <select id="mode"><option value="auto">auto (fast + direct-page fallback)</option><option value="fast">fast (HTTP only)</option><option value="browser">browser (direct product pages)</option></select>
  <button id="go">Start check</button> <span id="msg" class="mut"></span>
</form>
<div id="runs"></div>
</main><script>
const $=id=>document.getElementById(id);
function el(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e}
$('f').onsubmit=async ev=>{ev.preventDefault();$('go').disabled=true;$('msg').textContent='uploading...';
 const fd=new FormData();fd.append('mode',$('mode').value);fd.append('file',$('file').files[0]);
 try{const r=await fetch('/api/runs',{method:'POST',body:fd});const j=await r.json();
  $('msg').textContent=r.ok?('queued '+j.styles+' styles'+(j.invalid_rows?' ('+j.invalid_rows+' invalid rows skipped)':'')):(j.error||'error');
  $('msg').className=r.ok?'mut ok':'mut bad';load()}catch(e){$('msg').textContent='network error'}
 $('go').disabled=false};
function render(runs){const box=$('runs');box.replaceChildren();
 if(!runs.length){box.append(el('p','mut','No runs yet.'));return}
 for(const j of runs){const c=el('div','card');const top=el('div','row');
  top.append(el('strong','',j.run_id),el('span','mut',j.status));c.append(top);
  const p=j.progress;
  if(j.status==='running'&&p&&p.total){const pc=Math.min(100,100*p.done/p.total);const b=el('div','bar'),i=el('i');i.style.width=pc+'%';b.append(i);c.append(b);
   c.append(el('div','mut',p.phase+': '+p.done+' / '+p.total+' | '+p.rate+'/s | ETA '+p.eta_s+'s'))}
  else if(j.summary){const s=j.summary;c.append(el('div','mut','checked '+s.total_checked+' | OK '+s.ok+' | partial OOS '+s.partial_oos+' | all OOS '+s.all_oos+' | 404 '+s.not_found+' | errors '+s.errors+' | '+s.seconds+'s | alerts sent '+s.alerts_sent));
   const a=el('a','','Download report');a.href='/api/runs/'+j.run_id+'/report';c.append(a)}
  if(j.error)c.append(el('div','bad',j.error));box.append(c)}}
async function load(){try{const r=await fetch('/api/runs');if(r.ok)render((await r.json()).runs)}catch(e){}}
load();setInterval(load,2000);
</script></body></html>
"""
