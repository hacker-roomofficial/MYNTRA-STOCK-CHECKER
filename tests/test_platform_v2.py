import time
import pytest
from aiohttp.test_utils import TestClient, TestServer
from stockwatch.config import Config
from stockwatch.platform import create_app, totp, check_totp, LOGIN_ATTEMPTS
from stockwatch.parser import parse_gateway_json
from stockwatch.models import Item, Status


def test_missing_and_unreadable_sizes_never_oos():
    assert parse_gateway_json(Item('1', ('XXXL',)), {'style': {'id':1,'sizes':[
        {'label':'S','available':True}]}}).status == Status.ERROR
    assert parse_gateway_json(Item('1'), {'style': {'id':1,'sizes':[]}}).status == Status.ERROR
    assert parse_gateway_json(Item('1'), {'style': {'id':1,'sizes':[
        {'label':'M'}]}}).status == Status.ERROR
    r=parse_gateway_json(Item('1',('3XL',)),{'style':{'id':1,'sizes':[
        {'label':'XXXL','available':False}]}})
    assert r.status == Status.ALL_OOS


def test_totp():
    import base64
    secret=base64.b32encode(b'12345678901234567890').decode()
    now=time.time()
    assert check_totp(secret, totp(secret,now))
    assert not check_totp(secret,'0000000')


@pytest.mark.asyncio
async def test_v2_auth_access_and_optional_integrations(tmp_path,monkeypatch):
    LOGIN_ATTEMPTS.clear()
    monkeypatch.setenv('PLATFORM_ADMIN_USER','admin_v2')
    monkeypatch.setenv('PLATFORM_ADMIN_PASSWORD','A-great-password-12345')
    monkeypatch.setenv('PLATFORM_SYNC_SECONDS','3600')
    cfg=Config();cfg.data_dir=str(tmp_path);cfg.out_dir=str(tmp_path/'out')
    cfg.db_path=str(tmp_path/'out'/'monitor.db')
    client=TestClient(TestServer(create_app(cfg)))
    await client.start_server()
    try:
        login=await client.post('/login',data={'username':'admin_v2','password':'A-great-password-12345'},allow_redirects=False)
        assert login.status==302
        user=await (await client.get('/api/me')).json()
        csrf=user['csrf'];headers={'X-CSRF-Token':csrf}
        assert (await client.get('/api/google/status')).status==200
        assert (await client.get('/api/audit')).status==200
        assert (await client.post('/api/password',json={'current':'wrong','new':'good-password-123456'},headers=headers)).status==400
        m=await client.post('/api/mfa',json={'action':'begin','password':'A-great-password-12345'},headers=headers)
        assert m.status==200
        secret=(await m.json())['secret']
        response=await client.post('/api/mfa',json={'action':'verify','code':totp(secret)},headers=headers)
        assert response.status==200
        await client.post('/api/logout',headers=headers)
        assert (await client.post('/login',data={'username':'admin_v2','password':'A-great-password-12345'},allow_redirects=False)).status==401
        assert (await client.post('/login',data={'username':'admin_v2','password':'A-great-password-12345','otp':totp(secret)},allow_redirects=False)).status==302
        m=await (await client.get('/api/me')).json()
        csrf=m['csrf'];headers={'X-CSRF-Token':csrf}
        created=await client.post('/api/campaigns',json={'name':'Test campaign','sheet_url':'abc1234567890abcdefghijklmnop'},headers=headers)
        assert created.status==201
        cid=(await created.json())['id']
        assert (await client.get(f'/api/campaigns/{cid}/analytics')).status==200
        assert (await client.get(f'/api/campaigns/{cid}/history')).status==200
        assert (await client.post(f'/api/campaigns/{cid}/toggle',json={'active':False},headers=headers)).status==200
        assert (await client.post(f'/api/campaigns/{cid}/settings',json={'drive_folder_id':'abcdefghijklm','auto_drive':True},headers=headers)).status==200
        assert (await client.get(f'/api/campaigns/{cid}/settings')).status==200
        assert (await client.get(f'/api/campaigns/{cid}/drive')).status==200
    finally:
        await client.close()
