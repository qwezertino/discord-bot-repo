import asyncio
import base64
import hashlib
import json
import time
from dataclasses import replace
from http.cookies import SimpleCookie
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from app.auth import FLOW_COOKIE, SESSION_COOKIE, DiscordOAuth, OAuthError, csrf_token, digest
from app.web import create_app
from test_multibot import profile, request, settings

OWNER = '202122215498448896'
OPERATOR = '300000000000000001'
VIEWER = '300000000000000002'
STRANGER = '300000000000000003'


def oauth_settings(path):
    return replace(settings(path), auth_mode='discord', owner_discord_id=OWNER,
                   oauth_client_id='123456789012345678', oauth_client_secret='test-oauth-secret',
                   oauth_redirect_uri='http://localhost/auth/discord/callback')


def fixture_app(path):
    app = create_app(oauth_settings(path))
    app.state.database.save_profile(profile('other'))
    return app


def session(app, user_id):
    token = app.state.auth.create_session({'id': user_id, 'display_name': 'User ' + user_id})
    return token, [(b'cookie', f'{app.state.auth.session_cookie}={token}'.encode()), (b'x-csrf-token', csrf_token(token).encode()), (b'accept-language', b'en')]


def header(headers, name):
    return next((value.decode() for key, value in headers if key.decode() == name), '')


def cookie(headers, name):
    for key, value in headers:
        if key == b'set-cookie':
            jar = SimpleCookie(); jar.load(value.decode())
            if name in jar:
                return jar[name]
    raise AssertionError(f'Cookie {name} was not set')


def test_unauthenticated_requests_basic_cannot_bypass_oauth_and_canonical_host(tmp_path):
    async def scenario():
        app = create_app(replace(oauth_settings(tmp_path), panel_password="test-password"))
        assert (await request(app, 'GET', '/api/bots'))[0] == 401
        assert (await request(app, 'GET', '/docs'))[0] == 303
        status, body, headers = await request(app, 'GET', '/', return_headers=True)
        assert status == 303 and header(headers, 'location') == '/login'
        status, body = await request(app, 'GET', '/login')
        assert status == 200 and b'discord-login' in body
        assert b'test-oauth-secret' not in body
        assert json.loads((await request(app, 'GET', '/api/auth/me'))[1])['authenticated'] is False
        basic = base64.b64encode(b'admin:test-password')
        assert (await request(app, 'GET', '/api/bots', headers=[(b'authorization', b'Basic ' + basic)]))[0] == 401
        # A mismatch with the configured public URL is rejected before any OAuth work.
        assert (await request(app, 'GET', '/auth/discord/login', scheme='https'))[0] == 400
    asyncio.run(scenario())


@pytest.mark.parametrize('role', ['viewer', 'operator', None])
def test_every_bot_api_enforces_role_and_hides_unassigned_profiles(tmp_path, role):
    async def scenario():
        app = fixture_app(tmp_path)
        _, headers = session(app, OPERATOR)
        if role:
            app.state.auth.set_grant(OPERATOR, 'default', role)
        status, body = await request(app, 'GET', '/api/bots', headers=headers)
        visible = json.loads(body)
        assert status == 200
        assert [bot['id'] for bot in visible] == (['default'] if role else [])
        if role:
            assert visible[0]['access_role'] == role
            assert 'token_env' not in visible[0] and 'application_id' not in visible[0]
        service = SimpleNamespace(
            status=lambda: {"connected": True, "last_error": None, "user": "mock", "guild_count": 1},
            list_guilds=lambda: [], list_channels=lambda _: [], list_roles=lambda _: [],
            send_message=AsyncMock(return_value=SimpleNamespace(guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=2), id=3, jump_url='test')),
            edit_message=AsyncMock(), delete_message=AsyncMock(),
            import_own_messages=AsyncMock(return_value={'created': 0}),
            create_reaction_role=AsyncMock(return_value=('unicode:✅', '✅')),
        )
        app.state.bots.services['default'] = service
        record = app.state.database.add_message(1, 2, 4, {})
        rule_id = app.state.database.add_reaction_role(1, 2, 4, 'e', 'e', 5, True)
        for suffix in ['status', 'guilds', 'guilds/1/channels', 'guilds/1/roles', 'messages', 'reaction-roles', 'audit']:
            assert (await request(app, 'GET', '/api/bots/default/' + suffix, headers=headers))[0] == (200 if role else 404)
            assert (await request(app, 'GET', '/api/bots/other/' + suffix, headers=headers))[0] == 404
        payload = {'guild_id': '1', 'channel_id': '2', 'content': 'hello'}
        writes = [
            ('POST', 'messages', payload, 201), ('PUT', f'messages/{record}', payload, 200),
            ('POST', 'messages/import', {'guild_id': '1'}, 200),
            ('POST', 'reaction-roles', {'guild_id':'1','channel_id':'2','message_id':'3','role_id':'4','emoji':'✅'}, 201),
            ('DELETE', f'reaction-roles/{rule_id}', None, 200), ('DELETE', f'messages/{record}', None, 200),
            # Lack of a multipart file is a validation error only after authorization.
            ('POST', 'uploads', None, 422),
        ]
        for method, suffix, body, success in writes:
            status, _ = await request(app, method, '/api/bots/default/' + suffix, body, headers)
            assert status == (success if role == 'operator' else 403 if role else 404), suffix
        if role != 'operator':
            service.send_message.assert_not_awaited()
            service.edit_message.assert_not_awaited()
        for method, path, body in [
            ('POST', '/api/bots', profile('new')), ('PUT', '/api/bots/default', profile('default')),
            ('POST', '/api/bots/default/reconnect', None), ('GET', '/api/admin/users', None),
            ('PUT', f'/api/admin/users/{OPERATOR}/bots/default', {'role':'operator'}),
            ('PUT', f'/api/admin/users/{OWNER}', {'blocked': True}),
        ]:
            assert (await request(app, method, path, body, headers))[0] in {403, 404}
    asyncio.run(scenario())


def test_owner_grants_revokes_blocks_and_cannot_lock_out_owner(tmp_path):
    async def scenario():
        app = fixture_app(tmp_path)
        _, owner_headers = session(app, OWNER)
        _, user_headers = session(app, OPERATOR)
        _, second_session = session(app, OPERATOR)
        assert json.loads((await request(app, 'GET', '/api/auth/me', headers=owner_headers))[1])['owner'] is True
        grant_path = f'/api/admin/users/{OPERATOR}/bots/default'
        assert (await request(app, 'PUT', grant_path, {'role':'operator'}, owner_headers))[0] == 200
        assert len(json.loads((await request(app, 'GET', '/api/bots', headers=user_headers))[1])) == 1
        assert (await request(app, 'PUT', grant_path, {'role':None}, owner_headers))[0] == 200
        assert json.loads((await request(app, 'GET', '/api/bots', headers=user_headers))[1]) == []
        assert (await request(app, 'GET', '/api/bots/default/messages', headers=user_headers))[0] == 404
        assert (await request(app, 'PUT', grant_path, {'role':'owner'}, owner_headers))[0] == 422
        assert (await request(app, 'PUT', f'/api/admin/users/{OWNER}', {'blocked':True}, owner_headers))[0] == 400
        assert (await request(app, 'PUT', f'/api/admin/users/{OWNER}/bots/default', {'role':None}, owner_headers))[0] == 400
        assert (await request(app, 'PUT', f'/api/admin/users/{OPERATOR}', {'blocked':True}, owner_headers))[0] == 200
        for user_session in [user_headers, second_session]:
            assert (await request(app, 'GET', '/api/bots', headers=user_session))[0] == 401
        with pytest.raises(Exception) as exc:
            app.state.auth.create_session({'id':OPERATOR, 'display_name':'blocked'})
        assert getattr(exc.value, 'status_code', None) == 403
        assert (await request(app, 'PUT', f'/api/admin/users/{OPERATOR}', {'blocked':False}, owner_headers))[0] == 200
        # Unblocking does not resurrect revoked session cookies.
        assert (await request(app, 'GET', '/api/bots', headers=user_headers))[0] == 401
        audits = app.state.database.list_audit()
        assert {'access_changed', 'user_blocked', 'user_unblocked'} <= {entry['action'] for entry in audits}
        assert all(entry['actor_id'] == OWNER for entry in audits)
        users = json.loads((await request(app, 'GET', '/api/admin/users', headers=owner_headers))[1])
        assert next(user for user in users if user['id'] == OWNER)['owner'] is True
    asyncio.run(scenario())


def test_csrf_expiration_logout_and_hashed_session_storage(tmp_path):
    async def scenario():
        app = fixture_app(tmp_path)
        token, headers = session(app, OWNER)
        rows = app.state.database.fetch_all('SELECT * FROM panel_sessions')
        assert token not in json.dumps(rows)
        assert rows[0]['token_hash'] == digest(token)
        path = f'/api/admin/users/{VIEWER}/bots/default'
        assert (await request(app, 'PUT', path, {'role':'viewer'}, headers[:1]))[0] == 403
        bad = [*headers[:1], (b'x-csrf-token', b'wrong')]
        assert (await request(app, 'PUT', path, {'role':'viewer'}, bad))[0] == 403
        assert (await request(app, 'PUT', path, {'role':'viewer'}, [*headers, (b'origin', b'http://evil')]))[0] == 403
        assert (await request(app, 'POST', '/api/auth/logout', headers=headers))[0] == 200
        assert (await request(app, 'GET', '/api/bots', headers=headers))[0] == 401
        token, headers = session(app, OWNER)
        app.state.database.execute('UPDATE panel_sessions SET expires_at = ?', (int(time.time()) - 1,))
        assert (await request(app, 'GET', '/api/bots', headers=headers))[0] == 401
    asyncio.run(scenario())


def test_oauth_state_pkce_cookie_binding_rotation_and_replay(tmp_path):
    async def scenario():
        app = fixture_app(tmp_path)
        auth = app.state.auth
        auth.oauth.identify = AsyncMock(return_value={'id': OWNER, 'display_name':'Owner name'})
        status, _, headers = await request(app, 'GET', '/auth/discord/login?lang=en', return_headers=True)
        assert status == 303
        query = parse_qs(urlsplit(header(headers, 'location')).query)
        assert query['scope'] == ['identify'] and query['code_challenge_method'] == ['S256']
        assert 'test-oauth-secret' not in header(headers, 'location')
        binding = cookie(headers, FLOW_COOKIE)
        assert binding['httponly'] and binding['samesite'] == 'lax'
        state = query['state'][0]
        path = '/auth/discord/callback?' + urlencode({'state':state,'code':'test-code','iss':'https://discord.com'})
        bad = [(b'cookie', f'{FLOW_COOKIE}=wrong'.encode())]
        assert header((await request(app, 'GET', path, headers=bad, return_headers=True))[2], 'location') == '/login?error=state'
        auth.oauth.identify.assert_not_awaited()
        old_token, old_headers = session(app, OWNER)
        cookies = [(b'cookie', f'{FLOW_COOKIE}={binding.value}; {SESSION_COOKIE}={old_token}'.encode())]
        status, _, headers = await request(app, 'GET', path, headers=cookies, return_headers=True)
        assert status == 303 and header(headers, 'location') == '/'
        new_cookie = cookie(headers, SESSION_COOKIE)
        assert new_cookie.value != old_token and new_cookie['httponly'] and new_cookie['samesite'] == 'lax'
        code, verifier = auth.oauth.identify.call_args.args
        assert code == 'test-code'
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        assert challenge == query['code_challenge'][0]
        assert (await request(app, 'GET', '/api/bots', headers=old_headers))[0] == 401
        assert (await request(app, 'GET', '/api/bots', headers=[(b'cookie', f'{SESSION_COOKIE}={new_cookie.value}'.encode())]))[0] == 200
        status, _, headers = await request(app, 'GET', path, headers=cookies, return_headers=True)
        assert header(headers, 'location') == '/login?error=state'
        auth.oauth.identify.assert_awaited_once()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['expired','cancelled','issuer','provider','blocked'])
def test_oauth_failure_paths_are_closed_and_do_not_leak_secrets(tmp_path, failure):
    async def scenario():
        app = fixture_app(tmp_path); auth = app.state.auth
        auth.oauth.identify = AsyncMock(return_value={'id':STRANGER, 'display_name':'New user'})
        url, binding = auth.begin_login('uk')
        state = parse_qs(urlsplit(url).query)['state'][0]
        params = {'state':state, 'code':'secret-auth-code'}
        if failure == 'expired': auth.flows[digest(state)]['expires'] = 0
        if failure == 'cancelled': params['error'] = 'access_denied'
        if failure == 'issuer': params['iss'] = 'https://evil'
        if failure == 'provider': auth.oauth.identify.side_effect = OAuthError('test-oauth-secret')
        if failure == 'blocked': auth.set_blocked(STRANGER, True)
        status, body, headers = await request(app, 'GET', '/auth/discord/callback?' + urlencode(params), headers=[(b'cookie',f'{FLOW_COOKIE}={binding}'.encode())], return_headers=True)
        assert status == 303 and header(headers, 'location').startswith('/login?error=')
        assert not auth.database.fetch_all('SELECT * FROM panel_sessions')
        assert 'secret-auth-code' not in body.decode() + header(headers, 'location')
        assert 'test-oauth-secret' not in body.decode() + header(headers, 'location')
    asyncio.run(scenario())


def test_new_discord_identity_has_no_permissions_and_secure_cookies(tmp_path):
    async def scenario():
        config = replace(oauth_settings(tmp_path), oauth_redirect_uri='https://localhost/auth/discord/callback')
        app = create_app(config)
        app.state.auth.oauth.identify = AsyncMock(return_value={'id':STRANGER, 'display_name':'First visitor'})
        url, binding = app.state.auth.begin_login('en')
        state = parse_qs(urlsplit(url).query)['state'][0]
        status, _, headers = await request(app, 'GET', '/auth/discord/callback?' + urlencode({'state':state,'code':'test'}), headers=[(b'cookie',f'{app.state.auth.flow_cookie}={binding}'.encode())], return_headers=True, scheme='https')
        login_cookie = cookie(headers, app.state.auth.session_cookie)
        assert login_cookie['secure']
        user_headers = [(b'cookie',f'{app.state.auth.session_cookie}={login_cookie.value}'.encode())]
        assert json.loads((await request(app, 'GET', '/api/bots', headers=user_headers, scheme='https'))[1]) == []
        me = json.loads((await request(app, 'GET', '/api/auth/me', headers=user_headers, scheme='https'))[1])
        assert me['owner'] is False and me['user']['id'] == STRANGER
    asyncio.run(scenario())


@pytest.mark.parametrize('changes', [
    {'auth_mode':'invalid'}, {'oauth_client_secret':''}, {'owner_discord_id':''},
    {'oauth_client_id':'not-id'}, {'oauth_redirect_uri':'http://public.example/auth/discord/callback'},
    {'oauth_redirect_uri':'https://public.example/wrong'}, {'oauth_redirect_uri':'https://u:p@public.example/auth/discord/callback'},
])
def test_partial_or_unsafe_oauth_configuration_never_falls_back_to_local(tmp_path, changes):
    with pytest.raises(RuntimeError):
        create_app(replace(oauth_settings(tmp_path), **changes))


def test_discord_transport_uses_fixed_endpoints_and_never_retains_oauth_tokens(tmp_path, monkeypatch):
    import app.auth as auth_module
    calls = []
    class Response:
        status = 200
        def __init__(self, body): self.body = body
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self): return self.body
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def post(self, url, **kwargs):
            calls.append(('POST', url, kwargs))
            return Response({'token_type':'Bearer', 'scope':'identify', 'access_token':'test-access-token', 'refresh_token':'test-refresh-token'})
        def get(self, url, **kwargs):
            calls.append(('GET', url, kwargs))
            return Response({'id':OPERATOR, 'global_name':'A <user>', 'username':'user'})
    monkeypatch.setattr(auth_module.aiohttp, 'ClientSession', Client)
    client = DiscordOAuth(oauth_settings(tmp_path))
    user = asyncio.run(client.identify('test-code', 'test-verifier'))
    assert user == {'id':OPERATOR, 'display_name':'A <user>'}
    assert calls[0][1] == 'https://discord.com/api/v10/oauth2/token'
    assert calls[0][2]['data']['code_verifier'] == 'test-verifier'
    assert calls[0][2]['data']['client_secret'] == 'test-oauth-secret'
    assert calls[1][1] == 'https://discord.com/api/v10/users/@me'
    assert calls[1][2]['headers']['Authorization'] == 'Bearer test-access-token'
    assert all(call[2]['allow_redirects'] is False for call in calls)
    assert 'test-access-token' not in repr(client.__dict__)
    assert 'test-refresh-token' not in repr(client.__dict__)


def test_callback_query_redaction():
    import logging
    from app.logging_filters import OAuthQueryRedaction
    record = logging.LogRecord('uvicorn.access', logging.INFO, '', 1, '%s - "%s %s HTTP/%s" %d',
                               ('127.0.0.1', 'GET', '/auth/discord/%63allback?code=secret-code&state=secret-state', '1.1', 303), None)
    assert OAuthQueryRedaction().filter(record)
    assert 'secret-code' not in record.getMessage() and 'secret-state' not in record.getMessage()


def test_audit_has_request_actor_and_background_context_does_not_leak(tmp_path):
    async def scenario():
        app = fixture_app(tmp_path)
        _, headers = session(app, OPERATOR)
        app.state.auth.set_grant(OPERATOR, 'default', 'operator')
        message = SimpleNamespace(guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=2), id=123456789012345678, jump_url='test')
        app.state.bots.services['default'] = SimpleNamespace(send_message=AsyncMock(return_value=message))
        assert (await request(app, 'POST', '/api/bots/default/messages', {'guild_id':'1','channel_id':'2','content':'test'}, headers))[0] == 201
        assert app.state.database.list_audit()[0]['actor_id'] == OPERATOR
        app.state.database.audit('background', {})
        assert app.state.database.list_audit()[0]['actor_id'] is None
    asyncio.run(scenario())



def test_active_session_skips_oauth_and_survives_app_restart(tmp_path):
    async def scenario():
        app = fixture_app(tmp_path)
        token, headers = session(app, OWNER)
        for current in (app, create_app(oauth_settings(tmp_path))):
            current.state.auth.oauth.identify = AsyncMock()
            for path in ('/login', '/auth/discord/login'):
                status, _, response_headers = await request(current, 'GET', path, headers=headers, return_headers=True)
                assert status == 303
                assert header(response_headers, 'location') == '/'
            assert not current.state.auth.flows
            current.state.auth.oauth.identify.assert_not_awaited()
            me = json.loads((await request(current, 'GET', '/api/auth/me', headers=headers))[1])
            assert me['authenticated'] and me['owner'] and me['user']['id'] == OWNER
    asyncio.run(scenario())
