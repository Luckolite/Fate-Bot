"""Validation and authorization helpers for dashboard writes."""

from __future__ import annotations

from typing import Any

from .settings_store import (
    GENERAL_DEFAULTS,
    MESSAGE_DEFAULTS,
    PREFIX_DEFAULTS,
    RANKING_DEFAULTS,
    VERIFICATION_DEFAULTS,
    SUPPORTED_LANGUAGES,
)

ADMINISTRATOR = 1 << 3
MANAGE_GUILD = 1 << 5
MAX_PURGE_LIMIT = 3000


class ValidationError(ValueError):
    pass


def can_manage_guild(guild: dict[str, Any]) -> bool:
    try:
        permissions = int(guild.get("permissions", 0))
    except (TypeError, ValueError):
        return False
    return bool(guild.get("owner") or permissions & (ADMINISTRATOR | MANAGE_GUILD))


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValidationError(f"{name} must be a whole number.")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ValidationError(f"{name} must be a whole number.") from error
    if not minimum <= result <= maximum:
        raise ValidationError(f"{name} must be between {minimum} and {maximum}.")
    return result


def _boolean(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise ValidationError(f"{name} must be enabled or disabled.")
    return value


def _channel(value: Any, name: str, *, allow_current: bool = False):
    if value in (None, "", False):
        return False if allow_current else None
    if allow_current and value is True:
        return True
    text = str(value)
    if not text.isdigit() or len(text) > 20:
        raise ValidationError(f"{name} must be a valid Discord channel.")
    return int(text)


def _role(value: Any, name: str):
    if value in (None, "", False):
        return None
    text = str(value)
    if not text.isdigit() or len(text) > 20:
        raise ValidationError(f"{name} must be a valid Discord role.")
    return int(text)


def validate_settings(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValidationError("Settings must be a JSON object.")

    raw_general = payload.get("general", {})
    raw_prefix = payload.get("prefix", {})
    raw_ranking = payload.get("ranking", {})
    raw_messages = payload.get("messages", {})
    raw_verification = payload.get("verification", {})
    if not all(isinstance(value, dict) for value in (
        raw_general, raw_prefix, raw_ranking, raw_messages, raw_verification
    )):
        raise ValidationError("Each settings category must be an object.")

    prefix = str(raw_prefix.get("value", PREFIX_DEFAULTS["prefix"])).strip()
    if not 1 <= len(prefix) <= 5 or any(character.isspace() for character in prefix):
        raise ValidationError("The prefix must be 1–5 characters with no spaces.")

    disabled = raw_general.get("disabled_channels", GENERAL_DEFAULTS["disabled_channels"])
    if not isinstance(disabled, list) or len(disabled) > 25:
        raise ValidationError("Disabled channels must contain at most 25 channels.")
    disabled_channels = []
    for channel_id in disabled:
        parsed = _channel(channel_id, "Disabled channel")
        if parsed and parsed not in disabled_channels:
            disabled_channels.append(parsed)

    language = str(
        raw_general.get("language", GENERAL_DEFAULTS["language"])
    ).strip()
    if language not in SUPPORTED_LANGUAGES:
        raise ValidationError("That language is not supported.")

    minimum_xp = _integer(raw_ranking.get("min_xp_per_msg", 1), "Minimum XP", 0, 100)
    maximum_xp = _integer(raw_ranking.get("max_xp_per_msg", 1), "Maximum XP", 0, 100)
    if minimum_xp > maximum_xp:
        raise ValidationError("Minimum XP cannot be greater than maximum XP.")

    xp_disabled = raw_ranking.get(
        "disabled_channels", RANKING_DEFAULTS["disabled_channels"]
    )
    if not isinstance(xp_disabled, list) or len(xp_disabled) > 25:
        raise ValidationError("XP-disabled channels must contain at most 25 channels.")
    xp_disabled_channels = []
    for channel_id in xp_disabled:
        parsed = _channel(channel_id, "XP-disabled channel")
        if parsed and parsed not in xp_disabled_channels:
            xp_disabled_channels.append(parsed)

    verification_enabled = bool(
        raw_verification.get("enabled", VERIFICATION_DEFAULTS["enabled"])
    )
    verification_channel = _channel(
        raw_verification.get("channel_id"), "Verification channel"
    )
    verified_role = _role(
        raw_verification.get("verified_role_id"), "Verified role"
    )
    restricted_role = _role(
        raw_verification.get("temp_role_id"), "Restricted role"
    )
    if verification_enabled and not verification_channel:
        raise ValidationError("Choose a verification channel before enabling verification.")
    if verification_enabled and not verified_role:
        raise ValidationError("Choose a verified role before enabling verification.")
    if verified_role and restricted_role == verified_role:
        raise ValidationError("The restricted and verified roles must be different.")

    return {
        "general": {
            "purge_limit": _integer(
                raw_general.get("purge_limit", GENERAL_DEFAULTS["purge_limit"]),
                "Purge limit", 1, MAX_PURGE_LIMIT,
            ),
            "purge_confirmation": _boolean(
                raw_general.get(
                    "purge_confirmation", GENERAL_DEFAULTS["purge_confirmation"]
                ),
                "Purge confirmation",
            ),
            "warns_channel": _channel(raw_general.get("warns_channel"), "Warning channel"),
            "disabled_channels": disabled_channels,
            "language": language,
        },
        "prefix": {
            "value": prefix,
            "allow_personal": bool(raw_prefix.get("allow_personal", True)),
        },
        "ranking": {
            "min_xp_per_msg": minimum_xp,
            "max_xp_per_msg": maximum_xp,
            "first_lvl_xp_req": _integer(
                raw_ranking.get("first_lvl_xp_req", RANKING_DEFAULTS["first_lvl_xp_req"]),
                "First-level XP", 100, 2500,
            ),
            "timeframe": _integer(
                raw_ranking.get("timeframe", RANKING_DEFAULTS["timeframe"]),
                "XP timeframe", 1, 3600,
            ),
            "msgs_within_timeframe": _integer(
                raw_ranking.get("msgs_within_timeframe", RANKING_DEFAULTS["msgs_within_timeframe"]),
                "Messages per timeframe", 1, 3600,
            ),
            "disabled_channels": xp_disabled_channels,
        },
        "messages": {
            "level_up_messages": _channel(
                raw_messages.get("level_up_messages", MESSAGE_DEFAULTS["level_up_messages"]),
                "Level-up channel", allow_current=True,
            ),
            "redirect_mod_commands": _channel(
                raw_messages.get("redirect_mod_commands", MESSAGE_DEFAULTS["redirect_mod_commands"]),
                "Moderation response channel", allow_current=False,
            ) or False,
        },
        "verification": {
            "enabled": verification_enabled,
            "channel_id": verification_channel,
            "verified_role_id": verified_role,
            "temp_role_id": restricted_role,
            "delete_after": bool(
                raw_verification.get(
                    "delete_after", VERIFICATION_DEFAULTS["delete_after"]
                )
            ),
            "log_channel": _channel(
                raw_verification.get("log_channel"), "Verification log channel"
            ),
            "kick_on_fail": bool(
                raw_verification.get(
                    "kick_on_fail", VERIFICATION_DEFAULTS["kick_on_fail"]
                )
            ),
            "auto_start": bool(
                raw_verification.get(
                    "auto_start", VERIFICATION_DEFAULTS["auto_start"]
                )
            ),
            "time_limit": _integer(
                raw_verification.get(
                    "time_limit", VERIFICATION_DEFAULTS["time_limit"]
                ),
                "Verification time limit", 30, 600,
            ),
            "panel_active": bool(raw_verification.get("panel_active", False)),
        },
    }
