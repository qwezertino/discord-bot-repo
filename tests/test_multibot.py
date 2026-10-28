import asyncio
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot_manager import BotManager
from app.config import Settings
from app.database import Database
from app.discord_service import DiscordService, DiscordServiceError
from app.models import BotProfile
from app.web import create_app


def settings(path):
    return Settings('', '127.0.0.1', 8765, 'admin', None, path, 1, False)


def profile(bot_id):
    return BotProfile(id=bot_id, name=bot_id, token_env=f'{bot_id.upper()}_TOKEN').model_dump()


def databases(path):
    db = Database(path / 'test.sqlite3')
    db.initialize()
    db.save_profile(profile('other'))
    return db, db.scoped('other')


def test_sql_isolation_and_idempotent_import(tmp_path):
    first, second = databases(tmp_path)
    first_id, created = first.upsert_message(1, 2, 3, {'content': 'first'})
    assert created
    second_id, created = second.upsert_message(1, 2, 3, {'content': 'second'})
    assert created and second_id != first_id
    assert second.upsert_message(1, 2, 3, {'content': 'updated'}) == (second_id, False)
    assert first.get_message(first_id)['payload']['content'] == 'first'
    assert second.get_message(first_id) is None
    second.update_message(first_id, {'content': 'attack'})
    second.delete_message(first_id)
    assert first.get_message(first_id)
    first.add_upload('upload', 'image.png', 'stored.png', 'image/png', 12)
    assert second.get_upload('upload') is None
    rule = first.add_reaction_role(1, 2, 3, 'unicode:✅', '✅', 4, True)
    other_rule = second.add_reaction_role(1, 2, 3, 'unicode:✅', '✅', 5, True)
    second.delete_reaction_role(rule)
    assert second.get_reaction_role(rule) is None
    assert first.get_reaction_role(rule)
    second.delete_reaction_roles_for_message(3)
    assert first.find_reaction_role(3, 'unicode:✅')['id'] == rule
    assert second.get_reaction_role(other_rule) is None
    first.audit('first', {'message_id': '3'})
    second.audit('second', {})
    assert [r['action'] for r in first.list_audit()] == ['first']
    assert [r['action'] for r in second.list_audit()] == ['second']


def test_legacy_migration_preserves_all_tables(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    # Independent fixture of the previous public schema.
    schema = """
        CREATE TABLE managed_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL,
            channel_id TEXT NOT NULL, message_id TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE uploads (
            id TEXT PRIMARY KEY, original_name TEXT NOT NULL, stored_name TEXT NOT NULL,
            content_type TEXT, size_bytes INTEGER NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE reaction_roles (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL,
            channel_id TEXT NOT NULL, message_id TEXT NOT NULL, emoji_key TEXT NOT NULL,
            emoji_display TEXT NOT NULL, role_id TEXT NOT NULL,
            remove_on_unreact INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
            UNIQUE(message_id, emoji_key));
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
            details_json TEXT NOT NULL, created_at TEXT NOT NULL);
    """
    with sqlite3.connect(path) as connection:
        connection.executescript(schema)
        connection.execute("INSERT INTO managed_messages VALUES (9,'1','2','3','{}','old','old')")
        connection.execute("INSERT INTO uploads VALUES ('u','file','disk',NULL,42,'old')")
        connection.execute("INSERT INTO reaction_roles VALUES (7,'1','2','3','e','e','4',1,'old')")
        connection.execute("INSERT INTO audit_log VALUES (8,'sent','{}','old')")
        connection.execute("INSERT INTO managed_messages VALUES (99,'1','2','99','{}','old','old')")
        connection.execute("DELETE FROM managed_messages WHERE id = 99")
    db = Database(path)
    db.initialize()
    db.initialize()
    assert db.get_message(9)['message_id'] == '3'
    assert db.get_message(9)['created_at'] == 'old'
    assert db.add_message(1, 2, 4, {}) == 100
    assert db.get_upload('u')['stored_name'] == 'disk'
    assert db.get_reaction_role(7)['role_id'] == '4'
    assert db.list_audit()[0]['id'] == 8
    db.save_profile(profile('other'))
    assert db.scoped('other').upsert_message(1, 2, 3, {})[1]
    with sqlite3.connect(path) as connection:
        assert not connection.execute('PRAGMA foreign_key_check').fetchall()
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 2


async def request(app, method, path, body=None, headers=(), return_headers=False, scheme="http"):
    sent = []
    messages = asyncio.Queue()
    await messages.put({'type': 'http.request', 'body': body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else b'', 'more_body': False})
    async def receive():
        return await messages.get()
    async def send(message):
        sent.append(message)
    route, _, query = path.partition('?')
    scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.3'},
             'http_version': '1.1', 'method': method, 'scheme': scheme,
             'path': route, 'raw_path': route.encode(), 'query_string': query.encode(),
             'root_path': '', 'headers': [(b'host', b'localhost'), *(([(b'content-type', b'application/json')]) if not any(key == b'content-type' for key, _ in headers) else []), *headers],
             'client': ('127.0.0.1', 1), 'server': ('localhost', 80)}
    await asyncio.wait_for(app(scope, receive, send), timeout=3)
    status = next(message['status'] for message in sent if message['type'] == 'http.response.start')
    content = b''.join(message.get('body', b'') for message in sent)
    if return_headers:
        response_headers = next(message["headers"] for message in sent if message["type"] == "http.response.start")
        return status, content, response_headers
    return status, content


def test_api_empty_start_profiles_isolation_and_security(tmp_path):
    async def scenario():
        app = create_app(settings(tmp_path))
        async with app.router.lifespan_context(app):
            assert not app.state.bots.tasks
            status, body = await request(app, 'GET', '/')
            assert status == 200 and b'bot-select' in body
            status, body = await request(app, 'GET', '/api/bots/default/status')
            assert json.loads(body)['state'] == 'missing_token'
            assert (await request(app, 'POST', '/api/bots', profile('other')))[0] == 201
            assert (await request(app, 'POST', '/api/bots', profile('other')))[0] == 409
            assert (await request(app, 'GET', '/api/bots/missing/messages'))[0] == 404
            db = app.state.database
            record = db.add_message(1, 2, 3, {})
            rule = db.add_reaction_role(1, 2, 3, 'e', 'e', 4, True)
            # Connected clients are mocked; no network or Discord operations.
            service = SimpleNamespace(edit_message=AsyncMock(), delete_message=AsyncMock(), close=AsyncMock())
            app.state.bots.services['other'] = service
            for method in ('DELETE', 'PUT'):
                payload = {'guild_id': '1', 'channel_id': '2', 'content': 'attack'} if method == 'PUT' else None
                assert (await request(app, method, f'/api/bots/other/messages/{record}', payload))[0] == 404
            service.edit_message.assert_not_awaited()
            service.delete_message.assert_not_awaited()
            assert json.loads((await request(app, 'GET', '/api/bots/other/messages'))[1]) == []
            assert (await request(app, 'DELETE', f'/api/bots/other/reaction-roles/{rule}'))[0] == 404
            assert (await request(app, 'POST', '/api/bots', profile('bad'), [(b'origin', b'http://evil')]))[0] == 403
            del app.state.bots.services['other']
            assert (await request(app, 'GET', '/api/bots/other/guilds'))[0] == 409
            assert not app.state.bots.tasks
    asyncio.run(scenario())


def test_lifecycle_failure_restart_disable_and_secret_safety(tmp_path, monkeypatch):
    async def scenario():
        db, _ = databases(tmp_path)
        monkeypatch.setenv('OTHER_TOKEN', 'test-other')
        services = []
        class FakeService:
            def __init__(self, config, database):
                self.config, self.database = config, database
                self.client = SimpleNamespace(start=AsyncMock(side_effect=RuntimeError('test-secret')), is_closed=lambda: False, close=AsyncMock())
                self.last_error = None
                self.closed = False
                services.append(self)
            async def run(self):
                if self.database.bot_id == 'default':
                    await DiscordService.run(self)
                else:
                    await asyncio.Event().wait()
            async def close(self):
                self.closed = True
            def status(self):
                return {'connected': not self.last_error, 'last_error': self.last_error}
        manager = BotManager(replace(settings(tmp_path), discord_token='test-secret'), db, FakeService)
        await manager.start()
        await asyncio.sleep(0)
        assert manager.tasks['default'].done()
        assert not manager.tasks['other'].done()
        assert 'test-secret' not in json.dumps(manager.status(manager.profile('default')))
        original = manager.services['other']
        await manager.restart('other')
        assert original.closed
        assert manager.services['other'] is not original
        disabled = profile('other') | {'enabled': False}
        db.save_profile(disabled)
        await manager.restart('other')
        assert 'other' not in manager.services
        tasks = list(manager.tasks.values())
        await manager.close()
        assert not manager.tasks and not manager.services
        assert all(task.done() for task in tasks)
    asyncio.run(scenario())


def test_reject_foreign_attachments_and_authors(tmp_path):
    db, second = databases(tmp_path)
    db.add_upload('foreign', 'x', 'x', None, 1)
    service = DiscordService(settings(tmp_path), second)
    with pytest.raises(DiscordServiceError, match='не найдена'):
        service._prepare_files({'upload_ids': ['foreign']})
    service.client = SimpleNamespace(user=SimpleNamespace(id=10))
    with pytest.raises(DiscordServiceError, match='другому боту'):
        service._ensure_own_message(SimpleNamespace(author=SimpleNamespace(id=11)))


def test_import_own_messages_and_reaction_events_are_scoped(tmp_path):
    async def scenario():
        first, second = databases(tmp_path)
        message = SimpleNamespace(id=3, guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=2), author=SimpleNamespace(id=10), content='hello', embeds=[], attachments=[], mention_everyone=False, role_mentions=[], mentions=[])
        async def history(**kwargs):
            yield message
        channel = SimpleNamespace(id=2, name='test', history=history, permissions_for=lambda _: SimpleNamespace(view_channel=True, read_message_history=True))
        guild = SimpleNamespace(id=1, me=object(), text_channels=[channel])
        service = DiscordService(settings(tmp_path), first)
        service.client = SimpleNamespace(user=SimpleNamespace(id=10), is_ready=lambda: True, get_guild=lambda _: guild)
        assert (await service.import_own_messages(1, None, 100))['created'] == 1
        assert (await service.import_own_messages(1, None, 100))['updated'] == 1
        assert second.list_messages() == []
        first.add_reaction_role(1, 2, 3, 'unicode:✅', '✅', 4, True)
        second_service = DiscordService(settings(tmp_path), second)
        second_service.client = SimpleNamespace(user=SimpleNamespace(id=11), get_guild=lambda _: pytest.fail('Cross-bot reaction lookup'))
        event = SimpleNamespace(guild_id=1, channel_id=2, message_id=3, user_id=20, emoji='✅')
        await second_service.handle_reaction(event, added=True)
        # A mismatched guild/channel event must also be ignored.
        event.channel_id = 99
        await service.handle_reaction(event, added=True)
    asyncio.run(scenario())


def test_api_message_upload_and_rule_workflows_with_mock_discord(tmp_path):
    async def scenario():
        app = create_app(settings(tmp_path))
        async with app.router.lifespan_context(app):
            await request(app, 'POST', '/api/bots', profile('other'))
            db = app.state.database
            for bot_id, user_id in [('default', 10), ('other', 11)]:
                service = DiscordService(settings(tmp_path), db.scoped(bot_id))
                guild = SimpleNamespace(id=1)
                channel = SimpleNamespace(id=2)
                message = SimpleNamespace(id=123456789012345678 + user_id, guild=guild, channel=channel, author=SimpleNamespace(id=user_id), attachments=[], edit=AsyncMock(), delete=AsyncMock(), jump_url='https://discord.com/test')
                channel.send = AsyncMock(return_value=message)
                channel.fetch_message = AsyncMock(return_value=message)
                service._channel = AsyncMock(return_value=channel)
                service.client = SimpleNamespace(user=SimpleNamespace(id=user_id), is_closed=lambda: True)
                service.create_reaction_role = AsyncMock(return_value=('unicode:✅', '✅'))
                app.state.bots.services[bot_id] = service
                prefix = f'/api/bots/{bot_id}'
                multipart = b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="test.txt"\r\nContent-Type: text/plain\r\n\r\nhello\r\n--boundary--\r\n'
                status, body = await request(app, 'POST', prefix + '/uploads', multipart, [(b'content-type', b'multipart/form-data; boundary=boundary')])
                assert status == 200
                upload_id = json.loads(body)['id']
                payload = {'guild_id': '1', 'channel_id': '2', 'content': 'hello', 'embed': {'title': 'news'}, 'upload_ids': [upload_id]}
                status, body = await request(app, 'POST', prefix + '/messages', payload)
                assert status == 201
                record_id = json.loads(body)['id']
                assert isinstance(json.loads(body)['message_id'], str)
                args = channel.send.call_args.kwargs
                assert args['embed'].title == 'news'
                assert not args['allowed_mentions'].everyone
                for file in args['files']:
                    file.close()
                # A foreign attachment is rejected before sending anything.
                other = 'other' if bot_id == 'default' else 'default'
                assert db.scoped(other).get_upload(upload_id) is None
                assert (await request(app, 'PUT', prefix + f'/messages/{record_id}', payload))[0] == 200
                message.edit.assert_awaited_once()
                for file in message.edit.call_args.kwargs['attachments']:
                    file.close()
                rule = {'guild_id': '1', 'channel_id': '2', 'message_id': str(message.id), 'role_id': '4', 'emoji': '✅'}
                assert (await request(app, 'POST', prefix + '/reaction-roles', rule))[0] == 201
                assert (await request(app, 'POST', prefix + '/reaction-roles', rule))[0] == 409
                assert (await request(app, 'DELETE', prefix + f'/messages/{record_id}'))[0] == 200
                message.delete.assert_awaited_once()
                assert db.scoped(bot_id).list_messages() == []
                assert db.scoped(bot_id).list_reaction_roles() == []
                audits = json.loads((await request(app, 'GET', prefix + '/audit'))[1])
                assert all(entry['bot_id'] == bot_id for entry in audits)
    asyncio.run(scenario())


def test_config_no_token_and_legacy_privileged_setting(tmp_path, monkeypatch):
    import app.config as config
    monkeypatch.setattr(config, 'load_dotenv', lambda _: None)
    monkeypatch.delenv('DISCORD_BOT_TOKEN', raising=False)
    monkeypatch.setenv('PANEL_HOST', '127.0.0.1')
    assert config.Settings.load().discord_token == ''
    db = Database(tmp_path / 'legacy-setting.sqlite3')
    db.initialize(allow_privileged_roles=True)
    assert db.profiles()[0]['allow_privileged_roles'] == 1
    db.initialize(allow_privileged_roles=False)
    assert db.profiles()[0]['allow_privileged_roles'] == 1


def test_identity_and_connection_errors_never_expose_token(tmp_path):
    from app.discord_service import ControlClient
    import discord

    async def scenario():
        for error in [discord.LoginFailure('secret-value'), discord.PrivilegedIntentsRequired(None), RuntimeError('secret-value')]:
            service = DiscordService(replace(settings(tmp_path), discord_token='secret-value'), Database(tmp_path / 'unused.sqlite3'))
            service.client = SimpleNamespace(start=AsyncMock(side_effect=error), is_closed=lambda: False, close=AsyncMock())
            await service.run()
            assert service.last_error and 'secret-value' not in service.last_error
            service.client.close.assert_awaited_once()
        service = SimpleNamespace(application_id='123', last_error=None)
        client = SimpleNamespace(service=service, application_id=456, close=AsyncMock())
        await ControlClient.on_ready(client)
        client.close.assert_awaited_once()
        assert 'Application ID' in service.last_error
    asyncio.run(scenario())


def test_basic_auth_remains_required(tmp_path):
    import base64

    async def scenario():
        app = create_app(replace(settings(tmp_path), panel_password='test-password'))
        assert (await request(app, 'GET', '/api/bots'))[0] == 401
        credentials = base64.b64encode(b'admin:test-password')
        status, body = await request(app, 'GET', '/api/bots', headers=[(b'authorization', b'Basic ' + credentials)])
        assert status == 200
        assert b'test-password' not in body
    asyncio.run(scenario())


def test_profile_change_waits_for_inflight_operation_without_blocking_other_bot(tmp_path):
    async def scenario():
        app = create_app(settings(tmp_path))
        async with app.router.lifespan_context(app):
            await request(app, 'POST', '/api/bots', profile('other'))
            started, release = asyncio.Event(), asyncio.Event()
            async def send_message(payload):
                started.set()
                await release.wait()
                return SimpleNamespace(guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=2), id=3, jump_url='test')
            close = AsyncMock()
            app.state.bots.services['other'] = SimpleNamespace(send_message=send_message, close=close)
            publish = asyncio.create_task(request(app, 'POST', '/api/bots/other/messages', {'guild_id': '1', 'channel_id': '2', 'content': 'hello'}))
            await started.wait()
            disable = asyncio.create_task(request(app, 'PUT', '/api/bots/other', profile('other') | {'enabled': False}))
            await asyncio.sleep(0)
            assert not disable.done()
            close.assert_not_awaited()
            assert (await request(app, 'GET', '/api/bots/default/status'))[0] == 200
            release.set()
            assert (await publish)[0] == 201
            assert (await disable)[0] == 200
            close.assert_awaited_once()
            assert app.state.database.scoped('other').list_messages()[0]['message_id'] == '3'
    asyncio.run(scenario())


def test_known_tokens_create_named_profiles_without_discord_connection(tmp_path, monkeypatch):
    monkeypatch.setenv('IRON_CROWN_BOT_TOKEN', 'test-iron-secret')
    monkeypatch.setenv('PERIMETER_BOT_TOKEN', 'test-perimeter-secret')
    app = create_app(settings(tmp_path))
    profiles = app.state.database.profiles()
    assert [(p['id'], p['name']) for p in profiles] == [('iron_crown', 'Iron Crown'), ('perimeter', 'Perimeter')]
    assert not app.state.bots.tasks
    assert 'test-iron-secret' not in json.dumps(profiles)
    assert 'test-perimeter-secret' not in json.dumps(profiles)
    # Restart is idempotent and preserves edits, including disabled profiles.
    app.state.database.save_profile(profiles[0] | {'name': 'Custom name', 'enabled': False})
    app = create_app(settings(tmp_path))
    assert len(app.state.database.profiles()) == 2
    assert app.state.bots.profile('iron_crown')['name'] == 'Custom name'
    assert app.state.bots.profile('iron_crown')['enabled'] == 0


def test_discovery_keeps_legacy_data_and_existing_env_profiles(tmp_path, monkeypatch):
    monkeypatch.setenv('IRON_CROWN_BOT_TOKEN', 'test-secret')
    db = Database(tmp_path / 'discord_bot.sqlite3')
    db.initialize()
    record = db.add_message(1, 2, 3, {'content': 'legacy'})
    db.save_profile(BotProfile(id='custom', name='My bot', token_env='IRON_CROWN_BOT_TOKEN').model_dump())
    app = create_app(settings(tmp_path))
    assert {p['id'] for p in app.state.database.profiles()} == {'default', 'custom'}
    assert app.state.database.get_message(record)['payload']['content'] == 'legacy'


def test_discovery_keeps_explicitly_configured_default(tmp_path, monkeypatch):
    monkeypatch.setenv('PERIMETER_BOT_TOKEN', 'test-secret')
    db = Database(tmp_path / 'discord_bot.sqlite3')
    db.initialize()
    db.audit('profile_saved', {'id': 'default'})
    app = create_app(settings(tmp_path))
    assert {p['id'] for p in app.state.database.profiles()} == {'default', 'perimeter'}
