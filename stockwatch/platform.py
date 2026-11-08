"""Campaign dashboard v1: secure users, Google Sheet snapshots, scheduled stock checks.

A single-process starter deployment. Run only one dashboard instance; for multiple replicas
move the scheduler and queue to a dedicated distributed worker.
"""
from __future__ import annotations
import asyncio
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from aiohttp import web, ClientSession, ClientTimeout

from .config import Config
from .excel_io import read_items
from .models import norm_size
from .server import JobManager
from . import platform_integrations as gi

LOG = logging.getLogger('stockwatch.platform')
DB = web.AppKey('platform_db', object)
JOBS = web.AppKey('platform_jobs', JobManager)
CFG = web.AppKey('platform_cfg', Config)
SYNC = web.AppKey('platform_sync', object)
SESSION_SECONDS = 8 * 3600
LOGIN_ATTEMPTS = {}
ID_RE = re.compile(r'^[a-zA-Z0-9_-]{20,150}$')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
 password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','manager','employee')),
 active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
 csrf TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS campaigns(id INTEGER PRIMARY KEY, name TEXT NOT NULL,
 sheet_id TEXT NOT NULL, gid TEXT NOT NULL DEFAULT '0', mode TEXT NOT NULL DEFAULT 'browser',
 interval_minutes INTEGER NOT NULL DEFAULT 360, active INTEGER NOT NULL DEFAULT 1,
 created_by INTEGER NOT NULL, sync_hash TEXT, synced_at REAL, checked_at REAL,
 last_run_id TEXT, sync_error TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS memberships(user_id INTEGER NOT NULL, campaign_id INTEGER NOT NULL,
 PRIMARY KEY(user_id,campaign_id));
CREATE TABLE IF NOT EXISTS campaign_items(campaign_id INTEGER NOT NULL, style_id TEXT NOT NULL,
 sizes TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(campaign_id,style_id));
CREATE TABLE IF NOT EXISTS campaign_runs(run_id TEXT PRIMARY KEY, campaign_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, user_id INTEGER, event TEXT NOT NULL,
 detail TEXT, at REAL NOT NULL);
'''


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    result = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return salt.hex() + ':' + result.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split(':', 1)
        return hmac.compare_digest(password_hash(password, bytes.fromhex(salt)).split(':')[1], digest)
    except (ValueError, TypeError):
        return False


class PlatformDB:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript(SCHEMA)
        gi.init_google_schema(self.db)
        existing={r[1] for r in self.db.execute('PRAGMA table_info(campaigns)')}
        if 'pending_check' not in existing:
            self.db.execute('ALTER TABLE campaigns ADD COLUMN pending_check INTEGER NOT NULL DEFAULT 0')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS user_mfa(user_id INTEGER PRIMARY KEY, secret TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS campaign_settings(campaign_id INTEGER PRIMARY KEY,
          drive_folder_id TEXT, auto_drive INTEGER NOT NULL DEFAULT 0);
        ''')

    def one(self, sql, params=()):
        r = self.db.execute(sql, params).fetchone()
        return dict(r) if r else None

    def many(self, sql, params=()):
        return [dict(row) for row in self.db.execute(sql, params).fetchall()]

    def audit(self, uid, event, detail=''):
        self.db.execute('INSERT INTO audit(user_id,event,detail,at) VALUES(?,?,?,?)',
                        (uid, event, str(detail)[:500], time.time()))

    def bootstrap(self):
        if self.one('SELECT id FROM users LIMIT 1'):
            return
        user = os.environ.get('PLATFORM_ADMIN_USER', '').strip()
        password = os.environ.get('PLATFORM_ADMIN_PASSWORD', '')
        if not user or len(password) < 12:
            raise RuntimeError('Set PLATFORM_ADMIN_USER and PLATFORM_ADMIN_PASSWORD (at least 12 characters)')
        self.db.execute('INSERT INTO users(username,password_hash,role,created_at) VALUES(?,?,?,?)',
                        (user, password_hash(password), 'admin', time.time()))

    def close(self):
        self.db.close()


def sheet_identity(url: str):
    """Accept only Google spreadsheet IDs or canonical Sheets URLs; never arbitrary URLs."""
    url = url.strip()
    if re.fullmatch(r'[A-Za-z0-9_-]{20,150}', url):
        return url, '0'
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname not in ('docs.google.com',):
        raise ValueError('Use a Google Sheets URL or spreadsheet ID')
    m = re.fullmatch(r'/spreadsheets/d/([A-Za-z0-9_-]{20,150})(?:/.*)?', parsed.path)
    if not m:
        raise ValueError('Invalid Google Sheets URL')
    frag = parse_qs(parsed.fragment)
    query = parse_qs(parsed.query)
    gid = (frag.get('gid') or query.get('gid') or ['0'])[0]
    if not re.fullmatch(r'\d{1,12}', gid):
        raise ValueError('Invalid worksheet gid')
    return m.group(1), gid


def parse_sheet_csv(payload: str):
    reader = csv.reader(io.StringIO(payload.lstrip('\ufeff')))
    rows = list(reader)
    if not rows:
        raise ValueError('Sheet has no rows')
    headers = [str(h).strip().lower().replace(' ', '_') for h in rows[0]]
    if 'style_id' not in headers:
        raise ValueError('Sheet header must contain style_id')
    si = headers.index('style_id')
    zi = headers.index('sizes') if 'sizes' in headers else -1
    ai = headers.index('active') if 'active' in headers else -1
    merged = {}
    for row in rows[1:]:
        sid = row[si].strip() if len(row) > si else ''
        if not sid:
            continue
        if not sid.isdigit():
            raise ValueError('Invalid numeric style_id: ' + sid[:40])
        active = (row[ai].strip().lower() not in ('false', '0', 'no', 'off')) if ai >= 0 and len(row) > ai else True
        if not active:
            continue
        raw = row[zi] if zi >= 0 and len(row) > zi else ''
        sizes = {norm_size(v) for v in re.split(r'[,;|/]+', raw) if v.strip()}
        if sid not in merged:
            merged[sid] = sizes if sizes else None
        elif merged[sid] is not None:
            if not sizes:
                merged[sid] = None
            else:
                merged[sid].update(sizes)
    return {sid: (','.join(sorted(sizes)) if sizes is not None else '') for sid, sizes in merged.items()}


async def get_sheet_csv(sheet_id, gid):
    creds_path = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON', '')
    if creds_path:
        # Private sheet: share the Sheet with the service account's client_email.
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession
        def fetch_private():
            creds = service_account.Credentials.from_service_account_file(
                creds_path, scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
            session = AuthorizedSession(creds)
            try:
                response = session.get(f'https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}',
                                       params={'fields': 'sheets(properties(sheetId,title))'}, timeout=20)
                response.raise_for_status()
                worksheets = response.json().get('sheets', [])
                title = next((s['properties']['title'] for s in worksheets
                              if str(s['properties']['sheetId']) == str(gid)), None)
                if title is None:
                    raise ValueError('Worksheet gid not found')
                from urllib.parse import quote
                res = session.get(f'https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{quote(title, safe="")}',
                                  timeout=25)
                res.raise_for_status()
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerows(res.json().get('values', []))
                return output.getvalue()
            finally:
                session.close()
        return await asyncio.to_thread(fetch_private)
    # Public-sheet mode: workbook must be published/shareable without login.
    url = f'https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}'
    async with ClientSession(timeout=ClientTimeout(total=25)) as session:
        async with session.get(url, allow_redirects=True, max_redirects=4) as response:
            text = await response.text()
            if response.status != 200 or 'text/html' in response.headers.get('Content-Type', ''):
                raise ValueError('Sheet is not readable: publish it or configure GOOGLE_SERVICE_ACCOUNT_JSON')
            if len(text) > 5_000_000:
                raise ValueError('Sheet exceeds 5 MB limit')
            return text


def require_campaign(db, user, cid):
    c = db.one('SELECT * FROM campaigns WHERE id=?', (cid,))
    if not c:
        raise web.HTTPNotFound(text='Campaign not found')
    if user['role'] != 'admin' and c['created_by'] != user['id'] and not db.one(
            'SELECT 1 FROM memberships WHERE user_id=? AND campaign_id=?', (user['id'], cid)):
        raise web.HTTPForbidden(text='Campaign not assigned')
    return c


def job_submit(app, campaign, items):
    if not items:
        return None
    db = app[DB]
    previous = campaign['last_run_id']
    if previous:
        job = app[JOBS].store.job_get(previous)
        if job and job['status'] in ('queued', 'running'):
            return None
    output = Path(app[CFG].data_dir, 'inputs', f'campaign_{campaign["id"]}.csv')
    output.parent.mkdir(parents=True, exist_ok=True)
    # Immutable snapshot per run: never overwrite a file used by an in-progress job.
    output = output.with_name(f'campaign_{campaign["id"]}_{secrets.token_hex(6)}.csv')
    with output.open('w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['style_id', 'sizes'])
        for sid, sizes in sorted(items.items()):
            writer.writerow([sid, sizes])
    runid = app[JOBS].submit(str(output), campaign['mode'])
    db.db.execute('UPDATE campaigns SET last_run_id=?, checked_at=? WHERE id=?',
                  (runid, time.time(), campaign['id']))
    db.db.execute('INSERT INTO campaign_runs(run_id,campaign_id) VALUES(?,?)', (runid, campaign['id']))
    return runid


async def sync_campaign(app, campaign, force=False):
    db = app[DB]
    cid = campaign['id']
    try:
        raw = await gi.load_sheet(db.db, campaign, get_sheet_csv)
        items = parse_sheet_csv(raw)
        digest = hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()
        changed = digest != campaign['sync_hash']
        now = time.time()
        db.db.execute('BEGIN IMMEDIATE')
        try:
            db.db.execute('UPDATE campaigns SET sync_hash=?,synced_at=?,sync_error=NULL WHERE id=?',
                          (digest, now, cid))
            if changed:
                db.db.execute('DELETE FROM campaign_items WHERE campaign_id=?', (cid,))
                db.db.executemany('INSERT INTO campaign_items(campaign_id,style_id,sizes) VALUES(?,?,?)',
                                  [(cid, sid, sizes) for sid, sizes in items.items()])
            db.db.execute('COMMIT')
        except Exception:
            db.db.execute('ROLLBACK')
            raise
        due = not campaign['checked_at'] or now - campaign['checked_at'] >= campaign['interval_minutes'] * 60
        queued = changed or force or due or bool(campaign.get('pending_check'))
        runid = job_submit(app, campaign, items) if queued else None
        db.db.execute('UPDATE campaigns SET pending_check=? WHERE id=?', (int(bool(queued and not runid)),cid))
        return {'changed': changed, 'styles': len(items), 'run_id': runid, 'pending_check':bool(queued and not runid),'synced_at': now}
    except Exception as exc:
        LOG.warning('Sheet sync failed for campaign %s: %s', cid, exc)
        db.db.execute('UPDATE campaigns SET sync_error=? WHERE id=?', (str(exc)[:300], cid))
        raise


async def sync_loop(app):
    while True:
        for campaign in app[DB].many('SELECT * FROM campaigns WHERE active=1'):
            try:
                await sync_campaign(app, campaign)
                await archive_campaign(app, campaign)
            except Exception as exc:
                LOG.warning('Campaign %s scheduler error: %s', campaign['id'], exc)
        from .platform_v3 import process_report_schedules
        try:
            await process_report_schedules(app,DB,JOBS,archive_campaign)
        except Exception:
            LOG.exception('Scheduled report delivery cycle failed; scheduler will retry')
        await asyncio.sleep(max(30, int(os.environ.get('PLATFORM_SYNC_SECONDS', '90'))))


@web.middleware
async def security(request, handler):
    request['user'] = None
    token = request.cookies.get('sw_session', '')
    if ID_RE.fullmatch(token):
        s = request.app[DB].one('''SELECT u.id,u.username,u.role,u.active,s.csrf,s.expires
          FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?''',
          (hashlib.sha256(token.encode()).hexdigest(),))
        if s and s['active'] and s['expires'] > time.time():
            request['user'] = s
    public = request.path in ('/login', '/health') or request.path.startswith('/assets/') or (request.path == '/api/workspace' and request.method == 'GET')
    if not public and not request['user']:
        if request.path.startswith('/api/'):
            return web.json_response({'error': 'Unauthorized'}, status=401)
        raise web.HTTPFound('/login')
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and request.path != '/login':
        if not request['user'] or not hmac.compare_digest(
                request.headers.get('X-CSRF-Token', ''), request['user']['csrf']):
            return web.json_response({'error': 'CSRF token missing or invalid'}, status=403)
    try:
        response = await handler(request)
    except (ValueError, KeyError) as exc:
        response = web.json_response({'error': str(exc)}, status=400)
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
    response.headers['Cache-Control'] = 'no-store'
    if request.secure or os.environ.get('PLATFORM_SECURE_COOKIE') == '1':
        response.headers['Strict-Transport-Security']='max-age=31536000; includeSubDomains'
    return response


async def health(request):
    return web.json_response({'ok': True})


async def login(request):
    if request.method == 'GET':
        return web.FileResponse(Path(__file__).with_name('ui').joinpath('login.html'))
    form = await request.post()
    # Bounded in-memory login throttling (production: enforce distributed edge rate limits too).
    origin = request.remote or 'unknown'
    now = time.time()
    count, since = LOGIN_ATTEMPTS.get(origin, (0, now))
    if now - since > 900: count, since = 0, now
    if count >= 8:
        return web.Response(text='Too many login attempts; retry later', status=429)
    user = request.app[DB].one('SELECT * FROM users WHERE username=?', (str(form.get('username', '')),))
    if not user or not user['active'] or not verify_password(str(form.get('password', '')), user['password_hash']):
        LOGIN_ATTEMPTS[origin]=(count+1,since)
        await asyncio.sleep(.3)
        return web.Response(text=Path(__file__).with_name('ui').joinpath('login.html').read_text().replace('<!--ERROR-->', '<p class="error">Invalid login</p>'),content_type='text/html',status=401)
    mfa = request.app[DB].one('SELECT secret,enabled FROM user_mfa WHERE user_id=?',(user['id'],))
    if mfa and mfa['enabled'] and not check_totp(mfa['secret'], str(form.get('otp',''))):
        LOGIN_ATTEMPTS[origin]=(count+1,since)
        return web.Response(text=Path(__file__).with_name('ui').joinpath('login.html').read_text().replace('<!--ERROR-->', '<p class="error">Invalid verification code</p>'),content_type='text/html',status=401)
    LOGIN_ATTEMPTS.pop(origin,None)
    token = secrets.token_urlsafe(40)
    csrf = secrets.token_urlsafe(32)
    request.app[DB].db.execute('INSERT INTO sessions VALUES(?,?,?,?)',
        (hashlib.sha256(token.encode()).hexdigest(), user['id'], csrf, time.time()+SESSION_SECONDS))
    request.app[DB].audit(user['id'], 'login')
    res = web.HTTPFound('/')
    res.set_cookie('sw_session', token, httponly=True, secure=request.secure or os.environ.get('PLATFORM_SECURE_COOKIE') == '1',
                   samesite='Lax', max_age=SESSION_SECONDS, path='/')
    raise res


async def logout(request):
    token = request.cookies.get('sw_session', '')
    request.app[DB].db.execute('DELETE FROM sessions WHERE token_hash=?',
                               (hashlib.sha256(token.encode()).hexdigest(),))
    request.app[DB].audit(request['user']['id'], 'logout')
    res = web.json_response({'ok': True})
    res.del_cookie('sw_session')
    return res


async def me(request):
    u = request['user']
    return web.json_response({'user': {k:u[k] for k in ('id','username','role')}, 'csrf': u['csrf']})


async def campaigns(request):
    db = request.app[DB]
    u = request['user']
    if request.method == 'GET':
        if u['role'] == 'admin':
            rows = db.many('SELECT * FROM campaigns ORDER BY id DESC')
        else:
            rows = db.many('''SELECT DISTINCT c.* FROM campaigns c LEFT JOIN memberships m ON m.campaign_id=c.id
                        WHERE c.created_by=? OR m.user_id=? ORDER BY c.id DESC''', (u['id'],u['id']))
        for c in rows:
            c['styles'] = db.one('SELECT COUNT(*) AS n FROM campaign_items WHERE campaign_id=?', (c['id'],))['n']
            c['job'] = request.app[JOBS].store.job_get(c['last_run_id']) if c['last_run_id'] else None
        return web.json_response({'campaigns': rows})
    if u['role'] not in ('admin', 'manager'):
        raise web.HTTPForbidden(text='Only admins and managers can create campaigns')
    body = await request.json()
    name = str(body.get('name', '')).strip()
    if not 1 <= len(name) <= 100:
        raise ValueError('Name must be 1-100 characters')
    sid, gid = sheet_identity(str(body.get('sheet_url', '')))
    mode = body.get('mode', 'browser')
    if mode not in ('browser','auto','fast'):
        raise ValueError('Invalid mode')
    interval = int(body.get('interval_minutes', 360))
    if not 15 <= interval <= 10080:
        raise ValueError('Interval must be between 15 and 10080 minutes')
    cur = db.db.execute('''INSERT INTO campaigns(name,sheet_id,gid,mode,interval_minutes,created_by,created_at)
                    VALUES(?,?,?,?,?,?,?)''', (name,sid,gid,mode,interval,u['id'],time.time()))
    db.audit(u['id'], 'campaign_created', str(cur.lastrowid))
    return web.json_response({'id': cur.lastrowid}, status=201)


async def campaign_action(request):
    db = request.app[DB]
    u = request['user']
    cid = int(request.match_info['id'])
    c = require_campaign(db, u, cid)
    action = request.match_info['action']
    if action in ('sync','run'):
        if u['role'] not in ('admin','manager'):
            raise web.HTTPForbidden(text='Insufficient permission')
        if action == 'sync':
            try:
                result = await sync_campaign(request.app, c)
            except Exception as exc:
                return web.json_response({'error': str(exc)[:300]}, status=502)
        else:
            items = {r['style_id']:r['sizes'] for r in db.many('SELECT style_id,sizes FROM campaign_items WHERE campaign_id=?', (cid,))}
            result = {'run_id': job_submit(request.app, c, items)}
        db.audit(u['id'], action, str(cid))
        return web.json_response(result)
    if action == 'members':
        if u['role'] != 'admin':
            raise web.HTTPForbidden()
        body = await request.json()
        uid = int(body.get('user_id', 0))
        if not db.one('SELECT id FROM users WHERE id=? AND active=1',(uid,)):
            raise ValueError('User not found')
        db.db.execute('INSERT OR IGNORE INTO memberships VALUES(?,?)', (uid,cid))
        db.audit(u['id'],'campaign_assigned', f'{cid}:{uid}')
        return web.json_response({'ok':True})
    raise web.HTTPNotFound()


async def campaign_items(request):
    cid = int(request.match_info['id'])
    db = request.app[DB]
    c = require_campaign(db, request['user'], cid)
    items = db.many('SELECT style_id,sizes FROM campaign_items WHERE campaign_id=? ORDER BY style_id', (cid,))
    runid = c['last_run_id']
    results = []
    if runid:
        # Stock results live in the existing monitor database, not the platform auth database.
        results = list(request.app[JOBS].store.iter_results(runid))
        if len(results)>5000:
            results=results[:5000]
    return web.json_response({'items': items[:5000], 'results': results,
                              'total_items':len(items),'run': request.app[JOBS].store.job_get(runid) if runid else None})


async def users(request):
    u = request['user']
    if u['role'] != 'admin':
        raise web.HTTPForbidden()
    db = request.app[DB]
    if request.method == 'GET':
        return web.json_response({'users': db.many('SELECT id,username,role,active,created_at FROM users ORDER BY id')})
    body = await request.json()
    username = str(body.get('username','')).strip()
    password = str(body.get('password',''))
    role = body.get('role','employee')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{3,50}', username):
        raise ValueError('Username must be 3-50 letters/numbers/._-')
    if len(password)<12 or len(password)>200:
        raise ValueError('Password must be 12-200 characters')
    if role not in ('admin','manager','employee'):
        raise ValueError('Invalid role')
    try:
        cur = db.db.execute('INSERT INTO users(username,password_hash,role,created_at) VALUES(?,?,?,?)',
                            (username,password_hash(password),role,time.time()))
    except sqlite3.IntegrityError:
        raise ValueError('Username already exists')
    db.audit(u['id'], 'user_created', str(cur.lastrowid))
    return web.json_response({'id': cur.lastrowid}, status=201)


async def user_deactivate(request):
    if request['user']['role'] != 'admin':
        raise web.HTTPForbidden()
    uid = int(request.match_info['id'])
    if uid == request['user']['id']:
        raise ValueError('Cannot deactivate your own account')
    db = request.app[DB]
    db.db.execute('UPDATE users SET active=0 WHERE id=?',(uid,))
    db.db.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
    db.audit(request['user']['id'],'user_deactivated',str(uid))
    return web.json_response({'ok': True})


async def report(request):
    cid = int(request.match_info['id'])
    c = require_campaign(request.app[DB], request['user'], cid)
    runid = c['last_run_id']
    if not runid or not re.fullmatch(r'[A-Za-z0-9_-]+', runid):
        raise web.HTTPNotFound()
    path = Path(request.app[CFG].out_dir, f'report_{runid}.xlsx')
    if not path.is_file():
        raise web.HTTPNotFound(text='Report not ready')
    return web.FileResponse(path, headers={'Content-Disposition':f'attachment; filename="report_{runid}.xlsx"'})


async def index(request):
    return web.FileResponse(Path(__file__).with_name('ui').joinpath('dashboard.html'))


async def startup(app):
    app[JOBS].start()
    app[SYNC] = asyncio.create_task(sync_loop(app))


async def shutdown(app):
    app[SYNC].cancel()
    await asyncio.gather(app[SYNC], return_exceptions=True)
    await app[JOBS].stop()
    app[DB].close()



import base64
import struct

def totp(secret, when=None):
    # RFC 6238 TOTP SHA-1 / 30-second periods.
    import base64 as b64
    counter = int(time.time() if when is None else when)//30
    raw=b64.b32decode(secret,casefold=True)
    digest=hmac.new(raw,struct.pack('>Q',counter),hashlib.sha1).digest()
    pos=digest[-1]&15
    return str((struct.unpack('>I',digest[pos:pos+4])[0]&0x7fffffff)%1000000).zfill(6)

def check_totp(secret, code):
    return len(code)==6 and code.isdigit() and any(
        hmac.compare_digest(totp(secret,time.time()+d),code) for d in (-30,0,30))

async def mfa_manage(request):
    db=request.app[DB];uid=request['user']['id']
    if request.method=='GET':
        m=db.one('SELECT enabled FROM user_mfa WHERE user_id=?',(uid,))
        return web.json_response({'enabled':bool(m and m['enabled'])})
    body=await request.json()
    action=body.get('action')
    if action=='begin':
        existing=db.one('SELECT password_hash FROM users WHERE id=?',(uid,))
        if not verify_password(str(body.get('password','')),existing['password_hash']):
            raise ValueError('Verify your password before enabling authenticator')
        currently=db.one('SELECT secret,enabled FROM user_mfa WHERE user_id=?',(uid,))
        if currently and currently['enabled']:
            raise ValueError('MFA is already enabled')
        secret=base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')
        db.db.execute('INSERT INTO user_mfa VALUES(?,?,0) ON CONFLICT(user_id) DO UPDATE SET secret=excluded.secret,enabled=0',(uid,secret))
        # The secret is displayed once for authenticator enrollment and not logged.
        return web.json_response({'secret':secret,'otpauth_uri':f'otpauth://totp/StockIntelligence:{request["user"]["username"]}?secret={secret}&issuer=StockIntelligence'})
    if action=='verify':
        row=db.one('SELECT * FROM user_mfa WHERE user_id=?',(uid,))
        if not row or not check_totp(row['secret'],str(body.get('code',''))):
            raise ValueError('Invalid 6-digit authenticator code')
        db.db.execute('UPDATE user_mfa SET enabled=1 WHERE user_id=?',(uid,))
        db.audit(uid,'mfa_enabled')
        return web.json_response({'enabled':True})
    raise ValueError('Unknown MFA action')

async def change_password(request):
    db=request.app[DB];uid=request['user']['id']
    body=await request.json()
    existing=db.one('SELECT password_hash FROM users WHERE id=?',(uid,))
    if not verify_password(str(body.get('current','')),existing['password_hash']):
        raise ValueError('Current password is incorrect')
    new=str(body.get('new',''))
    if not 12<=len(new)<=200:raise ValueError('Password length must be 12 to 200')
    db.db.execute('UPDATE users SET password_hash=? WHERE id=?',(password_hash(new),uid))
    # Invalidate other sessions after a password change.
    token=request.cookies.get('sw_session','')
    db.db.execute('DELETE FROM sessions WHERE user_id=? AND token_hash<>?',
                  (uid,hashlib.sha256(token.encode()).hexdigest()))
    db.audit(uid,'password_changed')
    return web.json_response({'ok':True})

async def audit_events(request):
    if request['user']['role']!='admin':raise web.HTTPForbidden()
    return web.json_response({'events':request.app[DB].many('''SELECT a.id,u.username,a.event,a.detail,a.at
        FROM audit a LEFT JOIN users u ON a.user_id=u.id ORDER BY a.id DESC LIMIT 500''')})

async def google_start(request):
    if request['user']['role'] not in ('admin','manager'):raise web.HTTPForbidden()
    url=gi.oauth_start_url(request.app[DB].db,request['user']['id'])
    return web.json_response({'authorization_url':url})

async def google_callback(request):
    if request['user']['role'] not in ('admin','manager'):raise web.HTTPForbidden()
    if request.query.get('error'):raise web.HTTPBadRequest(text='Google authorization denied')
    await gi.oauth_finish(request.app[DB].db,request.query.get('state',''),
                          request.query.get('code',''),request['user']['id'])
    request.app[DB].audit(request['user']['id'],'google_connected')
    raise web.HTTPFound('/?google=connected')

async def google_status(request):
    db=request.app[DB]
    connected=db.one('SELECT user_id,connected_at FROM google_connections WHERE user_id=?',
                     (request['user']['id'],))
    return web.json_response({'connected':bool(connected),'connected_at':connected['connected_at'] if connected else None})

async def google_disconnect(request):
    db=request.app[DB]
    db.db.execute('DELETE FROM google_connections WHERE user_id=?',(request['user']['id'],))
    db.audit(request['user']['id'],'google_disconnected')
    return web.json_response({'ok':True})

async def campaign_settings(request):
    db=request.app[DB];cid=int(request.match_info['id'])
    c=require_campaign(db,request['user'],cid)
    if request.method=='GET':
        value=db.one('SELECT * FROM campaign_settings WHERE campaign_id=?',(cid,)) or {}
        return web.json_response({'drive_folder_id':value.get('drive_folder_id'), 'auto_drive':bool(value.get('auto_drive'))})
    if request['user']['role'] not in ('admin','manager'):raise web.HTTPForbidden()
    body=await request.json()
    folder=str(body.get('drive_folder_id','')).strip()
    if folder and not re.fullmatch(r'[A-Za-z0-9_-]{10,150}',folder):
        raise ValueError('Invalid Drive folder ID')
    auto=int(bool(body.get('auto_drive',False)))
    db.db.execute('INSERT INTO campaign_settings VALUES(?,?,?) ON CONFLICT(campaign_id) DO UPDATE SET drive_folder_id=excluded.drive_folder_id,auto_drive=excluded.auto_drive',(cid,folder or None,auto))
    db.audit(request['user']['id'],'campaign_settings',str(cid))
    return web.json_response({'ok':True})

async def archive_campaign(app,campaign,manual=False,archive_only=False):
    db=app[DB];cid=campaign['id'];rid=campaign['last_run_id']
    if not rid:return {'archived':False,'reason':'No run'}
    job=app[JOBS].store.job_get(rid)
    if not job or job['status'] not in ('done','done_with_errors'):
        return {'archived':False,'reason':'Report is not finished'}
    results=list(app[JOBS].store.iter_results(rid))
    db.db.executemany('''INSERT OR IGNORE INTO campaign_events(campaign_id,run_id,style_id,status,oos_sizes,in_sizes,checked_at)
        VALUES(?,?,?,?,?,?,?)''',[(cid,rid,r['style_id'],r['status'],r['oos_sizes'],r['in_sizes'],time.time()) for r in results])
    settings=db.one('SELECT * FROM campaign_settings WHERE campaign_id=?',(cid,)) or {}
    if archive_only or not (manual or settings.get('auto_drive')):
        return {'archived':True,'results':len(results)}
    if db.one('SELECT id FROM drive_exports WHERE campaign_id=? AND run_id=?',(cid,rid)):
        return {'archived':True,'already_uploaded':True}
    path=Path(app[CFG].out_dir,f'report_{rid}.xlsx')
    fid,url=await asyncio.to_thread(gi.upload_drive_file,db.db,campaign['created_by'],path,
               f'{campaign["name"]}_report_{rid}.xlsx',settings.get('drive_folder_id'))
    db.db.execute('INSERT INTO drive_exports(campaign_id,run_id,file_id,url,uploaded_at) VALUES(?,?,?,?,?)',
                  (cid,rid,fid,url,time.time()))
    return {'archived':True,'drive_file_id':fid,'drive_url':url}

async def upload_drive(request):
    db=request.app[DB];cid=int(request.match_info['id'])
    c=require_campaign(db,request['user'],cid)
    if request['user']['role'] not in ('admin','manager'):raise web.HTTPForbidden()
    try:result=await archive_campaign(request.app,c,manual=True)
    except Exception as exc:return web.json_response({'error':str(exc)[:250]},status=502)
    db.audit(request['user']['id'],'drive_export',str(cid))
    return web.json_response(result)

async def drive_history(request):
    db=request.app[DB];cid=int(request.match_info['id'])
    require_campaign(db,request['user'],cid)
    return web.json_response({'exports':db.many('SELECT run_id,url,uploaded_at FROM drive_exports WHERE campaign_id=? ORDER BY id DESC LIMIT 100',(cid,))})

async def history(request):
    db=request.app[DB];cid=int(request.match_info['id'])
    c=require_campaign(db,request['user'],cid)
    await archive_campaign(request.app,c, archive_only=True)
    sid=request.query.get('style_id','').strip()
    if sid and not sid.isdigit():raise ValueError('Invalid Style ID')
    if sid:
        rows=db.many('SELECT * FROM campaign_events WHERE campaign_id=? AND style_id=? ORDER BY id DESC LIMIT 500',(cid,sid))
    else:
        rows=db.many('SELECT * FROM campaign_events WHERE campaign_id=? ORDER BY id DESC LIMIT 500',(cid,))
    return web.json_response({'history':rows})

async def analytics(request):
    db=request.app[DB];u=request['user'];cid=int(request.match_info['id'])
    c=require_campaign(db,u,cid)
    await archive_campaign(request.app,c, archive_only=True)
    rows=db.many('''SELECT status,COUNT(*) n FROM campaign_events WHERE campaign_id=?
                   GROUP BY status''',(cid,))
    recent=db.many('''SELECT date(checked_at,'unixepoch') day,COUNT(*) checks,
      SUM(CASE WHEN status IN ('PARTIAL_OOS','ALL_OOS') THEN 1 ELSE 0 END) oos,
      SUM(CASE WHEN status='ERROR' THEN 1 ELSE 0 END) errors
      FROM campaign_events WHERE campaign_id=? GROUP BY day ORDER BY day DESC LIMIT 30''',(cid,))
    latest=db.many('''SELECT e.style_id,e.status,e.oos_sizes,e.in_sizes,e.checked_at
      FROM campaign_events e JOIN (SELECT style_id,MAX(id) id FROM campaign_events WHERE campaign_id=? GROUP BY style_id)
      recent ON recent.id=e.id ORDER BY e.style_id LIMIT 5000''',(cid,))
    summary={'total':len(latest),'available':sum(r['status']=='OK' for r in latest),
             'oos':sum(r['status'] in ('PARTIAL_OOS','ALL_OOS') for r in latest),
             'error':sum(r['status']=='ERROR' for r in latest)}
    return web.json_response({'summary':summary,'all_time_statuses':rows,'daily':recent,'latest':latest})

async def campaign_toggle(request):
    db=request.app[DB];cid=int(request.match_info['id'])
    require_campaign(db,request['user'],cid)
    if request['user']['role'] not in ('admin','manager'):raise web.HTTPForbidden()
    body=await request.json();active=int(bool(body.get('active',True)))
    db.db.execute('UPDATE campaigns SET active=? WHERE id=?',(active,cid))
    db.audit(request['user']['id'],'campaign_toggled',f'{cid}:{active}')
    return web.json_response({'active':bool(active)})

def create_app(cfg=None):
    cfg = cfg or Config()
    Path(cfg.out_dir).mkdir(parents=True, exist_ok=True)
    db = PlatformDB(str(Path(cfg.data_dir,'platform.db')))
    db.bootstrap()
    app = web.Application(middlewares=[security], client_max_size=1024*1024)
    app[DB], app[JOBS], app[CFG] = db, JobManager(cfg), cfg
    app.router.add_get('/api/google/start', google_start)
    app.router.add_get('/api/google/callback', google_callback)
    app.router.add_get('/api/google/status', google_status)
    app.router.add_post('/api/google/disconnect', google_disconnect)
    app.router.add_get('/api/audit', audit_events)
    app.router.add_get('/api/mfa', mfa_manage)
    app.router.add_post('/api/mfa', mfa_manage)
    app.router.add_post('/api/password', change_password)
    app.router.add_get('/api/campaigns/{id:\\d+}/history', history)
    app.router.add_get('/api/campaigns/{id:\\d+}/analytics', analytics)
    app.router.add_get('/api/campaigns/{id:\\d+}/drive', drive_history)
    app.router.add_post('/api/campaigns/{id:\\d+}/drive', upload_drive)
    app.router.add_get('/api/campaigns/{id:\\d+}/settings', campaign_settings)
    app.router.add_post('/api/campaigns/{id:\\d+}/settings', campaign_settings)
    app.router.add_post('/api/campaigns/{id:\\d+}/toggle', campaign_toggle)
    app.router.add_get('/health', health)
    app.router.add_get('/login', login)
    app.router.add_post('/login', login)
    app.router.add_get('/', index)
    app.router.add_get('/api/me', me)
    app.router.add_post('/api/logout', logout)
    app.router.add_get('/api/campaigns', campaigns)
    app.router.add_post('/api/campaigns', campaigns)
    app.router.add_get('/api/campaigns/{id:\\d+}/items', campaign_items)
    app.router.add_get('/api/campaigns/{id:\\d+}/report', report)
    app.router.add_post('/api/campaigns/{id:\\d+}/{action:sync|run|members}', campaign_action)
    app.router.add_get('/api/users', users)
    app.router.add_post('/api/users', users)
    app.router.add_post('/api/users/{id:\\d+}/deactivate', user_deactivate)
    from .platform_v3 import register
    register(app,DB,JOBS,CFG,require_campaign)
    app.router.add_static('/assets/',Path(__file__).with_name('ui'),show_index=False)
    app.on_startup.append(startup)
    app.on_cleanup.append(shutdown)
    return app


def serve(cfg, host='0.0.0.0', port=8080):
    web.run_app(create_app(cfg), host=host, port=port)


