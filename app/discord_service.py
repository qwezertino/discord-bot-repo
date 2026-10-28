from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import discord

from .config import Settings
from .database import Database


LOGGER = logging.getLogger(__name__)
SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


class DiscordServiceError(RuntimeError):
    pass


def emoji_key(emoji: discord.PartialEmoji | discord.Emoji | str) -> str:
    parsed = discord.PartialEmoji.from_str(emoji) if isinstance(emoji, str) else emoji
    if parsed.id:
        return f"custom:{parsed.id}"
    return f"unicode:{parsed.name}"


def parse_color(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, int):
        color = value
    else:
        raw = str(value).strip().removeprefix("#").removeprefix("0x")
        try:
            color = int(raw, 16)
        except ValueError as exc:
            raise DiscordServiceError("Цвет embed должен быть в формате #5865F2") from exc
    if not 0 <= color <= 0xFFFFFF:
        raise DiscordServiceError("Цвет embed находится вне допустимого диапазона")
    return color


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def build_embed(data: dict[str, Any] | None, image_filename: str | None) -> discord.Embed | None:
    if not data:
        return None

    title = _optional_text(data.get("title"))
    description = _optional_text(data.get("description"))
    url = _optional_text(data.get("url"))
    color = parse_color(data.get("color"))
    raw_timestamp = data.get("timestamp")
    timestamp: datetime | None = None
    if raw_timestamp is True:
        timestamp = datetime.now(UTC)
    elif isinstance(raw_timestamp, str) and raw_timestamp:
        try:
            timestamp = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise DiscordServiceError("Некорректное время embed") from exc

    if title and len(title) > 256:
        raise DiscordServiceError("Заголовок embed длиннее 256 символов")
    if description and len(description) > 4096:
        raise DiscordServiceError("Описание embed длиннее 4096 символов")

    embed = discord.Embed(
        title=title,
        description=description,
        url=url,
        color=color,
        timestamp=timestamp,
    )

    author = data.get("author") or {}
    author_name = _optional_text(author.get("name"))
    if author_name:
        if len(author_name) > 256:
            raise DiscordServiceError("Имя автора embed длиннее 256 символов")
        author_options: dict[str, Any] = {"name": author_name}
        if author_url := _optional_text(author.get("url")):
            author_options["url"] = author_url
        if author_icon := _optional_text(author.get("icon_url")):
            author_options["icon_url"] = author_icon
        embed.set_author(**author_options)

    footer = data.get("footer") or {}
    footer_text = _optional_text(footer.get("text"))
    if footer_text:
        if len(footer_text) > 2048:
            raise DiscordServiceError("Footer embed длиннее 2048 символов")
        footer_options: dict[str, Any] = {"text": footer_text}
        if footer_icon := _optional_text(footer.get("icon_url")):
            footer_options["icon_url"] = footer_icon
        embed.set_footer(**footer_options)

    thumbnail_url = _optional_text(data.get("thumbnail_url"))
    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)

    image_url = _optional_text(data.get("image_url"))
    if image_filename:
        embed.set_image(url=f"attachment://{image_filename}")
    elif image_url:
        embed.set_image(url=image_url)

    fields = data.get("fields") or []
    if len(fields) > 25:
        raise DiscordServiceError("В embed может быть не больше 25 полей")
    for field in fields:
        name = _optional_text(field.get("name")) or "\u200b"
        value = _optional_text(field.get("value")) or "\u200b"
        if len(name) > 256 or len(value) > 1024:
            raise DiscordServiceError("Поле embed превышает лимит Discord")
        embed.add_field(name=name, value=value, inline=bool(field.get("inline")))

    if len(embed) > 6000:
        raise DiscordServiceError("Общий размер embed превышает 6000 символов")
    if not any((title, description, author_name, footer_text, fields, image_url, image_filename, thumbnail_url)):
        return None
    return embed


class ControlClient(discord.Client):
    def __init__(self, service: "DiscordService") -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.reactions = True
        intents.members = True
        super().__init__(intents=intents)
        self.service = service

    async def on_ready(self) -> None:
        expected = getattr(self.service, "application_id", None)
        if expected and str(self.application_id) != expected:
            self.service.last_error = "Токен принадлежит другому Application ID"
            await self.close()
            return
        self.service.last_error = None
        LOGGER.info("Discord bot connected as %s", self.user)

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self.service.handle_reaction(payload, added=True)

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self.service.handle_reaction(payload, added=False)


class DiscordService:
    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.database = database
        self.client = ControlClient(self)
        self.last_error: str | None = None
        self.application_id: str | None = None

    async def run(self) -> None:
        try:
            await self.client.start(self.settings.discord_token, reconnect=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = {
                "LoginFailure": "Неверный токен бота",
                "PrivilegedIntentsRequired": "Включите Server Members Intent в Developer Portal",
            }.get(type(exc).__name__, "Ошибка подключения к Discord; выполните переподключение")
            LOGGER.error("Discord client stopped (%s)", type(exc).__name__)
            await self.close()

    async def close(self) -> None:
        if not self.client.is_closed():
            await self.client.close()

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.client.is_ready(),
            "user": str(self.client.user) if self.client.user else None,
            "guild_count": len(self.client.guilds),
            "last_error": self.last_error,
        }

    def ensure_ready(self) -> None:
        if not self.client.is_ready():
            detail = f": {self.last_error}" if self.last_error else ""
            raise DiscordServiceError(f"Бот ещё не подключён к Discord{detail}")
        if self.application_id and str(self.client.application_id) != self.application_id:
            raise DiscordServiceError("Токен принадлежит другому Application ID")

    def list_guilds(self) -> list[dict[str, Any]]:
        self.ensure_ready()
        return [
            {"id": str(guild.id), "name": guild.name, "icon": str(guild.icon.url) if guild.icon else None}
            for guild in sorted(self.client.guilds, key=lambda item: item.name.lower())
        ]

    def _guild(self, guild_id: int) -> discord.Guild:
        self.ensure_ready()
        guild = self.client.get_guild(guild_id)
        if not guild:
            raise DiscordServiceError("Бот не состоит на выбранном Discord-сервере")
        return guild

    def list_channels(self, guild_id: int) -> list[dict[str, Any]]:
        guild = self._guild(guild_id)
        result: list[dict[str, Any]] = []
        for channel in sorted(
            guild.text_channels, key=lambda item: (item.category_id or 0, item.position)
        ):
            permissions = channel.permissions_for(guild.me) if guild.me else None
            result.append(
                {
                    "id": str(channel.id),
                    "name": channel.name,
                    "category": channel.category.name if channel.category else None,
                    "can_send": bool(permissions and permissions.send_messages),
                    "can_embed_links": bool(permissions and permissions.embed_links),
                }
            )
        return result

    def list_roles(self, guild_id: int) -> list[dict[str, Any]]:
        guild = self._guild(guild_id)
        return [
            {
                "id": str(role.id),
                "name": role.name,
                "color": str(role.color),
                "managed": role.managed,
                "position": role.position,
            }
            for role in reversed(guild.roles)
            if not role.is_default()
        ]

    async def _channel(self, guild_id: int, channel_id: int) -> discord.TextChannel:
        guild = self._guild(guild_id)
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.client.fetch_channel(channel_id)
            except discord.HTTPException as exc:
                raise DiscordServiceError("Не удалось получить выбранный канал") from exc
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
            raise DiscordServiceError("Выбранный канал не является текстовым каналом сервера")
        return channel

    @staticmethod
    def _discord_filename(index: int, original_name: str) -> str:
        cleaned = SAFE_FILENAME.sub("_", Path(original_name).name).strip("._") or "file"
        return f"{index + 1}_{cleaned}"[:100]

    def _prepare_files(
        self, payload: dict[str, Any]
    ) -> tuple[list[discord.File], str | None]:
        upload_ids = list(dict.fromkeys(payload.get("upload_ids") or []))
        image_upload_id = payload.get("image_upload_id")
        if image_upload_id and image_upload_id not in upload_ids:
            upload_ids.append(image_upload_id)
        if len(upload_ids) > 10:
            raise DiscordServiceError("Discord принимает не больше 10 файлов в сообщении")

        files: list[discord.File] = []
        image_filename: str | None = None
        try:
            for index, upload_id in enumerate(upload_ids):
                upload = self.database.get_upload(str(upload_id))
                if not upload:
                    raise DiscordServiceError(f"Загрузка {upload_id} не найдена")
                path = self.settings.data_dir / "uploads" / upload["stored_name"]
                if not path.is_file():
                    raise DiscordServiceError(f"Файл {upload['original_name']} отсутствует на диске")
                filename = self._discord_filename(index, upload["original_name"])
                files.append(discord.File(path, filename=filename))
                if upload_id == image_upload_id:
                    image_filename = filename
        except Exception:
            for file in files:
                file.close()
            raise
        return files, image_filename

    @contextmanager
    def _message_files(self, payload: dict[str, Any]) -> Iterator[tuple[list[discord.File], str | None]]:
        files, image_filename = self._prepare_files(payload)
        try:
            yield files, image_filename
        finally:
            for file in files:
                file.close()

    @staticmethod
    def _import_embed(message: discord.Message) -> dict[str, Any] | None:
        source = next((item for item in message.embeds if item.type == "rich"), None)
        if source is None:
            return None

        attachment_urls: dict[str, discord.Attachment] = {}
        for attachment in message.attachments:
            attachment_urls[attachment.url] = attachment
            attachment_urls[attachment.proxy_url] = attachment

        image_url = str(source.image.url) if source.image and source.image.url else ""
        if image_url in attachment_urls:
            image_url = f"attachment://{attachment_urls[image_url].filename}"
        thumbnail_url = (
            str(source.thumbnail.url) if source.thumbnail and source.thumbnail.url else ""
        )
        if thumbnail_url in attachment_urls:
            thumbnail_url = f"attachment://{attachment_urls[thumbnail_url].filename}"

        author = source.author
        footer = source.footer
        return {
            "title": source.title or "",
            "description": source.description or "",
            "url": source.url or "",
            "color": f"#{source.colour.value:06x}" if source.colour is not None else "",
            "author": {
                "name": author.name or "" if author else "",
                "url": str(author.url) if author and author.url else "",
                "icon_url": str(author.icon_url) if author and author.icon_url else "",
            },
            "image_url": image_url,
            "thumbnail_url": thumbnail_url,
            "footer": {
                "text": footer.text or "" if footer else "",
                "icon_url": str(footer.icon_url) if footer and footer.icon_url else "",
            },
            "timestamp": source.timestamp.isoformat() if source.timestamp else False,
            "fields": [
                {"name": field.name, "value": field.value, "inline": field.inline}
                for field in source.fields
            ],
        }

    @classmethod
    def payload_from_message(cls, message: discord.Message) -> dict[str, Any]:
        return {
            "guild_id": str(message.guild.id),
            "channel_id": str(message.channel.id),
            "content": message.content or "",
            "embed": cls._import_embed(message),
            "upload_ids": [],
            "image_upload_id": None,
            "preserved_attachment_ids": [str(item.id) for item in message.attachments],
            "preserved_attachment_names": {
                str(item.id): item.filename for item in message.attachments
            },
            "allow_mentions": bool(
                message.mention_everyone or message.role_mentions or message.mentions
            ),
        }

    @staticmethod
    def _allowed_mentions(payload: dict[str, Any]) -> discord.AllowedMentions:
        if payload.get("allow_mentions"):
            return discord.AllowedMentions(everyone=True, users=True, roles=True)
        return discord.AllowedMentions.none()

    async def send_message(self, payload: dict[str, Any]) -> discord.Message:
        channel = await self._channel(int(payload["guild_id"]), int(payload["channel_id"]))
        with self._message_files(payload) as (files, image_filename):
            embed = build_embed(payload.get("embed"), image_filename)
            content = payload.get("content", "").strip() or None
            if not content and not embed and not files:
                raise DiscordServiceError("Сообщение не может быть пустым")
            try:
                return await channel.send(
                    content=content,
                    embed=embed,
                    files=files,
                    allowed_mentions=self._allowed_mentions(payload),
                )
            except discord.Forbidden as exc:
                raise DiscordServiceError("У бота недостаточно прав для отправки сообщения") from exc
            except discord.HTTPException as exc:
                raise DiscordServiceError(f"Discord отклонил сообщение: {exc}") from exc

    async def edit_message(self, record: dict[str, Any], payload: dict[str, Any]) -> None:
        channel = await self._channel(int(record["guild_id"]), int(record["channel_id"]))
        try:
            message = await channel.fetch_message(int(record["message_id"]))
        except discord.NotFound as exc:
            raise DiscordServiceError("Сообщение уже удалено из Discord") from exc
        self._ensure_own_message(message)
        with self._message_files(payload) as (files, image_filename):
            embed = build_embed(payload.get("embed"), image_filename)
            content = payload.get("content", "").strip() or None
            preserved_ids = {
                int(item) for item in payload.get("preserved_attachment_ids") or []
            }
            attachments: list[discord.Attachment | discord.File] = [
                attachment for attachment in message.attachments if attachment.id in preserved_ids
            ]
            attachments.extend(files)
            if not content and not embed and not attachments:
                raise DiscordServiceError("Сообщение не может быть пустым")
            try:
                await message.edit(
                    content=content,
                    embed=embed,
                    attachments=attachments,
                    allowed_mentions=self._allowed_mentions(payload),
                )
            except discord.Forbidden as exc:
                raise DiscordServiceError("Бот может редактировать только собственные сообщения") from exc
            except discord.HTTPException as exc:
                raise DiscordServiceError(f"Discord отклонил изменения: {exc}") from exc

    def _ensure_own_message(self, message: discord.Message) -> None:
        if not self.client.user or message.author.id != self.client.user.id:
            raise DiscordServiceError("Сообщение принадлежит другому боту")

    async def delete_message(self, record: dict[str, Any]) -> None:
        channel = await self._channel(int(record["guild_id"]), int(record["channel_id"]))
        try:
            message = await channel.fetch_message(int(record["message_id"]))
            self._ensure_own_message(message)
            await message.delete()
        except discord.NotFound:
            return
        except discord.Forbidden as exc:
            raise DiscordServiceError("Не удалось удалить сообщение: недостаточно прав") from exc

    async def import_own_messages(
        self,
        guild_id: int,
        channel_id: int | None,
        limit_per_channel: int | None,
    ) -> dict[str, Any]:
        guild = self._guild(guild_id)
        if not self.client.user:
            raise DiscordServiceError("Discord-пользователь бота ещё не определён")

        if channel_id is not None:
            channels = [await self._channel(guild.id, channel_id)]
        else:
            channels = list(guild.text_channels)

        scanned = 0
        imported = 0
        created = 0
        skipped_channels: list[str] = []
        for channel in channels:
            permissions = channel.permissions_for(guild.me) if guild.me else None
            if not permissions or not permissions.view_channel or not permissions.read_message_history:
                skipped_channels.append(channel.name)
                continue
            try:
                async for message in channel.history(limit=limit_per_channel):
                    scanned += 1
                    if message.author.id != self.client.user.id:
                        continue
                    payload = self.payload_from_message(message)
                    _, was_created = self.database.upsert_message(
                        guild.id, channel.id, message.id, payload
                    )
                    imported += 1
                    created += int(was_created)
            except discord.Forbidden:
                skipped_channels.append(channel.name)
            except discord.HTTPException as exc:
                LOGGER.warning("Could not import history from %s: %s", channel, exc)
                skipped_channels.append(channel.name)

        result = {
            "channels": len(channels),
            "scanned": scanned,
            "imported": imported,
            "created": created,
            "updated": imported - created,
            "skipped_channels": skipped_channels,
        }
        self.database.audit("messages_imported", result)
        return result

    def validate_role(self, guild: discord.Guild, role: discord.Role) -> None:
        bot_member = guild.me
        if not bot_member or not bot_member.guild_permissions.manage_roles:
            raise DiscordServiceError("Боту необходимо право Manage Roles")
        if role.is_default() or role.managed:
            raise DiscordServiceError("Этой ролью нельзя управлять вручную")
        if bot_member.top_role <= role:
            raise DiscordServiceError("Роль бота должна находиться выше выдаваемой роли")
        privileged = (
            role.permissions.administrator
            or role.permissions.manage_guild
            or role.permissions.manage_roles
        )
        if privileged and not self.settings.allow_privileged_roles:
            raise DiscordServiceError(
                "Выдача административных ролей отключена настройками безопасности"
            )

    async def create_reaction_role(self, data: dict[str, Any]) -> tuple[str, str]:
        guild = self._guild(int(data["guild_id"]))
        role = guild.get_role(int(data["role_id"]))
        if not role:
            raise DiscordServiceError("Выбранная роль не найдена")
        self.validate_role(guild, role)
        channel = await self._channel(guild.id, int(data["channel_id"]))
        try:
            message = await channel.fetch_message(int(data["message_id"]))
        except discord.NotFound as exc:
            raise DiscordServiceError("Сообщение для реакции не найдено") from exc

        parsed = discord.PartialEmoji.from_str(str(data["emoji"]).strip())
        display = str(parsed)
        try:
            await message.add_reaction(parsed)
        except discord.HTTPException as exc:
            raise DiscordServiceError(
                "Discord не принял эмодзи. Для кастомного эмодзи используй его на сервере."
            ) from exc
        return emoji_key(parsed), display

    async def handle_reaction(
        self, payload: discord.RawReactionActionEvent, *, added: bool
    ) -> None:
        if payload.guild_id is None or (self.client.user and payload.user_id == self.client.user.id):
            return
        mapping = self.database.find_reaction_role(payload.message_id, emoji_key(payload.emoji))
        if not mapping or mapping["guild_id"] != str(payload.guild_id) or mapping["channel_id"] != str(payload.channel_id) or (not added and not mapping["remove_on_unreact"]):
            return
        guild = self.client.get_guild(payload.guild_id)
        if not guild:
            return
        role = guild.get_role(int(mapping["role_id"]))
        if not role:
            LOGGER.warning("Reaction role %s no longer exists", mapping["role_id"])
            return
        try:
            member = payload.member if added else guild.get_member(payload.user_id)
            if member is None:
                member = await guild.fetch_member(payload.user_id)
            if member.bot:
                return
            if added:
                await member.add_roles(role, reason="Reaction role")
                action = "reaction_role_added"
            else:
                await member.remove_roles(role, reason="Reaction role removed")
                action = "reaction_role_removed"
            self.database.audit(
                action,
                {"guild_id": str(guild.id), "user_id": str(member.id), "role_id": str(role.id)},
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            LOGGER.warning("Could not update reaction role: %s", exc)
