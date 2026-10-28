import asyncio
import json
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock

import pytest

from app.web import create_app
from test_auth import OWNER, VIEWER, cookie, header, oauth_settings, session
from test_multibot import request, settings


def mounted_settings(path):
    return replace(oauth_settings(path), base_path='/panel', oauth_redirect_uri='http://localhost/panel/auth/discord/callback')


def test_prefix_boundaries_and_public_asset_urls(tmp_path):
    async def scenario():
        app = create_app(mounted_settings(tmp_path))
        status, _, headers = await request(app, 'GET', '/panel', return_headers=True)
        assert status == 308 and header(headers,'location') == '/panel/'
        status, _, headers = await request(app,'GET','/panel/',return_headers=True)
        assert status == 303 and header(headers,'location') == '/panel/login'
        for path in ['/','/api/bots','/api/auth/me','/login','/panel2/api/auth/me','/panels/login']:
            assert (await request(app,'GET',path))[0] == 404
        assert (await request(app,'GET','/panel/api/bots'))[0] == 401
        status, body = await request(app,'GET','/panel/login')
        assert status == 200
        page = body.decode()
        assert 'http://localhost/panel/static/style.css' in page
        assert 'http://localhost/panel/static/urls.js' in page
        assert 'href="/panel/auth/discord/login"' in page
        assert '"base_path": "/panel"' in page
        status, body = await request(app,'GET','/panel/static/urls.js')
        assert status == 200 and b'PanelURLs' in body
        status, body = await request(app,'GET','/panel/static/style.css')
        assert status == 200 and b':root' in body
        assert json.loads((await request(app,'GET','/panel/api/auth/me'))[1])['authenticated'] is False
        _, owner_headers = session(app,OWNER)
        status, body = await request(app,'GET','/panel/',headers=owner_headers)
        assert status == 200 and b'manage-users' in body
        assert (await request(app,'GET','/panel/api/bots',headers=owner_headers))[0] == 200
    asyncio.run(scenario())


def test_oauth_and_role_checks_keep_prefix(tmp_path):
    async def scenario():
        app=create_app(mounted_settings(tmp_path))
        auth=app.state.auth
        auth.oauth.identify=AsyncMock(return_value={'id':OWNER,'display_name':'Owner'})
        status,_,headers=await request(app,'GET','/panel/auth/discord/login',return_headers=True)
        assert status == 303
        query=parse_qs(urlsplit(header(headers,'location')).query)
        assert query['redirect_uri']==['http://localhost/panel/auth/discord/callback']
        binding=cookie(headers,auth.flow_cookie)
        status,_,headers=await request(app,'GET','/panel/auth/discord/callback?state='+query['state'][0]+'&code=test',headers=[(b'cookie',f'{auth.flow_cookie}={binding.value}'.encode())],return_headers=True)
        assert status == 303 and header(headers,'location') == '/panel/'
        login_cookie=cookie(headers,auth.session_cookie)
        assert login_cookie['path'] == '/'
        status,_,headers=await request(app,'GET','/panel/auth/discord/callback?state=bad',return_headers=True)
        assert header(headers,'location') == '/panel/login?error=state'
        _, viewer_headers=session(app,VIEWER)
        auth.set_grant(VIEWER,'default','viewer')
        assert (await request(app,'GET','/panel/api/bots/default/messages',headers=viewer_headers))[0] == 200
        assert (await request(app,'POST','/panel/api/bots/default/messages',{'guild_id':'1','channel_id':'2','content':'test'},viewer_headers))[0] == 403
        assert (await request(app,'GET','/panel/api/admin/users',headers=viewer_headers))[0] == 403
        _, owner_headers=session(app,OWNER)
        assert (await request(app,'GET','/panel/api/admin/users',headers=owner_headers))[0] == 200
        assert (await request(app,'PUT',f'/panel/api/admin/users/{VIEWER}/bots/default',{'role':None},owner_headers[:1]))[0] == 403
        assert (await request(app,'PUT',f'/panel/api/admin/users/{VIEWER}/bots/default',{'role':None},owner_headers))[0] == 200
        assert (await request(app,'GET','/panel/api/bots/default/messages',headers=viewer_headers))[0] == 404
    asyncio.run(scenario())


@pytest.mark.parametrize('base_path',['panel','/panel/','//panel','/../panel','/p%2fanel','/panel?x=1'])
def test_invalid_mount_paths_rejected(tmp_path,base_path):
    with pytest.raises(RuntimeError):
        create_app(replace(settings(tmp_path),base_path=base_path))


def test_callback_must_match_mount_path(tmp_path):
    with pytest.raises(RuntimeError):
        create_app(replace(oauth_settings(tmp_path),base_path='/panel'))
