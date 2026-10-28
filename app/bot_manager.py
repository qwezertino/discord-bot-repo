"""Owns Discord clients. Future game adapters receive a scoped service explicitly."""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from .config import Settings
from .database import Database
from .discord_service import DiscordService

LOGGER = logging.getLogger(__name__)


class BotManager:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        service_factory: Callable[[Settings, Database], DiscordService] = DiscordService,
    ) -> None:
        self.settings = settings
        self.database = database
        self.service_factory = service_factory
        self.services: dict[str, DiscordService] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.running = False

    def bootstrap_profiles(self) -> None:
        configured = [
            (bot_id, name, token_env)
            for bot_id, name, token_env in (
                ("iron_crown", "Iron Crown", "IRON_CROWN_BOT_TOKEN"),
                ("perimeter", "Perimeter", "PERIMETER_BOT_TOKEN"),
            )
            if os.getenv(token_env, "").strip()
        ]
        self.database.bootstrap_profiles(configured, bool(self.settings.discord_token))

    def lock(self, bot_id: str) -> asyncio.Lock:
        return self.locks.setdefault(bot_id, asyncio.Lock())

    def profile(self, bot_id: str) -> dict[str, Any] | None:
        return self.database.fetch_one("SELECT * FROM bot_profiles WHERE id = ?", (bot_id,))

    def token(self, profile: dict[str, Any]) -> str:
        if profile["token_env"] == "DISCORD_BOT_TOKEN":
            return self.settings.discord_token
        return os.getenv(profile["token_env"], "").strip()

    def status(self, profile: dict[str, Any]) -> dict[str, Any]:
        service = self.services.get(profile["id"])
        status = service.status() if service else {
            "connected": False, "user": None, "guild_count": 0, "last_error": None,
        }
        if not profile["enabled"]:
            state = "disabled"
        elif not self.token(profile):
            state = "missing_token"
        elif status["last_error"]:
            state = "error"
        else:
            state = "connected" if status["connected"] else "connecting"
        return {**status, "state": state}

    async def start(self) -> None:
        self.running = True
        for profile in self.database.profiles():
            await self.restart(profile["id"])

    async def stop_bot(self, bot_id: str) -> None:
        service = self.services.pop(bot_id, None)
        task = self.tasks.pop(bot_id, None)
        try:
            if service:
                await asyncio.wait_for(service.close(), timeout=10)
        except Exception as exc:
            # Do not include exception messages: a transport error may contain secrets.
            LOGGER.error("Could not close bot %s (%s)", bot_id, type(exc).__name__)
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def restart(self, bot_id: str) -> None:
        # The caller holds lock(bot_id) when serving a request. This prevents
        # replacing a client in the middle of a publish/import operation.
        await self.stop_bot(bot_id)
        profile = self.profile(bot_id)
        if not self.running or not profile or not profile["enabled"] or not self.token(profile):
            return
        service = self.service_factory(
            replace(
                self.settings,
                discord_token=self.token(profile),
                allow_privileged_roles=bool(profile["allow_privileged_roles"]),
            ),
            self.database.scoped(bot_id),
        )
        service.application_id = profile["application_id"]
        self.services[bot_id] = service
        self.tasks[bot_id] = asyncio.create_task(service.run(), name=f"discord-{bot_id}")

    async def close(self) -> None:
        self.running = False
        await asyncio.gather(*(self.stop_bot(bot_id) for bot_id in list(self.services)))
