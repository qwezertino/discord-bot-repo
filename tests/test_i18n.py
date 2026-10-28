import asyncio
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.discord_service import DiscordServiceError
from app.i18n import CATALOG, language, translate
from app.web import create_app
from test_multibot import request, settings


@pytest.mark.parametrize(('header', 'expected'), [
    (None, 'uk'), ('uk-UA, en;q=0.8', 'uk'), ('en-US,en;q=0.9', 'en'),
    ('fr, uk;q=0.8, en;q=0.9', 'en'), ('en;q=0,uk;q=1', 'uk'),
    ('invalid;q=bad', 'uk'),
])
def test_language_negotiation(header, expected):
    assert language(header) == expected


def test_catalog_covers_ui_and_preserves_interpolation_parameters():
    class Keys(HTMLParser):
        def __init__(self):
            super().__init__()
            self.keys = set()
        def handle_starttag(self, tag, attrs):
            for name, value in attrs:
                if name.startswith('data-i18n'):
                    if '{{' not in value:
                        self.keys.add(value)
    parser = Keys()
    parser.feed(Path('app/templates/index.html').read_text())
    parser.feed(Path('app/templates/login.html').read_text())
    js = Path('app/static/app.js').read_text()
    keys = parser.keys | set(re.findall(r'\bt\([\'"]([^\'"\n]+)[\'"]', js))
    assert keys <= CATALOG['messages'].keys()
    for key, versions in CATALOG['messages'].items():
        assert set(versions) == {'en', 'uk'}
        for text in versions.values():
            assert text
            assert set(re.findall(r'\{\w+\}', key)) == set(re.findall(r'\{\w+\}', text))


@pytest.mark.parametrize(('source', 'english', 'ukrainian'), [
    ('Загрузка 123456789012345678 не найдена', 'Upload 123456789012345678 was not found', 'Завантаження 123456789012345678 не знайдено'),
    ('Файл My Русский файл.txt отсутствует на диске', 'File My Русский файл.txt is missing from disk', 'Файл My Русский файл.txt відсутній на диску'),
    ('Файл больше лимита 10 МБ', 'File exceeds the 10 MB limit', 'Файл перевищує ліміт 10 МБ'),
    ('Бот ещё не подключён к Discord: Неверный токен бота', 'The bot is not connected to Discord: Invalid bot token', 'Бот ще не підключений до Discord: Неправильний токен бота'),
])
def test_dynamic_errors_preserve_names_and_ids(source, english, ukrainian):
    assert translate(source, 'en') == english
    assert translate(source, 'uk') == ukrainian


def test_localized_page_errors_and_validation_without_discord(tmp_path):
    async def scenario():
        app = create_app(settings(tmp_path))
        status, body = await request(app, 'GET', '/')
        assert status == 200
        text = body.decode()
        assert '<html lang="uk">' in text
        assert 'id="language-select"' in text
        catalog = re.search(r'<script id="i18n-catalog" type="application/json">(.*?)</script>', text, re.S)
        assert json.loads(catalog[1]) == CATALOG
        for locale, expected in [('uk', 'Профіль бота не знайдено'), ('en', 'Bot profile was not found')]:
            headers = [(b'accept-language', locale.encode())]
            status, body = await request(app, 'GET', '/api/bots/unknown/messages', headers=headers)
            assert status == 404 and json.loads(body)['detail'] == expected
            status, body = await request(app, 'POST', '/api/bots', {'id': 'bad id', 'name': 'private user data', 'token_env': 'test-secret'}, headers=headers)
            assert status == 422
            detail = json.loads(body)['detail']
            assert detail[0]['msg'] == ('This value has an invalid format' if locale == 'en' else 'Це значення має неправильний формат')
            assert b'test-secret' not in body and b'private user data' not in body
            invalid_origin = headers + [(b'origin', b'http://other-host')]
            status, body = await request(app, 'POST', '/api/bots', {}, headers=invalid_origin)
            assert status == 403
            assert json.loads(body)['detail'] == ('Invalid request origin' if locale == 'en' else 'Неприпустиме джерело запиту')
        assert not app.state.bots.tasks
    asyncio.run(scenario())


def test_service_errors_and_status_are_localized_per_request(tmp_path):
    async def scenario():
        app = create_app(settings(tmp_path))
        original_error = 'Неверный токен бота'
        def list_guilds():
            raise DiscordServiceError('Бот ещё не подключён к Discord: ' + original_error)
        app.state.bots.services['default'] = SimpleNamespace(
            status=lambda: {'connected': False, 'last_error': original_error},
            list_guilds=list_guilds,
        )
        async def check(locale):
            headers = [(b'accept-language', locale.encode())]
            status, body = await request(app, 'GET', '/api/bots/default/guilds', headers=headers)
            assert status == 400
            assert json.loads(body)['detail'] == translate('Бот ещё не подключён к Discord: ' + original_error, locale)
            for path in ['/api/bots/default/status', '/api/bots']:
                status, body = await request(app, 'GET', path, headers=headers)
                result = json.loads(body)
                error = result['last_error'] if isinstance(result, dict) else result[0]['status']['last_error']
                assert error == translate(original_error, locale)
        await asyncio.gather(check('en'), check('uk'))
        assert app.state.bots.services['default'].status()['last_error'] == original_error
    asyncio.run(scenario())
