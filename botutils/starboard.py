"""Shared Starboard configuration helpers."""

from __future__ import annotations

import re
from typing import Any

DEFAULT_EMOJI = "⭐"
DEFAULT_THRESHOLD = 3
MAX_BOARDS = 10
MAX_BOARD_NAME = 40

CUSTOM_EMOJI_RE = re.compile(
    r"^<(?P<animated>a?):(?P<name>[A-Za-z0-9_]{2,32}):(?P<id>\d{1,20})>$"
)
BOARD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def custom_emoji_id(value: Any) -> int | None:
    """Return the snowflake from a Discord custom-emoji mention."""
    match = CUSTOM_EMOJI_RE.fullmatch(str(value or "").strip())
    return int(match.group("id")) if match else None


def emoji_key(value: Any) -> str:
    """Return a stable key for a Unicode or Discord custom emoji.

    Discord custom emoji names can change, so their ID is authoritative. Unicode
    presentation selectors are ignored because Discord may add or remove them in
    reaction payloads.
    """
    text = str(value or "").strip()
    if not text:
        raise ValueError("Choose a reaction emoji.")
    if match := CUSTOM_EMOJI_RE.fullmatch(text):
        return f"custom:{match.group('id')}"
    if len(text) > 32 or any(character.isspace() for character in text):
        raise ValueError("Use one Unicode emoji or a Discord custom emoji mention.")
    normalized = text.replace("\ufe0e", "").replace("\ufe0f", "")
    if not normalized or all(ord(character) < 128 for character in normalized):
        raise ValueError(
            "Use the emoji itself, or paste a custom emoji such as <:spark:123456789012345678>."
        )
    return f"unicode:{normalized}"


def public_board(board: dict[str, Any]) -> dict[str, Any]:
    """Return the browser-safe fields of a stored board."""
    return {
        "board_id": str(board["board_id"]),
        "name": str(board["name"]),
        "channel_id": str(board["channel_id"]),
        "emoji": str(board["emoji"]),
        "threshold": int(board["threshold"]),
        "enabled": bool(board.get("enabled", True)),
    }
