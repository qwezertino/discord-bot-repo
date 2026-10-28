from types import SimpleNamespace

import discord
import pytest

from app.discord_service import (
    ControlClient,
    DiscordService,
    DiscordServiceError,
    build_embed,
    emoji_key,
    parse_color,
)


def test_embed_builder() -> None:
    embed = build_embed(
        {
            "title": "News",
            "description": "Update",
            "color": "#d5ff38",
            "fields": [{"name": "Status", "value": "Online", "inline": True}],
        },
        "1_banner.png",
    )
    assert embed is not None
    assert embed.title == "News"
    assert embed.image.url == "attachment://1_banner.png"
    assert len(embed.fields) == 1


def test_color_validation() -> None:
    assert parse_color("#5865F2") == 0x5865F2
    with pytest.raises(DiscordServiceError):
        parse_color("not-a-color")


def test_emoji_keys() -> None:
    assert emoji_key("✅") == "unicode:✅"
    assert emoji_key("<:perimeter:123456789012345678>") == "custom:123456789012345678"


def test_client_requests_member_events_but_not_message_content() -> None:
    client = ControlClient(object())  # type: ignore[arg-type]
    assert client.intents.guilds is True
    assert client.intents.reactions is True
    assert client.intents.members is True
    assert client.intents.message_content is False


def test_payload_from_plain_imported_message() -> None:
    message = SimpleNamespace(
        guild=SimpleNamespace(id=123),
        channel=SimpleNamespace(id=456),
        content="Imported",
        embeds=[],
        attachments=[],
        mention_everyone=False,
        role_mentions=[],
        mentions=[],
    )
    result = DiscordService.payload_from_message(message)  # type: ignore[arg-type]
    assert result["content"] == "Imported"
    assert result["guild_id"] == "123"
    assert result["preserved_attachment_ids"] == []


def test_payload_imports_first_rich_embed() -> None:
    embed = discord.Embed(title="Rules", description="Read them", color=0)
    embed.add_field(name="Link", value="https://example.com", inline=False)
    message = SimpleNamespace(
        guild=SimpleNamespace(id=123),
        channel=SimpleNamespace(id=456),
        content="",
        embeds=[embed],
        attachments=[],
        mention_everyone=False,
        role_mentions=[],
        mentions=[],
    )
    result = DiscordService.payload_from_message(message)  # type: ignore[arg-type]
    assert result["embed"]["title"] == "Rules"
    assert result["embed"]["color"] == "#000000"
    assert result["embed"]["fields"][0]["value"] == "https://example.com"
