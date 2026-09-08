"""Pure configuration helpers for Fate's anti-raid system.

The Discord cog owns live guild actions.  This module owns the persistent
schema so the bot, dashboard, and tests all agree on defaults and migrations.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from math import isfinite
from typing import Any, Mapping, MutableMapping

SCHEMA_VERSION = 2
VALID_MODES = ("observe", "enforce")
VALID_JOIN_ACTIONS = ("kick", "temporary_ban")
VALID_STAFF_ACTIONS = ("strip_roles", "kick", "ban")
PROTECTION_ORDER = (
    "join_burst",
    "suspicious_accounts",
    "destructive_actions",
)

PROTECTION_INFO = {
    "join_burst": {
        "label": "Join burst shield",
        "emoji": "🌊",
        "summary": "Locks down sudden member floods using a rolling window.",
    },
    "suspicious_accounts": {
        "label": "New-account clusters",
        "emoji": "🛰️",
        "summary": "Escalates when several young or blank-profile accounts arrive together.",
    },
    "destructive_actions": {
        "label": "Staff compromise guard",
        "emoji": "🔐",
        "summary": "Correlates rapid kicks, bans, and destructive server changes by actor.",
    },
}

PROTECTION_DEFAULTS = {
    "join_burst": {
        "enabled": True,
        "threshold": 10,
        "window": 20,
    },
    "suspicious_accounts": {
        "enabled": True,
        "max_account_age_hours": 24,
        "threshold": 4,
        "window": 30,
        "require_no_avatar": False,
    },
    "destructive_actions": {
        "enabled": True,
        "threshold": 3,
        "window": 15,
        "watch_bans": True,
        "watch_kicks": True,
        "watch_channels": True,
        "watch_roles": True,
        "watch_webhooks": True,
    },
}


def recommended_config(*, enabled: bool = True) -> dict[str, Any]:
    """Return a complete independent AntiRaid configuration."""
    return {
        "schema_version": SCHEMA_VERSION,
        "enabled": enabled,
        "mode": "enforce",
        "protections": deepcopy(PROTECTION_DEFAULTS),
        "response": {
            "join_action": "temporary_ban",
            "staff_action": "strip_roles",
            "lock_minutes": 10,
        },
        "alert_channel_id": None,
        "trusted_roles": [],
        "trusted_members": [],
        # Runtime containment state is persisted so a reload cannot silently
        # release a lockdown or forget members awaiting an automatic unban.
        "lockdown_until": 0.0,
        "temporary_bans": [],
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(maximum, max(minimum, parsed))


def _positive_id(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _id_list(value: Any, *, maximum: int = 500) -> list[int]:
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[int] = []
    for item in value:
        parsed = _positive_id(item)
        if parsed is not None and parsed not in result:
            result.append(parsed)
        if len(result) >= maximum:
            break
    return result


def _choice(value: Any, choices: tuple[str, ...], default: str) -> str:
    parsed = str(value or "").lower()
    return parsed if parsed in choices else default


def normalize_config(config: MutableMapping[str, Any]) -> bool:
    """Migrate and sanitize an AntiRaid configuration in place.

    Legacy ``mass_join``, ``mass_ban``, and ``self_bot`` toggles are mapped to
    their nearest safer v2 protections.  The old self-bot heuristic never had
    an active message listener; it becomes clustered suspicious-account
    detection rather than an individual-message ban rule.
    """
    before = deepcopy(dict(config))
    defaults = recommended_config()
    protections = _mapping(config.get("protections"))
    response = _mapping(config.get("response"))
    is_legacy = config.get("schema_version") != SCHEMA_VERSION

    def legacy_enabled(old_key: str, new_key: str) -> bool:
        current = _mapping(protections.get(new_key))
        if "enabled" in current:
            return bool(current["enabled"])
        if is_legacy and old_key in config:
            return bool(config.get(old_key))
        return bool(PROTECTION_DEFAULTS[new_key]["enabled"])

    join_raw = _mapping(protections.get("join_burst"))
    risk_raw = _mapping(protections.get("suspicious_accounts"))
    destructive_raw = _mapping(protections.get("destructive_actions"))

    normalized = recommended_config(
        enabled=bool(config.get("enabled", bool(config)))
    )
    normalized["mode"] = _choice(
        config.get("mode"), VALID_MODES, defaults["mode"]
    )
    normalized["protections"] = {
        "join_burst": {
            "enabled": legacy_enabled("mass_join", "join_burst"),
            "threshold": _bounded_int(join_raw.get("threshold"), 10, 3, 100),
            "window": _bounded_int(join_raw.get("window"), 20, 3, 300),
        },
        "suspicious_accounts": {
            "enabled": legacy_enabled("self_bot", "suspicious_accounts"),
            "max_account_age_hours": _bounded_int(
                risk_raw.get("max_account_age_hours"), 24, 1, 8_760
            ),
            "threshold": _bounded_int(risk_raw.get("threshold"), 4, 2, 50),
            "window": _bounded_int(risk_raw.get("window"), 30, 3, 600),
            "require_no_avatar": bool(risk_raw.get("require_no_avatar", False)),
        },
        "destructive_actions": {
            "enabled": legacy_enabled("mass_ban", "destructive_actions"),
            "threshold": _bounded_int(
                destructive_raw.get("threshold"), 3, 2, 25
            ),
            "window": _bounded_int(destructive_raw.get("window"), 15, 3, 300),
            **{
                key: bool(destructive_raw.get(key, True))
                for key in (
                    "watch_bans",
                    "watch_kicks",
                    "watch_channels",
                    "watch_roles",
                    "watch_webhooks",
                )
            },
        },
    }
    normalized["response"] = {
        "join_action": _choice(
            response.get("join_action"),
            VALID_JOIN_ACTIONS,
            defaults["response"]["join_action"],
        ),
        "staff_action": _choice(
            response.get("staff_action"),
            VALID_STAFF_ACTIONS,
            defaults["response"]["staff_action"],
        ),
        "lock_minutes": _bounded_int(
            response.get("lock_minutes"), 10, 1, 1_440
        ),
    }
    normalized["alert_channel_id"] = _positive_id(
        config.get("alert_channel_id")
    )
    normalized["trusted_roles"] = _id_list(config.get("trusted_roles"))
    normalized["trusted_members"] = _id_list(config.get("trusted_members"))
    try:
        lockdown_until = float(config.get("lockdown_until", 0.0))
        normalized["lockdown_until"] = (
            max(0.0, lockdown_until) if isfinite(lockdown_until) else 0.0
        )
    except (TypeError, ValueError, OverflowError):
        normalized["lockdown_until"] = 0.0
    normalized["temporary_bans"] = _id_list(
        config.get("temporary_bans"), maximum=1_000
    )

    config.clear()
    config.update(normalized)
    return before != dict(config)


def enabled_protections(config: Mapping[str, Any]) -> tuple[str, ...]:
    protections = _mapping(config.get("protections"))
    return tuple(
        name
        for name in PROTECTION_ORDER
        if bool(_mapping(protections.get(name)).get("enabled", False))
    )


def account_is_suspicious(
    created_at: datetime,
    *,
    max_age_hours: int,
    has_avatar: bool,
    require_no_avatar: bool,
    now: datetime | None = None,
) -> bool:
    """Return whether a joining account contributes to the risky cluster."""
    current = now or datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    age_hours = max(0.0, (current - created_at).total_seconds() / 3_600)
    if age_hours > max_age_hours:
        return False
    return not has_avatar if require_no_avatar else True


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3_600:
        value = max(1, round(seconds / 3_600))
        return f"{value} hour{'s' if value != 1 else ''}"
    if seconds >= 60:
        value = max(1, round(seconds / 60))
        return f"{value} minute{'s' if value != 1 else ''}"
    return f"{seconds} second{'s' if seconds != 1 else ''}"
