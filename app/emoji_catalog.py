from __future__ import annotations

from functools import lru_cache

import emoji


@lru_cache(maxsize=1)
def unicode_emoji_catalog() -> list[dict[str, str]]:
    """Return fully-qualified Unicode emoji bundled with the emoji package."""
    catalog: list[dict[str, str]] = []
    for glyph, data in emoji.EMOJI_DATA.items():
        if data.get("status") != 2:
            continue
        raw_name = str(data.get("en", "")).strip(":")
        name = raw_name.replace("_", " ").replace("-", " ")
        catalog.append({"emoji": glyph, "name": name})
    return catalog
