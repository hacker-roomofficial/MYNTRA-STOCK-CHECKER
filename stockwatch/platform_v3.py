"""Version 3 UI-facing features. All mutations use existing CSRF/session middleware."""
import csv
import io
import json
import re
import secrets
import time
from pathlib import Path
from aiohttp import web


async def process_report_schedules(app, dbkey, jobskey, archive_campaign):
    """Deliver at most once per completed run and per schedule period."""
    db=app[dbkey];now=time.time()
    schedules=db.many('SELECT * FROM report_schedules WHERE active=1')
    for s in schedules:
        c=db.one('SELECT * FROM campaigns WHERE id=?',(s['campaign_id'],))
        if not c or not c['active'] or not c['last_run_id'] or s['last_run_id']==c['last_run_id']:
            continue
        if s['last_sent_at'] and now-s['last_sent_at'] < s['frequency_hours']*3600:
            continue
        job=app[jobskey].store.job_get(c['last_run_id'])
        if not job or job['status'] not in ('done','done_with_errors'):
            continue
        try:
            # The stock checker already writes the local XLSX. Drive is optional.
            if s['auto_drive']:
                await archive_campaign(app,c,manual=True)
            db.db.execute('UPDATE report_schedules SET last_sent_at=?,last_run_id=?,last_error=NULL WHERE id=?',
                          (now,c['last_run_id'],s['id']))
            db.audit(s['created_by'],'report_delivered',f"schedule={s['id']};run={c['last_run_id']}")
        except Exception as ex:
            db.db.execute('UPDATE report_schedules SET last_error=? WHERE id=?',(str(ex)[:250],s['id']))


def register(app, dbkey, jobskey, cfgkey, require_campaign):
    db = app[dbkey]
    db.db.executescript('''
    CREATE TABLE IF NOT EXISTS workspace_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS report_schedules(id INTEGER PRIMARY KEY,campaign_id INTEGER NOT NULL,
      frequency_hours INTEGER NOT NULL DEFAULT 24,auto_drive INTEGER NOT NULL DEFAULT 0,
      active INTEGER NOT NULL DEFAULT 1,created_by INTEGER NOT NULL,created_at REAL NOT NULL);
    ''')

    columns=[r[1] for r in db.db.execute('PRAGMA table_info(report_schedules)')]
    if 'last_sent_at' not in columns:
        db.db.execute('ALTER TABLE report_schedules ADD COLUMN last_sent_at REAL')
    if 'last_run_id' not in columns:
        db.db.execute('ALTER TABLE report_schedules ADD COLUMN last_run_id TEXT')
    if 'last_error' not in columns:
        db.db.execute('ALTER TABLE report_schedules ADD COLUMN last_error TEXT')

    def workspace():
        rows=db.many('SELECT key,value FROM workspace_settings')
        value={r['key']:r['value'] for r in rows}
        return {'configured':bool(value.get('brand_name')),'brand_name':value.get('brand_name',''),
                'accent':value.get('accent','#5145ee'),'logo_initials':value.get('logo_initials','SI'),
                'theme_default':value.get('theme_default','light')}

    async def workspace_api(req):
        if req.method=='GET':return web.json_response(workspace())
        if req['user']['role']!='admin':raise web.HTTPForbidden()
        body=await req.json()
        title=str(body.get('brand_name','')).strip()
        if not 2<=len(title)<=75:raise ValueError('Organization name must be 2-75 characters')
        accent=str(body.get('accent','#5145ee'))
        if not re.fullmatch(r'#[0-9a-fA-F]{6}',accent):raise ValueError('Color must be #RRGGBB')
        initials=str(body.get('logo_initials','SI')).strip().upper()
        if not re.fullmatch(r'[A-Z0-9]{1,3}',initials):raise ValueError('Logo initials must be 1-3 characters')
        theme=str(body.get('theme_default','light'))
        if theme not in ('light','dark'):raise ValueError('Invalid theme')
        for k,v in [('brand_name',title),('accent',accent),('logo_initials',initials),('theme_default',theme)]:
            db.db.execute('INSERT INTO workspace_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(k,v))
        db.audit(req['user']['id'],'workspace_updated',title)
        return web.json_response(workspace())

    async def campaign_edit(req):
        cid=int(req.match_info['id']);c=require_campaign(db,req['user'],cid)
        if req['user']['role'] not in ('admin','manager'):raise web.HTTPForbidden()
        # Managers can edit campaigns they created; admins can edit any.
        if req['user']['role']!='admin' and c['created_by']!=req['user']['id']:raise web.HTTPForbidden()
        if req.method=='DELETE':
            # Archive rather than erase history; preserve reports and audit trail.
            db.db.execute('UPDATE campaigns SET active=0 WHERE id=?',(cid,))
            db.audit(req['user']['id'],'campaign_archived',str(cid))
            return web.json_response({'ok':True,'archived':True})
        b=await req.json();name=str(b.get('name',c['name'])).strip()
        if not 1<=len(name)<=100:raise ValueError('Campaign name must have 1-100 characters')
        interval=int(b.get('interval_minutes',c['interval_minutes']))
        if not 15<=interval<=10080:raise ValueError('Interval must be 15-10080 minutes')
        mode=str(b.get('mode',c['mode']))
        if mode not in ('browser','auto','fast'):raise ValueError('Invalid mode')
        from .platform import sheet_identity
        if b.get('sheet_url'):
            sid,gid=sheet_identity(str(b['sheet_url']))
        else:sid,gid=c['sheet_id'],c['gid']
        db.db.execute('''UPDATE campaigns SET name=?,sheet_id=?,gid=?,interval_minutes=?,mode=?,sync_hash=NULL WHERE id=?''',
                      (name,sid,gid,interval,mode,cid))
        db.audit(req['user']['id'],'campaign_edited',str(cid))
        return web.json_response({'ok':True})

    async def members(req):
        cid=int(req.match_info['id']);require_campaign(db,req['user'],cid)
        if req['user']['role']!='admin':raise web.HTTPForbidden()
        if req.method=='GET':
            return web.json_response({'members':db.many('''SELECT u.id,u.username,u.role FROM users u
            JOIN memberships m ON m.user_id=u.id WHERE m.campaign_id=?''',(cid,))})
        b=await req.json();uid=int(b.get('user_id',0));action=b.get('action')
        if not db.one('SELECT id FROM users WHERE id=? AND active=1',(uid,)):raise ValueError('User not found')
        if action=='assign':db.db.execute('INSERT OR IGNORE INTO memberships VALUES(?,?)',(uid,cid))
        elif action=='remove':db.db.execute('DELETE FROM memberships WHERE user_id=? AND campaign_id=?',(uid,cid))
        else:raise ValueError('Invalid membership action')
        db.audit(req['user']['id'],'campaign_membership',f'{cid}:{uid}:{action}')
        return web.json_response({'ok':True})

    async def user_edit(req):
        if req['user']['role']!='admin':raise web.HTTPForbidden()
        uid=int(req.match_info['id']);user=db.one('SELECT * FROM users WHERE id=?',(uid,))
        if not user:raise web.HTTPNotFound()
        b=await req.json();role=str(b.get('role',user['role']));active=int(bool(b.get('active',user['active'])))
        if role not in ('admin','manager','employee'):raise ValueError('Invalid role')
        if uid==req['user']['id'] and (role!='admin' or not active):raise ValueError('Cannot remove your own administrator access')
        db.db.execute('UPDATE users SET role=?,active=? WHERE id=?',(role,active,uid))
        if not active:db.db.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
        db.audit(req['user']['id'],'user_updated',f'{uid}:{role}:{active}')
        return web.json_response({'ok':True})

    async def summary(req):
        u=req['user']
        if u['role']=='admin':campaigns=db.many('SELECT * FROM campaigns ORDER BY id DESC')
        else:campaigns=db.many('''SELECT DISTINCT c.* FROM campaigns c LEFT JOIN memberships m ON m.campaign_id=c.id
           WHERE c.created_by=? OR m.user_id=? ORDER BY c.id DESC''',(u['id'],u['id']))
        ids=[c['id'] for c in campaigns]
        counts={'campaigns':len(campaigns),'active_campaigns':sum(c['active'] for c in campaigns),
                'styles':0,'oos':0,'errors':0,'available':0,'checks':0}
        latest=[];last_sync=[]
        for c in campaigns:
            items=db.one('SELECT COUNT(*) n FROM campaign_items WHERE campaign_id=?',(c['id'],))['n']
            counts['styles']+=items
            if c['last_run_id']:
                runs=list(req.app[jobskey].store.iter_results(c['last_run_id']))
                for r in runs:
                    status=r['status']
                    if status in ('PARTIAL_OOS','ALL_OOS'):counts['oos']+=1
                    elif status=='ERROR':counts['errors']+=1
                    elif status=='OK':counts['available']+=1
                    latest.append({'campaign_id':c['id'],'campaign_name':c['name'],**{k:r[k] for k in ('style_id','status','oos_sizes','in_sizes','checked_at_str','brand','name','error')}})
                counts['checks']+=len(runs)
            if c['synced_at']:last_sync.append({'campaign':c['name'],'at':c['synced_at'],'error':c['sync_error']})
        latest.sort(key=lambda r:r['checked_at_str'] or '',reverse=True)
        return web.json_response({'counts':counts,'latest':latest[:150],'last_sync':sorted(last_sync,key=lambda x:x['at'],reverse=True)[:1]})

    async def platform_events(req):
        u=req['user']
        if u['role']=='admin':rows=db.many('''SELECT e.*,c.name campaign_name FROM campaign_events e
             JOIN campaigns c ON c.id=e.campaign_id ORDER BY e.id DESC LIMIT 2000''')
        else:rows=db.many('''SELECT e.*,c.name campaign_name FROM campaign_events e
             JOIN campaigns c ON c.id=e.campaign_id LEFT JOIN memberships m ON m.campaign_id=c.id
             WHERE c.created_by=? OR m.user_id=? GROUP BY e.id ORDER BY e.id DESC LIMIT 2000''',(u['id'],u['id']))
        return web.json_response({'events':rows})

    async def report_csv(req):
        cid=int(req.match_info['id']);require_campaign(db,req['user'],cid)
        rows=db.many('SELECT style_id,sizes,active FROM campaign_items WHERE campaign_id=? ORDER BY style_id',(cid,))
        buf=io.StringIO();w=csv.writer(buf);w.writerow(['style_id','sizes','active']);w.writerows([(r['style_id'],r['sizes'],r['active']) for r in rows])
        db.audit(req['user']['id'],'campaign_csv_export',str(cid))
        return web.Response(text=buf.getvalue(),content_type='text/csv',headers={'Content-Disposition':f'attachment; filename="campaign_{cid}_items.csv"'})

    async def schedules(req):
        u=req['user']
        if req.method=='GET':
            rows=db.many('''SELECT s.*,c.name campaign_name FROM report_schedules s JOIN campaigns c ON c.id=s.campaign_id''')
            authorized=[]
            for r in rows:
                try:require_campaign(db,u,r['campaign_id']);authorized.append(r)
                except web.HTTPException:pass
            return web.json_response({'schedules':authorized})
        if u['role'] not in ('admin','manager'):raise web.HTTPForbidden()
        body=await req.json();cid=int(body.get('campaign_id',0));require_campaign(db,u,cid)
        freq=int(body.get('frequency_hours',24))
        if not 1<=freq<=720:raise ValueError('Frequency must be 1-720 hours')
        cur=db.db.execute('INSERT INTO report_schedules(campaign_id,frequency_hours,auto_drive,created_by,created_at) VALUES(?,?,?,?,?)',
                          (cid,freq,int(bool(body.get('auto_drive',False))),u['id'],time.time()))
        db.audit(u['id'],'report_schedule_created',str(cur.lastrowid))
        return web.json_response({'id':cur.lastrowid},status=201)

    async def remove_schedule(req):
        if req['user']['role'] not in ('admin','manager'):raise web.HTTPForbidden()
        sid=int(req.match_info['id']);schedule=db.one('SELECT * FROM report_schedules WHERE id=?',(sid,))
        if not schedule:raise web.HTTPNotFound()
        require_campaign(db,req['user'],schedule['campaign_id'])
        db.db.execute('DELETE FROM report_schedules WHERE id=?',(sid,));db.audit(req['user']['id'],'report_schedule_deleted',str(sid))
        return web.json_response({'ok':True})

    app.router.add_get('/api/workspace',workspace_api)
    app.router.add_post('/api/workspace',workspace_api)
    app.router.add_get('/api/summary',summary)
    app.router.add_get('/api/events',platform_events)
    app.router.add_put('/api/campaigns/{id:\\d+}',campaign_edit)
    app.router.add_delete('/api/campaigns/{id:\\d+}',campaign_edit)
    app.router.add_get('/api/campaigns/{id:\\d+}/members',members)
    app.router.add_post('/api/campaigns/{id:\\d+}/membership',members)
    app.router.add_get('/api/campaigns/{id:\\d+}/csv',report_csv)
    app.router.add_put('/api/users/{id:\\d+}',user_edit)
    app.router.add_get('/api/schedules',schedules)
    app.router.add_post('/api/schedules',schedules)
    app.router.add_delete('/api/schedules/{id:\\d+}',remove_schedule)
