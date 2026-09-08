"""Validation for dashboard-managed Discord modules."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from botutils.antiraid import (
    SCHEMA_VERSION as ANTIRAID_SCHEMA_VERSION,
    VALID_JOIN_ACTIONS as ANTIRAID_JOIN_ACTIONS,
    VALID_MODES as ANTIRAID_VALID_MODES,
    VALID_STAFF_ACTIONS as ANTIRAID_STAFF_ACTIONS,
    recommended_config as recommended_antiraid_config,
)
from botutils.antispam import (
    MODULE_DEFAULTS as ANTISPAM_MODULE_DEFAULTS,
    MODULE_ORDER as ANTISPAM_MODULE_ORDER,
    SCHEMA_VERSION as ANTISPAM_SCHEMA_VERSION,
    VALID_MODES as ANTISPAM_VALID_MODES,
)
from botutils.starboard import (
    BOARD_ID_RE,
    MAX_BOARD_NAME,
    MAX_BOARDS,
    emoji_key,
)
from .validation import ValidationError

MODULE_NAMES = {
    "autorole",
    "chatfilter",
    "logger",
    "modmail",
    "anti_spam",
    "anti_raid",
    "welcome",
    "leave",
    "restore_roles",
    "selfroles",
    "starboard",
    "vc_log",
}

LOGGER_GROUPS = (
    "Mentions",
    "Message Edit",
    "Message Delete",
    "Message Update",
    "Server Update",
    "Channel Update",
    "Role Update",
    "Webhook Update",
    "Associations",
    "Member Update",
    "Emoji Update",
    "Sticker Update",
    "Invite Update",
    "Auto Moderation",
    "Threads",
    "Ghost Typing",
    "Scheduled Events",
    "Stage Events",
    "Soundboard",
)

LOGGER_CONFIGURATION_KEYS = {
    "channel",
    "channels",
    "secure",
    "ignored_roles",
    "ignored_channels",
    "ignored_bots",
    "disabled",
    "theme",
    "color",
    "colors",
    "local_archive",
}

LOGGER_ARCHIVE_KEYS = {
    "enabled",
    "retention_days",
    "cache_messages",
    "store_attachments",
    "attachment_size_limit_mb",
}
LOGGER_ARCHIVE_DEFAULTS = {
    "enabled": False,
    "retention_days": 30,
    "cache_messages": True,
    "store_attachments": False,
    "attachment_size_limit_mb": 25,
}

ANTISPAM_POLICY_TARGETS = {
    "default",
    "rate_limit",
    "mass_pings",
    "mass_pings_per_msg",
    "ghost_pings",
    "duplicates",
    "duplicate_segments",
    "same_link",
    "same_image",
    "sticker",
    "same_sticker",
    "inhuman",
    "copy_paste",
    "anti_macro",
    "evasion",
    "edit_churn",
    "delete_resend",
    "coordinated",
}
ANTISPAM_ACTIONS = {"adaptive", "observe", "delete", "warn", "kick", "ban"}


def _object(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValidationError("Module settings must be a JSON object.")
    return payload


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


def _whole_number(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, float) and not value.is_integer():
        raise ValidationError(f"{name} must be a whole number.")
    return _integer(value, name, minimum, maximum)


def _boolean(value: Any, name: str, default: bool) -> bool:
    if value is None:
        return default
    if type(value) is not bool:
        raise ValidationError(f"{name} must be on or off.")
    return value


def _snowflake(value: Any, name: str, *, optional: bool = True) -> int | None:
    if optional and value in (None, "", False):
        return None
    text = str(value)
    if not text.isdigit() or not 1 <= len(text) <= 20:
        raise ValidationError(f"{name} must be a valid Discord ID.")
    return int(text)


def _snowflakes(value: Any, name: str, maximum: int) -> list[int]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise ValidationError(f"{name} must contain at most {maximum} items.")
    result = []
    for item in value:
        snowflake = _snowflake(item, name, optional=False)
        if snowflake not in result:
            result.append(snowflake)
    return result


def _text(value: Any, name: str, maximum: int, *, required: bool = False) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise ValidationError(f"{name} cannot be empty.")
    if len(result) > maximum:
        raise ValidationError(f"{name} must be {maximum} characters or fewer.")
    return result


def _phrases(value: Any, name: str, maximum: int = 250) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise ValidationError(f"{name} must contain at most {maximum} entries.")
    result = []
    for entry in value:
        phrase = _text(entry, name, 200)
        if phrase and phrase not in result:
            result.append(phrase)
    return result


def _images(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > 20:
        raise ValidationError("Image URLs must contain at most 20 links.")
    result = []
    for entry in value:
        url = _text(entry, "Image URL", 500, required=True)
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValidationError("Each image must use a complete, secure link starting with https://.")
        if url not in result:
            result.append(url)
    return result


def _antispam_thresholds(value: Any, name: str) -> list[dict[str, int]]:
    if not isinstance(value, list) or len(value) > 25:
        raise ValidationError(f"{name} must contain between 0 and 25 rolling rules.")
    result: list[dict[str, int]] = []
    seen: set[tuple[int, int]] = set()
    for index, raw in enumerate(value, 1):
        if not isinstance(raw, dict):
            raise ValidationError(f"{name} rule {index} must be an object.")
        rule = {
            "threshold": _whole_number(
                raw.get("threshold"), f"{name} rule {index} message count", 2, 100
            ),
            "timespan": _whole_number(
                raw.get("timespan"), f"{name} rule {index} window", 1, 3600
            ),
        }
        marker = (rule["timespan"], rule["threshold"])
        if marker not in seen:
            seen.add(marker)
            result.append(rule)
    return sorted(result, key=lambda rule: (rule["timespan"], rule["threshold"]))


def _antispam_action(value: Any, target: str) -> str | None:
    action = str(value or ("adaptive" if target == "default" else "inherit")).lower()
    if action == "inherit" and target != "default":
        return None
    if action in ANTISPAM_ACTIONS:
        return action
    match = re.fullmatch(r"timeout:(\d+)", action)
    if match and 10 <= int(match.group(1)) <= 2_419_200:
        return f"timeout:{int(match.group(1))}"
    raise ValidationError(f"Choose a valid response for {target.replace('_', ' ')}.")


def _antispam_protections(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise ValidationError("AntiSpam protections must be a JSON object.")
    protections: dict[str, dict[str, Any]] = {}
    for name in ANTISPAM_MODULE_ORDER:
        raw = value.get(name)
        if not isinstance(raw, dict):
            raise ValidationError(f"Configure the {name.replace('_', ' ')} protection.")
        default = ANTISPAM_MODULE_DEFAULTS[name]
        enabled = _boolean(raw.get("enabled"), f"{name} enabled", True)
        if name == "rate_limit":
            protections[name] = {
                "enabled": enabled,
                "thresholds": _antispam_thresholds(
                    raw.get("thresholds", default), "Flood control"
                ),
            }
        elif name == "mass_pings":
            protections[name] = {
                "enabled": enabled,
                "per_message": _whole_number(
                    raw.get("per_message", default["per_message"]),
                    "Mentions per message",
                    0,
                    100,
                ),
                "ghost_pings": _boolean(
                    raw.get("ghost_pings"), "Ghost-ping detection", default["ghost_pings"]
                ),
                "thresholds": _antispam_thresholds(
                    raw.get("thresholds", default["thresholds"]), "Mention bursts"
                ),
            }
        elif name == "duplicates":
            protections[name] = {"enabled": enabled}
            for key, label, maximum in (
                ("per_message", "Repeated words per message", 100),
                ("same_link", "Repeated-link window", 3600),
                ("same_image", "Repeated-image window", 3600),
                ("sticker", "Sticker burst window", 3600),
                ("same_sticker", "Repeated-sticker window", 3600),
                ("max_open_threads", "Open threads per member", 100),
            ):
                protections[name][key] = _whole_number(
                    raw.get(key, default[key]), label, 0, maximum
                )
            protections[name]["thresholds"] = _antispam_thresholds(
                raw.get("thresholds", default["thresholds"]), "Repeated text"
            )
        elif name == "inhuman":
            protections[name] = {"enabled": enabled}
            for key, current_default in default.items():
                protections[name][key] = _boolean(
                    raw.get(key), key.replace("_", " ").title(), current_default
                )
        else:
            bounds = {
                "anti_macro": {
                    "samples": (6, 30), "tolerance": (1, 50),
                    "min_interval": (1, 300), "max_interval": (2, 3600),
                },
                "evasion": {
                    "zero_width": (1, 100), "combining_marks": (1, 100),
                    "spoiler_segments": (1, 100), "max_edits": (2, 25),
                    "edit_window": (2, 300), "resend_window": (0, 300),
                },
                "coordinated": {
                    "window": (3, 300), "unique_users": (2, 25),
                    "min_length": (6, 200),
                },
            }[name]
            protections[name] = {"enabled": enabled}
            for key, (minimum, maximum) in bounds.items():
                protections[name][key] = _whole_number(
                    raw.get(key, default[key]), key.replace("_", " ").title(), minimum, maximum
                )
            if name == "anti_macro" and protections[name]["max_interval"] <= protections[name]["min_interval"]:
                raise ValidationError("Automation maximum interval must be greater than its minimum interval.")
    active_checks = {
        "rate_limit": lambda item: bool(item["thresholds"]),
        "mass_pings": lambda item: bool(item["per_message"] or item["ghost_pings"] or item["thresholds"]),
        "duplicates": lambda item: bool(item["thresholds"] or any(item[key] for key in ("per_message", "same_link", "same_image", "sticker", "same_sticker", "max_open_threads"))),
        "inhuman": lambda item: any(item[key] for key in ANTISPAM_MODULE_DEFAULTS["inhuman"]),
    }
    for name, has_checks in active_checks.items():
        if protections[name]["enabled"] and not has_checks(protections[name]):
            raise ValidationError(
                f"{name.replace('_', ' ').title()} is enabled but has no active checks. Add a check or disable that protection."
            )
    return protections


def _logger_event(value: Any) -> str:
    event = str(value or "").strip()
    if not re.fullmatch(r"[a-z0-9_]{1,50}", event):
        raise ValidationError(f"That logging event name is not valid: {event or 'empty'}")
    return event


def _logger_archive(value: Any) -> dict[str, Any]:
    if value is None:
        return dict(LOGGER_ARCHIVE_DEFAULTS)
    if not isinstance(value, dict):
        raise ValidationError("Local Logging history settings must be a JSON object.")
    unknown = set(value) - LOGGER_ARCHIVE_KEYS
    if unknown:
        raise ValidationError(
            f"Local Logging history contains an unsupported setting: {sorted(unknown)[0]}"
        )
    return {
        "enabled": _boolean(
            value.get("enabled"), "Local Logging history", False
        ),
        "retention_days": _integer(
            value.get("retention_days", 30),
            "Local Logging history retention",
            1,
            365,
        ),
        "cache_messages": _boolean(
            value.get("cache_messages"), "Uncached message recovery", True
        ),
        "store_attachments": _boolean(
            value.get("store_attachments"), "Local image and file storage", False
        ),
        "attachment_size_limit_mb": _whole_number(
            value.get("attachment_size_limit_mb", 25),
            "Local image and file size limit",
            1,
            25,
        ),
    }


def _logger_configuration(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValidationError("The logging JSON must contain one configuration object.")
    unknown = set(value) - LOGGER_CONFIGURATION_KEYS
    if unknown:
        raise ValidationError(f"The logging JSON contains an unsupported setting: {sorted(unknown)[0]}")

    routes = value.get("channels", {})
    if not isinstance(routes, dict) or len(routes) > 150:
        raise ValidationError("Logging JSON can contain at most 150 event routes.")
    normalized_routes = {
        _logger_event(event): _snowflake(
            channel_id,
            f"Destination for {event}",
            optional=False,
        )
        for event, channel_id in routes.items()
    }

    disabled = value.get("disabled", [])
    if not isinstance(disabled, list) or len(disabled) > 150:
        raise ValidationError("Logging JSON can contain at most 150 disabled events.")
    normalized_disabled = []
    for event in disabled:
        event = _logger_event(event)
        if event not in normalized_disabled:
            normalized_disabled.append(event)

    theme = value.get("theme") or None
    if theme not in {None, "Role Color", "RGB", "Solid Color", "Custom"}:
        raise ValidationError(
            "Logging theme must be Default, Role Color, Changing Colors, Solid Color, or Custom."
        )

    colors = value.get("colors", {})
    if not isinstance(colors, dict) or len(colors) > 150:
        raise ValidationError("Logging JSON can contain at most 150 custom event colors.")
    normalized_colors = {
        _logger_event(event): _integer(color, f"Color for {event}", 0, 0xFFFFFF)
        for event, color in colors.items()
    }

    result = {
        "channel": _snowflake(value.get("channel"), "Primary logging channel", optional=False),
        "channels": normalized_routes,
        "secure": bool(value.get("secure", False)),
        "ignored_roles": _snowflakes(value.get("ignored_roles", []), "Ignored roles", 100),
        "ignored_channels": _snowflakes(
            value.get("ignored_channels", []),
            "Ignored logging channels",
            100,
        ),
        "ignored_bots": _snowflakes(value.get("ignored_bots", []), "Ignored bots", 100),
        "disabled": normalized_disabled,
        "theme": theme,
        "local_archive": _logger_archive(value.get("local_archive")),
    }
    if theme == "Solid Color" or "color" in value:
        result["color"] = _integer(value.get("color"), "Solid logging color", 0, 0xFFFFFF)
    if theme == "Custom" or colors:
        result["colors"] = normalized_colors
    return result


def _message_module(payload: dict[str, Any], label: str, default_message: str) -> dict[str, Any]:
    enabled = bool(payload.get("enabled", False))
    channel_id = _snowflake(payload.get("channel_id"), f"{label} channel")
    if enabled and not channel_id:
        raise ValidationError(f"Choose a {label.lower()} channel before enabling it.")
    images = _images(payload.get("image_urls", []))
    return {
        "enabled": enabled,
        "channel_id": channel_id,
        "message": _text(
            payload.get("message", default_message),
            f"{label} message",
            1900,
            required=True,
        ),
        "use_images": bool(payload.get("use_images", False)),
        "image_urls": images,
    }


def validate_module_settings(module: str, payload: Any) -> dict[str, Any]:
    """Normalize one module write without accepting unrelated configuration."""
    if module not in MODULE_NAMES:
        raise ValidationError("That dashboard module is not supported.")
    raw = _object(payload)

    if module == "autorole":
        roles = raw.get("roles", [])
        if not isinstance(roles, list) or len(roles) > 25:
            raise ValidationError("Auto Role supports at most 25 roles.")
        normalized_roles = []
        seen = set()
        for item in roles:
            if not isinstance(item, dict):
                raise ValidationError("Each auto role must include a role and delay.")
            role_id = _snowflake(item.get("role_id"), "Auto role", optional=False)
            if role_id in seen:
                continue
            delay = _integer(item.get("delay", 0), "Auto role delay", 0, 31_536_000)
            if 0 < delay < 60:
                raise ValidationError("Delayed auto roles must wait at least 60 seconds.")
            normalized_roles.append({"role_id": role_id, "delay": delay})
            seen.add(role_id)
        enabled = bool(raw.get("enabled", False))
        if enabled and not normalized_roles:
            raise ValidationError("Choose at least one role before enabling Auto Role.")
        return {
            "enabled": enabled,
            "wait_for_verify": bool(raw.get("wait_for_verify", False)),
            "roles": normalized_roles,
        }

    if module == "chatfilter":
        return {
            "enabled": bool(raw.get("enabled", False)),
            "blacklist": _phrases(raw.get("blacklist", []), "Filtered words"),
            "whitelist": _phrases(raw.get("whitelist", []), "Allowed words"),
            "ignored_channels": _snowflakes(raw.get("ignored_channels", []), "Ignored channels", 50),
            "regex": bool(raw.get("regex", False)),
            "filter_nicks": bool(raw.get("filter_nicks", False)),
            "filter_bots": bool(raw.get("filter_bots", False)),
            "filter_webhooks": bool(raw.get("filter_webhooks", False)),
            "filter_phishing": bool(raw.get("filter_phishing", False)),
        }

    if module == "logger":
        enabled = bool(raw.get("enabled", False))
        configuration = _logger_configuration(raw.get("configuration")) if enabled else None
        if enabled and configuration is not None:
            return {
                "enabled": True,
                "channel_id": configuration["channel"],
                "secure": configuration["secure"],
                "theme": configuration["theme"],
                "groups": list(LOGGER_GROUPS),
                "ignored_channels": configuration["ignored_channels"],
                "local_archive": configuration["local_archive"],
                "configuration": configuration,
            }
        channel_id = _snowflake(raw.get("channel_id"), "Log channel")
        if enabled and not channel_id:
            raise ValidationError("Choose a channel before enabling Logging.")
        groups = raw.get("groups", list(LOGGER_GROUPS))
        if not isinstance(groups, list):
            raise ValidationError("Choose at least one type of activity to record.")
        selected = []
        for group in groups:
            if group not in LOGGER_GROUPS:
                raise ValidationError(f"That Logging option is not available: {group}")
            if group not in selected:
                selected.append(group)
        theme = raw.get("theme") or None
        if theme not in {None, "Role Color", "RGB", "Solid Color", "Custom"}:
            raise ValidationError(
                "Choose a supported Logging color theme."
            )
        return {
            "enabled": enabled,
            "channel_id": channel_id,
            "secure": bool(raw.get("secure", False)),
            "theme": theme,
            "groups": selected,
            "ignored_channels": _snowflakes(raw.get("ignored_channels", []), "Ignored log channels", 50),
            "local_archive": _logger_archive(raw.get("local_archive")),
            "configuration": None,
        }

    if module == "modmail":
        enabled = bool(raw.get("enabled", False))
        channel_id = _snowflake(raw.get("channel_id"), "Modmail channel")
        if enabled and not channel_id:
            raise ValidationError("Choose a staff channel before enabling Modmail.")
        return {
            "enabled": enabled,
            "channel_id": channel_id,
        }

    if module == "anti_spam":
        protections = _antispam_protections(raw.get("protections", {}))
        mode = str(raw.get("mode", "enforce")).lower()
        if mode not in ANTISPAM_VALID_MODES:
            raise ValidationError("Choose Observe, Delete only, or Enforce mode.")
        raw_punishments = raw.get("punishments", {})
        if not isinstance(raw_punishments, dict):
            raise ValidationError("AntiSpam response policies must be a JSON object.")
        unknown_targets = set(raw_punishments) - ANTISPAM_POLICY_TARGETS
        if unknown_targets:
            raise ValidationError(
                f"That AntiSpam policy is not supported: {sorted(unknown_targets)[0]}"
            )
        punishments: dict[str, str] = {}
        for target in ANTISPAM_POLICY_TARGETS:
            action = _antispam_action(raw_punishments.get(target), target)
            if action is not None:
                punishments[target] = action
        punishments.setdefault("default", "adaptive")
        result = {
            "schema_version": ANTISPAM_SCHEMA_VERSION,
            "enabled": _boolean(raw.get("enabled"), "AntiSpam", False),
            "mode": mode,
            "protections": protections,
            "punishments": punishments,
            "ignored_channels": _snowflakes(raw.get("ignored_channels", []), "Ignored channels", 250),
            "trusted_roles": _snowflakes(raw.get("trusted_roles", []), "Trusted roles", 250),
            "trusted_members": _snowflakes(raw.get("trusted_members", []), "Trusted members", 250),
        }
        if result["enabled"] and not any(
            protection["enabled"] for protection in protections.values()
        ):
            raise ValidationError("Choose at least one Spam Protection check.")
        return result

    if module == "anti_raid":
        defaults = recommended_antiraid_config(enabled=False)
        raw_protections = raw.get("protections", {})
        raw_response = raw.get("response", {})
        if not isinstance(raw_protections, dict):
            raise ValidationError("Raid Protection checks must be a JSON object.")
        if not isinstance(raw_response, dict):
            raise ValidationError("Raid Protection responses must be a JSON object.")

        def protection(name: str) -> dict[str, Any]:
            value = raw_protections.get(name, {})
            if not isinstance(value, dict):
                raise ValidationError(f"{name.replace('_', ' ').title()} must be an object.")
            return value

        burst = protection("join_burst")
        risky = protection("suspicious_accounts")
        destructive = protection("destructive_actions")
        mode = str(raw.get("mode", defaults["mode"])).lower()
        if mode not in ANTIRAID_VALID_MODES:
            raise ValidationError("Choose Observe or Enforce mode for Raid Protection.")
        join_action = str(
            raw_response.get("join_action", defaults["response"]["join_action"])
        ).lower()
        staff_action = str(
            raw_response.get("staff_action", defaults["response"]["staff_action"])
        ).lower()
        if join_action not in ANTIRAID_JOIN_ACTIONS:
            raise ValidationError("Choose Kick or Temporary Ban for join containment.")
        if staff_action not in ANTIRAID_STAFF_ACTIONS:
            raise ValidationError("Choose a valid compromised-staff response.")
        result = {
            "schema_version": ANTIRAID_SCHEMA_VERSION,
            "enabled": _boolean(raw.get("enabled"), "Raid Protection", False),
            "mode": mode,
            "protections": {
                "join_burst": {
                    "enabled": _boolean(burst.get("enabled"), "Join burst shield", True),
                    "threshold": _whole_number(burst.get("threshold", 10), "Join burst count", 3, 100),
                    "window": _whole_number(burst.get("window", 20), "Join burst window", 3, 300),
                },
                "suspicious_accounts": {
                    "enabled": _boolean(risky.get("enabled"), "New-account clusters", True),
                    "max_account_age_hours": _whole_number(
                        risky.get("max_account_age_hours", 24), "Maximum account age", 1, 8_760
                    ),
                    "threshold": _whole_number(risky.get("threshold", 4), "Risk cluster count", 2, 50),
                    "window": _whole_number(risky.get("window", 30), "Risk cluster window", 3, 600),
                    "require_no_avatar": _boolean(
                        risky.get("require_no_avatar"), "Require a blank avatar", False
                    ),
                },
                "destructive_actions": {
                    "enabled": _boolean(
                        destructive.get("enabled"), "Compromised staff guard", True
                    ),
                    "threshold": _whole_number(
                        destructive.get("threshold", 3), "Destructive action count", 2, 25
                    ),
                    "window": _whole_number(
                        destructive.get("window", 15), "Destructive action window", 3, 300
                    ),
                    **{
                        key: _boolean(destructive.get(key), label, True)
                        for key, label in (
                            ("watch_bans", "Watch bans"),
                            ("watch_kicks", "Watch kicks"),
                            ("watch_channels", "Watch channel deletion"),
                            ("watch_roles", "Watch role deletion"),
                            ("watch_webhooks", "Watch webhook deletion"),
                        )
                    },
                },
            },
            "response": {
                "join_action": join_action,
                "staff_action": staff_action,
                "lock_minutes": _whole_number(
                    raw_response.get("lock_minutes", 10), "Lockdown duration", 1, 1_440
                ),
            },
            "alert_channel_id": _snowflake(raw.get("alert_channel_id"), "Incident alert channel"),
            "trusted_roles": _snowflakes(raw.get("trusted_roles", []), "Trusted roles", 250),
            "trusted_members": _snowflakes(raw.get("trusted_members", []), "Trusted members", 250),
        }
        if result["enabled"] and not any(
            settings["enabled"] for settings in result["protections"].values()
        ):
            raise ValidationError("Choose at least one Raid Protection check.")
        destructive_result = result["protections"]["destructive_actions"]
        if destructive_result["enabled"] and not any(
            destructive_result[key]
            for key in (
                "watch_bans",
                "watch_kicks",
                "watch_channels",
                "watch_roles",
                "watch_webhooks",
            )
        ):
            raise ValidationError("Choose at least one destructive staff action to watch.")
        if (
            result["enabled"]
            and result["mode"] == "enforce"
            and result["protections"]["destructive_actions"]["enabled"]
            and staff_action in {"kick", "ban"}
            and raw.get("confirm_staff_action") is not True
        ):
            raise ValidationError(f"Confirm the {staff_action.title()} staff response before saving.")
        return result

    if module == "welcome":
        result = _message_module(raw, "Welcome", "Welcome !mention")
        result["wait_for_verify"] = bool(raw.get("wait_for_verify", False))
        return result

    if module == "leave":
        return _message_module(raw, "Leave", "!user left !server")

    if module == "restore_roles":
        return {
            "enabled": bool(raw.get("enabled", False)),
            "allow_permissions": bool(raw.get("allow_permissions", False)),
        }

    if module == "starboard":
        boards = raw.get("boards", [])
        if not isinstance(boards, list) or len(boards) > MAX_BOARDS:
            raise ValidationError(
                f"Starboard supports at most {MAX_BOARDS} boards per server."
            )
        normalized = []
        names: set[str] = set()
        emojis: set[str] = set()
        identifiers: set[str] = set()
        for index, board in enumerate(boards, 1):
            if not isinstance(board, dict):
                raise ValidationError(f"Starboard {index} must be an object.")
            name = _text(
                board.get("name"),
                f"Starboard {index} name",
                MAX_BOARD_NAME,
                required=True,
            )
            name_key = name.casefold()
            if name_key in names:
                raise ValidationError("Starboard names must be unique in this server.")
            names.add(name_key)

            board_id = _text(board.get("board_id"), "Starboard identifier", 32)
            if board_id:
                if not BOARD_ID_RE.fullmatch(board_id) or board_id in identifiers:
                    raise ValidationError("That starboard identifier is invalid or duplicated.")
                identifiers.add(board_id)

            channel_id = _snowflake(
                board.get("channel_id"), f"{name} destination", optional=False
            )
            emoji = _text(board.get("emoji"), f"{name} emoji", 100, required=True)
            try:
                key = emoji_key(emoji)
            except ValueError as error:
                raise ValidationError(str(error)) from error
            if key in emojis:
                raise ValidationError("Each starboard must use a different reaction emoji.")
            emojis.add(key)
            normalized.append(
                {
                    "board_id": board_id or None,
                    "name": name,
                    "channel_id": channel_id,
                    "emoji": emoji,
                    "threshold": _integer(
                        board.get("threshold", 3),
                        f"{name} reaction threshold",
                        1,
                        100,
                    ),
                    "enabled": _boolean(board.get("enabled"), f"{name} state", True),
                }
            )
        return {
            "enabled": any(board["enabled"] for board in normalized),
            "boards": normalized,
        }

    if module == "vc_log":
        enabled = bool(raw.get("enabled", False))
        channel_id = _snowflake(raw.get("channel_id"), "Voice log channel")
        if enabled and not channel_id:
            raise ValidationError("Choose a channel before enabling the Voice Activity Log.")
        return {
            "enabled": enabled,
            "channel_id": channel_id,
            "keep_clean": bool(raw.get("keep_clean", True)),
        }

    action = str(raw.get("action", "save"))
    if action not in {"create", "update", "delete"}:
        raise ValidationError("Choose a valid self-role menu action.")
    menu_id = _snowflake(raw.get("menu_id"), "Self-role menu", optional=action == "create")
    if action == "delete":
        return {"action": action, "menu_id": menu_id}
    channel_id = _snowflake(raw.get("channel_id"), "Self-role channel")
    if action == "create" and not channel_id:
        raise ValidationError("Choose a channel for the new self-role menu.")
    role_ids = _snowflakes(raw.get("role_ids", []), "Self-role roles", 25)
    if not role_ids:
        raise ValidationError("Choose at least one role for the self-role menu.")
    return {
        "action": action,
        "menu_id": menu_id,
        "channel_id": channel_id,
        "role_ids": role_ids,
        "text": _text(raw.get("text", "Choose your role"), "Menu message", 1900, required=True),
        "label": _text(raw.get("label", "Select your role"), "Menu label", 100, required=True),
        "style": "buttons" if raw.get("style") == "buttons" else "dropdown",
        "limit": _integer(raw.get("limit", 1), "Role selection limit", 0, 25),
        "show_percentage": bool(raw.get("show_percentage", True)),
        "show_roles": bool(raw.get("show_roles", True)),
    }
