from __future__ import annotations

import copy
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


Snowflake = str
SAME_URL_MASKED_LINK = re.compile(r"\[(https?://[^\]\s]+)\]\(\1\)")


def normalize_duplicate_masked_urls(value: str) -> str:
    return SAME_URL_MASKED_LINK.sub(r"\1", value)


def _snowflake(value: str | int) -> str:
    cleaned = str(value).strip()
    if not cleaned.isdigit() or int(cleaned) <= 0:
        raise ValueError("Discord ID must contain only digits")
    return cleaned


class ReactionRoleCreate(BaseModel):
    guild_id: Snowflake
    channel_id: Snowflake
    message_id: Snowflake
    emoji: str = Field(min_length=1, max_length=128)
    role_id: Snowflake
    remove_on_unreact: bool = True

    @field_validator("emoji")
    @classmethod
    def clean_emoji(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Emoji is required")
        return value

    @field_validator("guild_id", "channel_id", "message_id", "role_id", mode="before")
    @classmethod
    def validate_snowflake(cls, value: str | int) -> str:
        return _snowflake(value)


class MessagePayload(BaseModel):
    guild_id: Snowflake
    channel_id: Snowflake
    content: str = ""
    embed: dict[str, Any] | None = None
    upload_ids: list[str] = Field(default_factory=list, max_length=10)
    image_upload_id: str | None = None
    preserved_attachment_ids: list[Snowflake] = Field(default_factory=list, max_length=10)
    preserved_attachment_names: dict[Snowflake, str] = Field(default_factory=dict)
    allow_mentions: bool = False

    @field_validator("guild_id", "channel_id", mode="before")
    @classmethod
    def validate_snowflake(cls, value: str | int) -> str:
        return _snowflake(value)

    @field_validator("content")
    @classmethod
    def content_limit(cls, value: str) -> str:
        # Discord does not generate a rich link card for a URL hidden behind
        # masked-link Markdown. The toolbar used to produce [URL](URL), which
        # has no visual benefit, so preserve it as a bare URL instead.
        value = normalize_duplicate_masked_urls(value)
        if len(value) > 2000:
            raise ValueError("Message content is limited to 2000 characters")
        return value

    @field_validator("embed")
    @classmethod
    def normalize_embed_links(
        cls, value: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        normalized = copy.deepcopy(value)
        if isinstance(normalized.get("description"), str):
            normalized["description"] = normalize_duplicate_masked_urls(
                normalized["description"]
            )
        for field in normalized.get("fields") or []:
            if isinstance(field, dict) and isinstance(field.get("value"), str):
                field["value"] = normalize_duplicate_masked_urls(field["value"])
        return normalized

    @field_validator("upload_ids")
    @classmethod
    def unique_uploads(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))

    @field_validator("preserved_attachment_ids", mode="before")
    @classmethod
    def validate_attachment_ids(cls, value: list[str | int]) -> list[str]:
        return list(dict.fromkeys(_snowflake(item) for item in value))

    @field_validator("preserved_attachment_names", mode="before")
    @classmethod
    def validate_attachment_names(cls, value: dict[str | int, Any]) -> dict[str, str]:
        return {
            _snowflake(attachment_id): str(name)[:255]
            for attachment_id, name in value.items()
        }

    @model_validator(mode="after")
    def total_attachment_limit(self) -> "MessagePayload":
        local_uploads = set(self.upload_ids)
        if self.image_upload_id:
            local_uploads.add(self.image_upload_id)
        if len(local_uploads) + len(self.preserved_attachment_ids) > 10:
            raise ValueError("Discord allows at most 10 attachments per message")
        return self


class MessageImportRequest(BaseModel):
    guild_id: Snowflake
    channel_id: Snowflake | None = None
    limit_per_channel: int | None = Field(default=1000, ge=1, le=10000)

    @field_validator("guild_id", mode="before")
    @classmethod
    def validate_guild_id(cls, value: str | int) -> str:
        return _snowflake(value)

    @field_validator("channel_id", mode="before")
    @classmethod
    def validate_channel_id(cls, value: str | int | None) -> str | None:
        return _snowflake(value) if value is not None else None


class BotProfile(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=100)
    application_id: Snowflake | None = None
    token_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    enabled: bool = True
    allow_privileged_roles: bool = False

    @field_validator("application_id", mode="before")
    @classmethod
    def application_snowflake(cls, value: str | int | None) -> str | None:
        return _snowflake(value) if value not in (None, "") else None
