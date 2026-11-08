"""V3 routes, access controls, and usable UI assets."""
import os
import pytest
from aiohttp.test_utils import TestServer, TestClient
from stockwatch.config import Config
from stockwatch.platform import create_app

@pytest.mark.asyncio
async def test_brand_and_navigation_and_access(tmp_path,monkeypatch):
    monkeypatch.setenv('PLATFORM_ADMIN_USER','rootadmin')
    monkeypatch.setenv('PLATFORM_ADMIN_PASSWORD','VERY-strong-test-pass-1234!')
    monkeypatch.setenv('PLATFORM_SYNC_SECONDS','3600')
    cfg=Config();cfg.data_dir=str(tmp_path);cfg.out_dir=str(tmp_path/'out');cfg.db_path=str(tmp_path/'out'/'monitor.db')
    client=TestClient(TestServer(create_app(cfg)));await client.start_server()
    try:
        assert (await client.get('/assets/styles.css')).status==200
        assert (await client.get('/assets/app.js')).status==200
        pre=await (await client.get('/api/workspace')).json()
        assert pre['configured'] is False
        invalid=await client.post('/api/workspace',json={'brand_name':'Random Corp'})
        assert invalid.status==401
        login=await client.post('/login',data={'username':'rootadmin','password':'VERY-strong-test-pass-1234!'},allow_redirects=False)
        assert login.status==302
        me=await (await client.get('/api/me')).json();h={'X-CSRF-Token':me['csrf']}
        dash=await client.get('/');assert dash.status==200
        txt=await dash.text();assert '/assets/app.js' in txt and 'TWINS LADY' not in txt
        b=await client.post('/api/workspace',json={'brand_name':'My Retail Brand','accent':'#123ABC','logo_initials':'MR','theme_default':'light'},headers=h)
        assert b.status==200,await b.text()
        assert (await (await client.get('/api/workspace')).json())['brand_name']=='My Retail Brand'
        new=await client.post('/api/campaigns',json={'name':'Retail tracking','sheet_url':'https://docs.google.com/spreadsheets/d/abcdefghij1234567890ABCDxyz/edit#gid=0'},headers=h)
        assert new.status==201,await new.text();cid=(await new.json())['id']
        upd=await client.put(f'/api/campaigns/{cid}',json={'name':'Updated Retail','interval_minutes':60,'mode':'browser'},headers=h)
        assert upd.status==200,await upd.text()
        res=await client.get('/api/summary');assert res.status==200
        assert (await client.get('/api/events')).status==200
        created=await client.post('/api/users',json={'username':'operator','password':'operatorpass-1234','role':'employee'},headers=h)
        uid=(await created.json())['id']
        assign=await client.post(f'/api/campaigns/{cid}/membership',json={'user_id':uid,'action':'assign'},headers=h)
        assert assign.status==200,await assign.text()
        members=await (await client.get(f'/api/campaigns/{cid}/members')).json()
        assert any(x['id']==uid for x in members['members'])
        assert (await client.get(f'/api/campaigns/{cid}/csv')).status==200
        sc=await client.post('/api/schedules',json={'campaign_id':cid,'frequency_hours':24,'auto_drive':True},headers=h)
        assert sc.status==201,await sc.text()
        assert len((await (await client.get('/api/schedules')).json())['schedules'])==1
        assert (await client.put(f'/api/users/{uid}',json={'role':'employee','active':True},headers=h)).status==200
        await client.post('/api/logout',headers=h)
        assert (await client.post('/login',data={'username':'operator','password':'operatorpass-1234'},allow_redirects=False)).status==302
        assert (await client.get('/api/campaigns')).status==200
        assert (await client.get('/api/users')).status==403
        assert (await client.post('/api/workspace',json={'brand_name':'Hacked'},headers={'X-CSRF-Token':(await (await client.get('/api/me')).json())['csrf']})).status==403
    finally: await client.close()
