import asyncio
import os
import tempfile

import pytest
from aiohttp.test_utils import TestClient, TestServer
from stockwatch.config import Config
from stockwatch.platform import (PlatformDB, create_app, parse_sheet_csv, password_hash,
                                 sheet_identity, verify_password)


def test_sheet_formats_and_change_inputs():
    text = 'style_id,sizes,active\n46608964,"XS, S, 3XL",TRUE\n46608964,XXXL,TRUE\n45048632,"28, 30",FALSE\n'
    assert parse_sheet_csv(text) == {'46608964': 'S,XS,XXXL'}
    assert parse_sheet_csv('style_id,sizes\n123,\n123,M\n') == {'123': ''}
    with pytest.raises(ValueError):
        parse_sheet_csv('sizes\nM\n')
    with pytest.raises(ValueError):
        parse_sheet_csv('style_id\nabc\n')


def test_spreadsheet_id_url_restrictions():
    sid = 'abc1234567890abcdefghijklmnop'
    assert sheet_identity(f'https://docs.google.com/spreadsheets/d/{sid}/edit#gid=123') == (sid, '123')
    with pytest.raises(ValueError):
        sheet_identity('https://evil.com/spreadsheets/d/' + sid)
    with pytest.raises(ValueError):
        sheet_identity('http://docs.google.com/spreadsheets/d/' + sid)


def test_passwords():
    h = password_hash('very secure password')
    assert verify_password('very secure password', h)
    assert not verify_password('wrong', h)


@pytest.mark.asyncio
async def test_login_rbac_and_csrf(tmp_path, monkeypatch):
    monkeypatch.setenv('PLATFORM_ADMIN_USER', 'admin_test')
    monkeypatch.setenv('PLATFORM_ADMIN_PASSWORD', 'SecurePassword123456!')
    monkeypatch.setenv('PLATFORM_SYNC_SECONDS', '3600')
    cfg = Config()
    cfg.data_dir = str(tmp_path)
    cfg.out_dir = str(tmp_path / 'out')
    cfg.db_path = str(tmp_path / 'out' / 'stockwatch.db')
    server = TestServer(create_app(cfg))
    client = TestClient(server)
    await client.start_server()
    try:
        res = await client.get('/api/me', allow_redirects=False)
        assert res.status == 401
        res = await client.post('/login', data={'username': 'admin_test', 'password':'wrong'}, allow_redirects=False)
        assert res.status == 401
        res = await client.post('/login', data={'username':'admin_test','password':'SecurePassword123456!'}, allow_redirects=False)
        assert res.status == 302
        res = await client.get('/api/me')
        payload = await res.json()
        assert payload['user']['role'] == 'admin'
        csrf = payload['csrf']
        res = await client.post('/api/campaigns',json={'name':'Test','sheet_url':'abc1234567890abcdefghijklmnop'})
        assert res.status == 403
        res = await client.post('/api/campaigns', headers={'X-CSRF-Token':csrf},
                                json={'name':'Test','sheet_url':'abc1234567890abcdefghijklmnop'})
        assert res.status == 201
        res = await client.post('/api/users',headers={'X-CSRF-Token':csrf},
                                json={'username':'worker','password':'WorkerPassword!123','role':'employee'})
        assert res.status == 201
        res = await client.post('/api/logout',headers={'X-CSRF-Token':csrf})
        assert res.status == 200
        res = await client.get('/api/me')
        assert res.status == 401
    finally:
        await client.close()
