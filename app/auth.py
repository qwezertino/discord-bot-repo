"""Discord identity login, server-side sessions and per-bot authorization."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import aiohttp
from fastapi import HTTPException, Request

from .config import Settings
from .database import Database, utc_now

SESSION_COOKIE = "panel_session"
FLOW_COOKIE = "panel_oauth"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def csrf_token(session_token: str) -> str:
    return hmac.new(session_token.encode(), b"panel-csrf-v1", hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Identity:
    id: str
    display_name: str
    owner: bool


class OAuthError(Exception):
    """Never include upstream responses, credentials or authorization codes."""


class DiscordOAuth:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def identify(self, code: str, verifier: str) -> dict[str, str]:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.post(
                    "https://discord.com/api/v10/oauth2/token",
                    data={"client_id": self.settings.oauth_client_id, "client_secret": self.settings.oauth_client_secret,
                          "grant_type": "authorization_code", "code": code,
                          "redirect_uri": self.settings.oauth_redirect_uri, "code_verifier": verifier},
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        raise OAuthError()
                    tokens = await response.json()
                if (not isinstance(tokens, dict) or not isinstance(tokens.get("access_token"), str)
                        or tokens.get("token_type", "").lower() != "bearer"
                        or "identify" not in tokens.get("scope", "").split()):
                    raise OAuthError()
                async with session.get(
                    "https://discord.com/api/v10/users/@me",
                    headers={"Authorization": f"Bearer {tokens['access_token']}"},
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        raise OAuthError()
                    user = await response.json()
                user_id = user.get("id") if isinstance(user, dict) else None
                if not isinstance(user_id, str) or not user_id.isdigit() or int(user_id) <= 0 or user.get("bot"):
                    raise OAuthError()
                name = user.get("global_name") or user.get("username") or user_id
                return {"id": user_id, "display_name": str(name)[:128]}
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, AttributeError):
            raise OAuthError() from None


class AuthService:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database
        self.oauth = DiscordOAuth(settings)
        self.flows: dict[str, dict[str, Any]] = {}
        if settings.owner_discord_id:
            database.execute("UPDATE panel_users SET blocked = 0 WHERE id = ?", (settings.owner_discord_id,))

    @property
    def enabled(self) -> bool:
        return self.settings.auth_mode == "discord"

    @property
    def secure_cookie(self) -> bool:
        return urlsplit(self.settings.oauth_redirect_uri).scheme == "https"

    @property
    def session_cookie(self) -> str:
        return "__Host-" + SESSION_COOKIE if self.secure_cookie else SESSION_COOKIE

    @property
    def flow_cookie(self) -> str:
        return "__Host-" + FLOW_COOKIE if self.secure_cookie else FLOW_COOKIE

    def resolve(self, request: Request) -> Identity | None:
        if not self.enabled:
            return Identity("local", "Local administrator", True)
        token = request.cookies.get(self.session_cookie, "")
        if not token or len(token) > 128:
            return None
        user = self.database.fetch_one(
            """SELECT u.* FROM panel_sessions s JOIN panel_users u ON u.id = s.user_id
               WHERE s.token_hash = ? AND s.expires_at > ? AND u.blocked = 0""",
            (digest(token), int(time.time())),
        )
        if not user:
            return None
        return Identity(user["id"], user["display_name"], user["id"] == self.settings.owner_discord_id)

    async def owner(self, request: Request) -> Identity:
        identity = request.state.identity
        if not identity or not identity.owner:
            raise HTTPException(403, "Owner access is required")
        return identity

    def access_role(self, identity: Identity, bot_id: str) -> str | None:
        if identity.owner:
            return "owner"
        grant = self.database.fetch_one("SELECT role FROM bot_access WHERE user_id = ? AND bot_id = ?", (identity.id, bot_id))
        return grant["role"] if grant else None

    def bot_access(self, request: Request, bot_id: str) -> str:
        identity = request.state.identity
        if not identity:
            raise HTTPException(401, "Sign in to continue")
        role = self.access_role(identity, bot_id)
        if role is None:
            # Do not reveal the existence of inaccessible profiles.
            raise HTTPException(404, "Bot profile was not found")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and role not in {"operator", "owner"}:
            raise HTTPException(403, "This bot is available in read-only mode")
        return role

    def begin_login(self, locale: str) -> tuple[str, str]:
        now = time.time()
        self.flows = {key: flow for key, flow in self.flows.items() if flow["expires"] > now}
        if len(self.flows) >= 256:
            raise HTTPException(429, "Too many sign-in attempts. Try again shortly.")
        state, binding, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        self.flows[digest(state)] = {"binding": digest(binding), "verifier": verifier, "locale": locale, "expires": now + 600}
        query = urlencode({"client_id": self.settings.oauth_client_id, "redirect_uri": self.settings.oauth_redirect_uri,
                           "response_type": "code", "scope": "identify", "state": state,
                           "code_challenge": challenge, "code_challenge_method": "S256"})
        return "https://discord.com/oauth2/authorize?" + query, binding

    def consume_flow(self, state: str, binding: str) -> dict | None:
        if not state or not binding or len(state) > 128 or len(binding) > 128:
            return None
        key = digest(state)
        flow = self.flows.get(key)
        if not flow or flow["expires"] <= time.time() or not hmac.compare_digest(flow["binding"], digest(binding)):
            return None
        return self.flows.pop(key)

    def create_session(self, user: dict[str, str], previous_token: str = "") -> str:
        now = int(time.time())
        token = secrets.token_urlsafe(32)
        with self.database._lock, self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO panel_users (id, display_name, created_at, last_login_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name, last_login_at=excluded.last_login_at""",
                (user["id"], user["display_name"], utc_now(), utc_now()),
            )
            blocked = connection.execute("SELECT blocked FROM panel_users WHERE id = ?", (user["id"],)).fetchone()[0]
            if blocked:
                raise HTTPException(403, "Your access to the panel has been blocked")
            connection.execute("DELETE FROM panel_sessions WHERE expires_at <= ? OR token_hash = ?", (now, digest(previous_token)))
            connection.execute("INSERT INTO panel_sessions VALUES (?, ?, ?)", (digest(token), user["id"], now + self.settings.session_hours * 3600))
            connection.commit()
        return token

    def logout(self, token: str) -> None:
        self.database.execute("DELETE FROM panel_sessions WHERE token_hash = ?", (digest(token),))

    def users(self) -> list[dict]:
        users = self.database.fetch_all("SELECT * FROM panel_users ORDER BY created_at, id")
        grants = self.database.fetch_all("SELECT * FROM bot_access ORDER BY bot_id")
        for user in users:
            user["owner"] = user["id"] == self.settings.owner_discord_id
            user["grants"] = [{"bot_id": row["bot_id"], "role": row["role"]} for row in grants if row["user_id"] == user["id"]]
        return users

    def set_grant(self, user_id: str, bot_id: str, role: str | None) -> None:
        if user_id == self.settings.owner_discord_id:
            raise HTTPException(400, "Owner access cannot be changed here")
        if not self.database.fetch_one("SELECT id FROM bot_profiles WHERE id = ?", (bot_id,)):
            raise HTTPException(404, "Bot profile was not found")
        with self.database._lock, self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT OR IGNORE INTO panel_users (id, display_name, created_at) VALUES (?, ?, ?)", (user_id, user_id, utc_now()))
            if role is None:
                connection.execute("DELETE FROM bot_access WHERE user_id = ? AND bot_id = ?", (user_id, bot_id))
            else:
                connection.execute("""INSERT INTO bot_access VALUES (?, ?, ?) ON CONFLICT(user_id, bot_id) DO UPDATE SET role=excluded.role""", (user_id, bot_id, role))
            connection.commit()

    def set_blocked(self, user_id: str, blocked: bool) -> None:
        if user_id == self.settings.owner_discord_id:
            raise HTTPException(400, "Owner access cannot be changed here")
        with self.database._lock, self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT OR IGNORE INTO panel_users (id, display_name, created_at) VALUES (?, ?, ?)", (user_id, user_id, utc_now()))
            connection.execute("UPDATE panel_users SET blocked = ? WHERE id = ?", (int(blocked), user_id))
            if blocked:
                connection.execute("DELETE FROM panel_sessions WHERE user_id = ?", (user_id,))
            connection.commit()
