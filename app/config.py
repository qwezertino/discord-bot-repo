from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class Settings:
    discord_token: str = field(repr=False)
    panel_host: str
    panel_port: int
    panel_username: str
    panel_password: str | None = field(repr=False)
    data_dir: Path
    max_upload_mb: int
    allow_privileged_roles: bool

    auth_mode: str = "local"
    owner_discord_id: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: str = field(default="", repr=False)
    oauth_redirect_uri: str = "http://127.0.0.1:8765/auth/discord/callback"
    session_hours: int = 12

    def validate_security(self) -> None:
        if self.auth_mode not in {"local", "discord"}:
            raise RuntimeError("PANEL_AUTH_MODE must be local or discord")
        if self.owner_discord_id and (not self.owner_discord_id.isdigit() or int(self.owner_discord_id) <= 0):
            raise RuntimeError("PANEL_OWNER_DISCORD_ID must be a Discord user ID")
        if self.auth_mode == "local":
            if not _is_loopback(self.panel_host) and not self.panel_password:
                raise RuntimeError("PANEL_PASSWORD is required when PANEL_HOST is not a loopback address.")
            return
        if not self.owner_discord_id or not self.oauth_client_id or not self.oauth_client_secret:
            raise RuntimeError("Discord login requires PANEL_OWNER_DISCORD_ID, DISCORD_OAUTH_CLIENT_ID and DISCORD_OAUTH_CLIENT_SECRET")
        if not self.oauth_client_id.isdigit() or int(self.oauth_client_id) <= 0:
            raise RuntimeError("DISCORD_OAUTH_CLIENT_ID must be a Discord application ID")
        uri = urlsplit(self.oauth_redirect_uri)
        try:
            uri.port
        except ValueError:
            raise RuntimeError("Invalid DISCORD_OAUTH_REDIRECT_URI port") from None
        if (not uri.hostname or uri.username or uri.password or uri.query or uri.fragment
                or uri.path != "/auth/discord/callback"
                or not (uri.scheme == "https" or uri.scheme == "http" and _is_loopback(uri.hostname))):
            raise RuntimeError("DISCORD_OAUTH_REDIRECT_URI must be HTTPS (HTTP is allowed on localhost) and end with /auth/discord/callback")
        if not 1 <= self.session_hours <= 168:
            raise RuntimeError("PANEL_SESSION_HOURS must be between 1 and 168")

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv(BASE_DIR / ".env")

        token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
        host = os.getenv("PANEL_HOST", "127.0.0.1").strip()
        password = os.getenv("PANEL_PASSWORD", "").strip() or None
        data_dir = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data"))).expanduser()
        if not data_dir.is_absolute():
            data_dir = (BASE_DIR / data_dir).resolve()

        settings = cls(
            auth_mode=os.getenv("PANEL_AUTH_MODE", "local").strip().lower(),
            owner_discord_id=os.getenv("PANEL_OWNER_DISCORD_ID", "").strip(),
            oauth_client_id=os.getenv("DISCORD_OAUTH_CLIENT_ID", "").strip(),
            oauth_client_secret=os.getenv("DISCORD_OAUTH_CLIENT_SECRET", "").strip(),
            oauth_redirect_uri=os.getenv("DISCORD_OAUTH_REDIRECT_URI", "http://127.0.0.1:8765/auth/discord/callback").strip(),
            session_hours=int(os.getenv("PANEL_SESSION_HOURS", "12")),
            discord_token=token,
            panel_host=host,
            panel_port=int(os.getenv("PANEL_PORT", "8765")),
            panel_username=os.getenv("PANEL_USERNAME", "admin").strip() or "admin",
            panel_password=password,
            data_dir=data_dir,
            max_upload_mb=max(1, int(os.getenv("MAX_UPLOAD_MB", "10"))),
            allow_privileged_roles=_as_bool(
                os.getenv("ALLOW_PRIVILEGED_REACTION_ROLES"), False
            ),
        )
        settings.validate_security()
        return settings
