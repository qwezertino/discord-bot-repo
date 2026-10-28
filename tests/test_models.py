import pytest
from pydantic import ValidationError

from app.models import MessagePayload, ReactionRoleCreate


def test_snowflakes_remain_strings_for_javascript_clients() -> None:
    snowflake = "123456789012345678"
    payload = MessagePayload(guild_id=snowflake, channel_id=snowflake)
    assert payload.guild_id == snowflake
    assert payload.channel_id == snowflake

    rule = ReactionRoleCreate(
        guild_id=snowflake,
        channel_id=snowflake,
        message_id=snowflake,
        emoji="✅",
        role_id=snowflake,
    )
    assert rule.model_dump()["message_id"] == snowflake


def test_invalid_snowflake_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MessagePayload(guild_id="not-an-id", channel_id="123")


def test_duplicate_masked_url_becomes_bare_for_discord_preview() -> None:
    url = "https://example.com/project"
    payload = MessagePayload(
        guild_id="123",
        channel_id="456",
        content=f"[{url}]({url})",
    )
    assert payload.content == url


def test_duplicate_masked_urls_are_normalized_inside_embed() -> None:
    url = "https://example.com/rules"
    payload = MessagePayload(
        guild_id="123",
        channel_id="456",
        embed={
            "description": f"Правила: [{url}]({url})",
            "fields": [{"name": "Docs", "value": f"[{url}]({url})"}],
        },
    )
    assert payload.embed is not None
    assert payload.embed["description"] == f"Правила: {url}"
    assert payload.embed["fields"][0]["value"] == url


def test_total_attachment_limit_includes_imported_attachments() -> None:
    with pytest.raises(ValidationError):
        MessagePayload(
            guild_id="123",
            channel_id="456",
            upload_ids=["local-upload"],
            preserved_attachment_ids=[str(index) for index in range(1, 11)],
        )
