from app.emoji_catalog import unicode_emoji_catalog


def test_catalog_contains_standard_unicode_emoji() -> None:
    catalog = unicode_emoji_catalog()
    by_emoji = {item["emoji"]: item["name"] for item in catalog}

    assert len(catalog) > 3000
    assert "😀" in by_emoji
    assert "❤️" in by_emoji
    assert "🚀" in by_emoji
    assert by_emoji["😀"]
