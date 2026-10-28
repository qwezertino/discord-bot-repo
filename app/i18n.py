"""Shared UI/error catalog; persisted data and Discord operations stay language-neutral."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

CATALOG = json.loads((Path(__file__).parent / "static" / "translations.json").read_text(encoding="utf-8"))
_DYNAMIC = []
for source, key in CATALOG["sources"].items():
    if "{" in source:
        parts = re.split(r"(\{\w+\})", source)
        pattern = "".join(
            f"(?P<{part[1:-1]}>.*?)" if re.fullmatch(r"\{\w+\}", part) else re.escape(part)
            for part in parts
        )
        _DYNAMIC.append((re.compile(pattern, re.DOTALL), key))


def language(header: str | None) -> str:
    preferences = []
    for item in (header or "").split(","):
        tag, _, quality = item.strip().partition(";")
        try:
            priority = float(quality.strip().removeprefix("q=")) if quality else 1.0
        except ValueError:
            continue
        preferences.append((priority, tag.lower().split("-")[0]))
    for priority, tag in sorted(preferences, key=lambda item: -item[0]):
        if priority > 0 and tag in {"uk", "en"}:
            return tag
    return "uk"


def translate(message: str, locale: str, **values: Any) -> str:
    key = CATALOG["sources"].get(message, message)
    if key not in CATALOG["messages"]:
        for pattern, candidate in _DYNAMIC:
            match = pattern.fullmatch(message)
            if match:
                key = candidate
                values = match.groupdict()
                # Service readiness errors can contain a second known error.
                if values.get("detail"):
                    detail = values["detail"]
                    prefix = ": " if detail.startswith(": ") else ""
                    values["detail"] = prefix + translate(detail[len(prefix):], locale)
                break
        else:
            return message
    template = CATALOG["messages"][key][locale if locale in {"uk", "en"} else "uk"]
    return re.sub(r"\{(\w+)\}", lambda match: str(values.get(match[1], match[0])), template)


def localized_status(status: dict, locale: str) -> dict:
    return {**status, "last_error": translate(status["last_error"], locale) if status.get("last_error") else None}


def validation_detail(error: dict, locale: str) -> dict:
    message = error["msg"].removeprefix("Value error, ")
    translated = translate(message, locale)
    if translated == message and message not in CATALOG["messages"]:
        key = {
            "missing": "This field is required",
            "string_too_short": "This value is too short",
            "string_too_long": "This value is too long",
            "too_long": "This value is too long",
            "string_pattern_mismatch": "This value has an invalid format",
            "greater_than_equal": "This value is below the allowed minimum",
            "less_than_equal": "This value exceeds the allowed maximum",
            "string_type": "A string is required",
            "int_parsing": "An integer is required",
            "int_type": "An integer is required",
            "bool_parsing": "A boolean is required",
            "json_invalid": "Invalid JSON",
        }.get(error["type"], "Invalid input")
        translated = translate(key, locale)
    # Validation inputs/context can contain submitted values; don't echo them.
    return {"loc": error["loc"], "type": error["type"], "msg": translated}
