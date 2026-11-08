"""Optional Google OAuth + Drive integration.

OAuth is server-side; tokens are encrypted at rest. No credentials are included in the release.
Sheet access falls back to service account / publicly readable URLs when OAuth is absent.
"""
from __future__ import annotations
import asyncio
import base64
import io
import json
import os
import secrets
import time
from urllib.parse import quote, urlencode

from aiohttp import web, ClientSession, ClientTimeout

GOOGLE_SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets.readonly',
    'https://www.googleapis.com/auth/drive.file',
]

def cipher():
    from cryptography.fernet import Fernet
    key = os.getenv('PLATFORM_ENCRYPTION_KEY', '')
    if not key:
        raise RuntimeError('PLATFORM_ENCRYPTION_KEY is required for Google OAuth; generate a Fernet key')
    return Fernet(key.encode())

def init_google_schema(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS google_connections(
      user_id INTEGER PRIMARY KEY, email TEXT, encrypted_credentials BLOB NOT NULL, connected_at REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS oauth_states(state_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
      expires REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS drive_exports(id INTEGER PRIMARY KEY, campaign_id INTEGER NOT NULL,
      run_id TEXT NOT NULL, file_id TEXT NOT NULL, url TEXT NOT NULL, uploaded_at REAL NOT NULL,
      UNIQUE(campaign_id,run_id));
    CREATE TABLE IF NOT EXISTS campaign_events(id INTEGER PRIMARY KEY, campaign_id INTEGER NOT NULL,
      run_id TEXT, style_id TEXT, status TEXT NOT NULL, oos_sizes TEXT, in_sizes TEXT,
      checked_at REAL NOT NULL, UNIQUE(run_id,style_id));
    ''')

def oauth_config():
    cid = os.getenv('GOOGLE_OAUTH_CLIENT_ID','')
    secret = os.getenv('GOOGLE_OAUTH_CLIENT_SECRET','')
    redirect = os.getenv('GOOGLE_OAUTH_REDIRECT_URI','')
    if not cid or not secret or not (redirect.startswith('https://') or redirect.startswith('http://127.0.0.1:') or redirect.startswith('http://localhost:')):
        raise ValueError('Set GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET and GOOGLE_OAUTH_REDIRECT_URI (HTTPS publicly, HTTP localhost for local testing)')
    return cid,secret,redirect

def oauth_start_url(db,user_id):
    cid,_,redirect = oauth_config()
    state = secrets.token_urlsafe(32)
    import hashlib
    db.execute('INSERT INTO oauth_states VALUES(?,?,?)', (hashlib.sha256(state.encode()).hexdigest(),user_id,time.time()+600))
    return 'https://accounts.google.com/o/oauth2/v2/auth?'+urlencode({
        'client_id':cid,'redirect_uri':redirect,'response_type':'code',
        'scope':' '.join(GOOGLE_SCOPES),'access_type':'offline','prompt':'consent',
        'state':state,'include_granted_scopes':'true'})

async def oauth_finish(db,state,code,expected_user):
    import hashlib
    tokenhash = hashlib.sha256(state.encode()).hexdigest()
    found = db.execute('SELECT * FROM oauth_states WHERE state_hash=?',(tokenhash,)).fetchone()
    db.execute('DELETE FROM oauth_states WHERE state_hash=?',(tokenhash,))
    if not found or found['expires'] < time.time() or found['user_id'] != expected_user:
        raise ValueError('Google connection expired or invalid. Start again.')
    cid,secret,redirect = oauth_config()
    async with ClientSession(timeout=ClientTimeout(total=25)) as session:
        async with session.post('https://oauth2.googleapis.com/token',data={
            'client_id':cid,'client_secret':secret,'code':code,
            'redirect_uri':redirect,'grant_type':'authorization_code'}) as resp:
            payload = await resp.json()
            if resp.status != 200:
                raise ValueError('Google authorization failed')
    if not payload.get('refresh_token'):
        raise ValueError('Google did not provide offline access; please reconnect with consent')
    # The service-side store is encrypted using a deployment secret, never browser-side.
    encrypted=cipher().encrypt(json.dumps(payload).encode())
    db.execute('INSERT INTO google_connections VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET encrypted_credentials=excluded.encrypted_credentials, connected_at=excluded.connected_at',
               (expected_user,'Google account',encrypted,time.time()))

def get_google_credentials(db,owner):
    row=db.execute('SELECT encrypted_credentials FROM google_connections WHERE user_id=?',(owner,)).fetchone()
    if not row:
        return None
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    payload=json.loads(cipher().decrypt(row['encrypted_credentials']).decode())
    cid,secret,_=oauth_config()
    creds=Credentials(token=payload.get('access_token'), refresh_token=payload['refresh_token'],
        token_uri='https://oauth2.googleapis.com/token',client_id=cid,client_secret=secret,scopes=GOOGLE_SCOPES)
    # Refresh on each request, avoiding untracked access-token expiry across restarts.
    creds.refresh(Request())
    return creds

def sheet_csv_with_oauth(db, owner, sheet_id, gid):
    from google.auth.transport.requests import AuthorizedSession
    creds=get_google_credentials(db,owner)
    if not creds: return None
    sess=AuthorizedSession(creds)
    try:
        res=sess.get(f'https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}',
            params={'fields':'sheets(properties(sheetId,title))'},timeout=25)
        res.raise_for_status()
        title=next((s['properties']['title'] for s in res.json().get('sheets',[])
            if str(s['properties']['sheetId']) == str(gid)),None)
        if title is None:raise ValueError('Google worksheet gid not found')
        res=sess.get(f'https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{quote(title,safe="")}',timeout=25)
        res.raise_for_status()
        import csv
        out=io.StringIO();csv.writer(out).writerows(res.json().get('values',[]))
        return out.getvalue()
    finally:sess.close()

async def load_sheet(db, campaign, fallback):
    owner=campaign['created_by']
    exists=db.execute('SELECT 1 FROM google_connections WHERE user_id=?',(owner,)).fetchone()
    if exists:
        return await asyncio.to_thread(sheet_csv_with_oauth,db,owner,campaign['sheet_id'],campaign['gid'])
    return await fallback(campaign['sheet_id'], campaign['gid'])

def upload_drive_file(db, owner, path, filename, folder_id=None):
    from google.auth.transport.requests import AuthorizedSession
    creds=get_google_credentials(db,owner)
    if not creds: raise ValueError('Google OAuth account not connected')
    if not path.is_file():raise FileNotFoundError(str(path))
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    service=build('drive','v3',credentials=creds,cache_discovery=False)
    meta={'name':filename,'mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}
    if folder_id:meta['parents']=[folder_id]
    media=MediaFileUpload(str(path),mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',resumable=True)
    uploaded=service.files().create(body=meta,media_body=media,fields='id,webViewLink').execute()
    return uploaded['id'],uploaded.get('webViewLink',f'https://drive.google.com/file/d/{uploaded["id"]}/view')
