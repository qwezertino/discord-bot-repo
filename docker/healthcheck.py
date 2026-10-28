"""Read-only local probe with the canonical origin used by the HTTPS proxy."""
import json
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from app.config import Settings

settings = Settings.load()
origin = urlsplit(settings.oauth_redirect_uri)
request = Request(
    'http://127.0.0.1:8765' + settings.panel_url('/api/auth/me'),
    headers={'Host': origin.netloc, 'X-Forwarded-Proto': origin.scheme},
)
with urlopen(request, timeout=4) as response:
    assert response.status == 200 and json.load(response)['mode'] == 'discord'
