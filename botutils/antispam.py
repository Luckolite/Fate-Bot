"""Pure configuration and detection helpers for Fate's anti-spam system.

The Discord cog deliberately keeps network and moderation work separate from
these helpers.  That makes configuration migrations and the less obvious
content heuristics deterministic and inexpensive to test.
"""

from __future__ import annotations

import re
import statistics
import unicodedata
from copy import deepcopy
from typing import Any, Iterable, Mapping, MutableMapping, Optional, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SCHEMA_VERSION = 2
VALID_MODES = ("observe", "delete", "enforce")

MODULE_ORDER = (
    "rate_limit",
    "mass_pings",
    "duplicates",
    "inhuman",
    "anti_macro",
    "evasion",
    "coordinated",
)

MODULE_INFO = {
    "rate_limit": {
        "label": "Flood control",
        "emoji": "🌊",
        "summary": "Rolling per-member message limits stop fast floods.",
    },
    "mass_pings": {
        "label": "Mentions & ghost pings",
        "emoji": "📣",
        "summary": "Limits mention bursts and reports deleted pings.",
    },
    "duplicates": {
        "label": "Repeats, links & media",
        "emoji": "🧬",
        "summary": "Finds normalized repeats, links, images, stickers, and thread spam.",
    },
    "inhuman": {
        "label": "Message integrity",
        "emoji": "🧱",
        "summary": "Catches character walls, vertical spam, and malformed text.",
    },
    "anti_macro": {
        "label": "Automation cadence",
        "emoji": "⏱️",
        "summary": "Detects long, unnaturally regular posting cadences.",
    },
    "evasion": {
        "label": "Evasion resistance",
        "emoji": "🫥",
        "summary": "Finds invisible Unicode, Zalgo, spoiler walls, and edit churn.",
    },
    "coordinated": {
        "label": "Coordinated campaigns",
        "emoji": "🕸️",
        "summary": "Clusters the same normalized payload across members and channels.",
    },
}

MODULE_DEFAULTS = {
    "rate_limit": [
        {"timespan": 3, "threshold": 4},
        {"timespan": 10, "threshold": 6},
    ],
    "mass_pings": {
        "per_message": 4,
        "ghost_pings": True,
        "thresholds": [
            {"timespan": 10, "threshold": 3},
            {"timespan": 30, "threshold": 6},
        ],
    },
    "duplicates": {
        "per_message": 10,
        "same_link": 25,
        "same_image": 25,
        "sticker": 10,
        "same_sticker": 60,
        "max_open_threads": 1,
        "thresholds": [{"timespan": 25, "threshold": 4}],
    },
    "inhuman": {
        "non_abc": True,
        "tall_messages": True,
        "empty_lines": True,
        "unknown_chars": True,
        "ascii": True,
        "copy_paste": True,
    },
    "anti_macro": {
        "samples": 14,
        "tolerance": 8,
        "min_interval": 1,
        "max_interval": 45,
    },
    "evasion": {
        "zero_width": 6,
        "combining_marks": 12,
        "spoiler_segments": 10,
        "max_edits": 4,
        "edit_window": 20,
        "resend_window": 30,
    },
    "coordinated": {
        "window": 20,
        "unique_users": 3,
        "min_length": 18,
    },
}

SYSTEM_KEYS = {
    "enabled",
    "mode",
    "schema_version",
    "ignored",
    "trusted_roles",
    "trusted_members",
    "disabled_modules",
    "punishments",
}

DETECTION_PARENT = {
    "rate_limit": "rate_limit",
    "mass_pings": "mass_pings",
    "mass_pings_per_msg": "mass_pings",
    "ghost_pings": "mass_pings",
    "duplicates": "duplicates",
    "duplicate_segments": "duplicates",
    "same_link": "duplicates",
    "same_image": "duplicates",
    "sticker": "duplicates",
    "same_sticker": "duplicates",
    "max_open_threads": "duplicates",
    "non_abc": "inhuman",
    "tall_messages": "inhuman",
    "empty_lines": "inhuman",
    "unknown_chars": "inhuman",
    "ascii": "inhuman",
    "copy_paste": "inhuman",
    "inhuman": "inhuman",
    "anti_macro": "anti_macro",
    "zero_width": "evasion",
    "combining_marks": "evasion",
    "spoiler_segments": "evasion",
    "edit_churn": "evasion",
    "delete_resend": "evasion",
    "evasion": "evasion",
    "coordinated": "coordinated",
}

ACTION_LABELS = {
    "adaptive": "Adaptive timeout",
    "observe": "Observe only",
    "delete": "Delete only",
    "warn": "Warn + delete",
    "kick": "Kick",
    "ban": "Ban",
}

URL_RE = re.compile(
    r"(?i)\b(?:https?://|www\.|discord(?:app)?\.com/invite/|discord\.gg/)"
    r"[^\s<>]+"
)
MARKDOWN_RE = re.compile(r"[*_~`>|]+")
SPACE_RE = re.compile(r"\s+")
REPEATED_PUNCTUATION_RE = re.compile(r"([^\w\s])\1{2,}")
REPEATED_CHARACTER_RE = re.compile(r"([\w])\1{4,}", re.IGNORECASE)

TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "ref_src",
}

# A deliberately small confusable map.  It catches common ASCII-lookalike
# mutations without pretending to transliterate whole languages.
CONFUSABLES = str.maketrans(
    {
        "а": "a",
        "е": "e",
        "о": "o",
        "р": "p",
        "с": "c",
        "х": "x",
        "у": "y",
        "і": "i",
        "ј": "j",
        "α": "a",
        "β": "b",
        "ε": "e",
        "ζ": "z",
        "η": "h",
        "ι": "i",
        "κ": "k",
        "μ": "m",
        "ν": "n",
        "ο": "o",
        "ρ": "p",
        "τ": "t",
        "υ": "y",
        "χ": "x",
    }
)

# These format controls have legitimate shaping or bidirectional-text uses and
# must not be treated as invisible-spam evidence.  Fingerprinting may still
# remove them so visually equivalent content compares consistently.
LEGITIMATE_FORMAT_CONTROLS = {
    "\u061c",  # Arabic letter mark
    "\u200c",  # zero-width non-joiner
    "\u200d",  # zero-width joiner (also compound emoji)
    "\u200e",  # left-to-right mark
    "\u200f",  # right-to-left mark
    "\u2066",  # left-to-right isolate
    "\u2067",  # right-to-left isolate
    "\u2068",  # first-strong isolate
    "\u2069",  # pop directional isolate
}


def recommended_config() -> dict[str, Any]:
    """Return a complete, independent recommended configuration."""
    return {
        "schema_version": SCHEMA_VERSION,
        "enabled": True,
        "mode": "enforce",
        "ignored": [],
        "trusted_roles": [],
        "trusted_members": [],
        "disabled_modules": [],
        "punishments": {
            "default": "adaptive",
            "ghost_pings": "warn",
            "copy_paste": "warn",
        },
        **deepcopy(MODULE_DEFAULTS),
    }


def _normalize_id_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        try:
            parsed = int(item)
        except (TypeError, ValueError):
            continue
        if parsed > 0 and parsed not in result:
            result.append(parsed)
    return result


def _coerce_int(
    value: Any, default: int, minimum: int, maximum: int
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def normalize_thresholds(value: Any, fallback: Sequence[Mapping[str, int]]) -> list[dict[str, int]]:
    """Validate, deduplicate, and sort rolling-window rules."""
    # An empty list (or legacy None) was an intentional way to disable this
    # detector without removing its parent module.  Never turn it back on
    # during migration.
    if value is None:
        return []
    supplied_list = isinstance(value, list)
    if not supplied_list:
        value = fallback
    result = []
    for rule in value:
        if not isinstance(rule, Mapping):
            continue
        try:
            timespan = int(rule.get("timespan", 0))
            threshold = int(rule.get("threshold", 0))
        except (TypeError, ValueError):
            continue
        if not 1 <= timespan <= 3600 or not 2 <= threshold <= 100:
            continue
        normalized = {"timespan": timespan, "threshold": threshold}
        if normalized not in result:
            result.append(normalized)
    if not result and not supplied_list:
        result = deepcopy(list(fallback))
    return sorted(result, key=lambda rule: (rule["timespan"], rule["threshold"]))


def normalize_action(value: Any) -> str | None:
    """Convert legacy punishment values to the current stable action format."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return f"timeout:{max(1, min(value, 2_419_200))}"
    if not isinstance(value, str):
        return None
    action = value.strip().lower().replace("purge", "delete")
    if action == "mute":
        action = "adaptive"
    if action in ACTION_LABELS:
        return action
    if action.startswith("timeout:"):
        try:
            seconds = int(action.split(":", 1)[1])
        except ValueError:
            return None
        return f"timeout:{max(1, min(seconds, 2_419_200))}"
    return None


def normalize_config(config: MutableMapping[str, Any]) -> bool:
    """Migrate one cached guild config in place; return whether it changed."""
    before = deepcopy(dict(config))
    legacy_schema = config.get("schema_version") != SCHEMA_VERSION
    config["schema_version"] = SCHEMA_VERSION
    config["enabled"] = bool(config.get("enabled", True))
    mode = str(config.get("mode", "enforce")).lower()
    config["mode"] = mode if mode in VALID_MODES else "enforce"
    for key in ("ignored", "trusted_roles", "trusted_members"):
        config[key] = _normalize_id_list(config.get(key, []))

    disabled = config.get("disabled_modules", [])
    if not isinstance(disabled, list):
        disabled = []
    config["disabled_modules"] = list(
        dict.fromkeys(name for name in disabled if name in MODULE_ORDER)
    )

    raw_punishments = config.get("punishments", {})
    if not isinstance(raw_punishments, Mapping):
        raw_punishments = {}
    punishments = {}
    for target, raw_action in raw_punishments.items():
        action = normalize_action(raw_action)
        if action and (target == "default" or target in DETECTION_PARENT):
            punishments[str(target)] = action
    punishments.setdefault("default", "adaptive")
    punishments.setdefault("ghost_pings", "warn")
    punishments.setdefault("copy_paste", "warn")
    config["punishments"] = punishments

    if "rate_limit" in config:
        config["rate_limit"] = normalize_thresholds(
            config["rate_limit"], MODULE_DEFAULTS["rate_limit"]
        )
    if "mass_pings" in config:
        raw = config["mass_pings"] if isinstance(config["mass_pings"], Mapping) else {}
        module = deepcopy(MODULE_DEFAULTS["mass_pings"])
        module.update({key: raw[key] for key in module if key in raw})
        module["per_message"] = (
            0
            if module["per_message"] is None
            else _coerce_int(
                module["per_message"],
                MODULE_DEFAULTS["mass_pings"]["per_message"],
                0,
                100,
            )
        )
        module["ghost_pings"] = bool(module["ghost_pings"])
        module["thresholds"] = normalize_thresholds(
            module["thresholds"], MODULE_DEFAULTS["mass_pings"]["thresholds"]
        )
        # Legacy mention checks used a strict `>` boundary.  Increment valid
        # stored limits once so migration preserves the moderator's effective
        # trigger point while the new UI can describe intuitive `>=` limits.
        if legacy_schema:
            if (
                isinstance(raw.get("per_message"), int)
                and not isinstance(raw.get("per_message"), bool)
                and module["per_message"] > 0
            ):
                module["per_message"] = min(100, module["per_message"] + 1)
            if isinstance(raw.get("thresholds"), list):
                for rule in module["thresholds"]:
                    rule["threshold"] = min(100, rule["threshold"] + 1)
        config["mass_pings"] = module
    if "duplicates" in config:
        raw = config["duplicates"] if isinstance(config["duplicates"], Mapping) else {}
        module = deepcopy(MODULE_DEFAULTS["duplicates"])
        module.update({key: raw[key] for key in module if key in raw})
        for key in (
            "per_message",
            "same_link",
            "same_image",
            "sticker",
            "same_sticker",
            "max_open_threads",
        ):
            if module[key] is None:
                module[key] = 0
            else:
                module[key] = _coerce_int(
                    module[key], MODULE_DEFAULTS["duplicates"][key], 0, 3600
                )
        module["thresholds"] = normalize_thresholds(
            module["thresholds"], MODULE_DEFAULTS["duplicates"]["thresholds"]
        )
        if legacy_schema and isinstance(raw.get("thresholds"), list):
            for rule in module["thresholds"]:
                rule["threshold"] = min(100, rule["threshold"] + 1)
        config["duplicates"] = module
    if "inhuman" in config:
        raw = config["inhuman"] if isinstance(config["inhuman"], Mapping) else {}
        config["inhuman"] = {
            key: bool(raw.get(key, default))
            for key, default in MODULE_DEFAULTS["inhuman"].items()
        }
    for name in ("anti_macro", "evasion", "coordinated"):
        if name not in config:
            continue
        raw = config[name] if isinstance(config[name], Mapping) else {}
        module = deepcopy(MODULE_DEFAULTS[name])
        module.update({key: raw[key] for key in module if key in raw})
        bounds = {
            "anti_macro": {
                "samples": (6, 30),
                "tolerance": (1, 50),
                "min_interval": (1, 300),
                "max_interval": (2, 3600),
            },
            "evasion": {
                "zero_width": (1, 100),
                "combining_marks": (1, 100),
                "spoiler_segments": (1, 100),
                "max_edits": (2, 25),
                "edit_window": (2, 300),
                "resend_window": (0, 300),
            },
            "coordinated": {
                "window": (3, 300),
                "unique_users": (2, 25),
                "min_length": (6, 200),
            },
        }
        for key, default in MODULE_DEFAULTS[name].items():
            minimum, maximum = bounds[name][key]
            module[key] = _coerce_int(module[key], default, minimum, maximum)
        if (
            name == "anti_macro"
            and module["max_interval"] <= module["min_interval"]
        ):
            module["max_interval"] = max(
                MODULE_DEFAULTS["anti_macro"]["max_interval"],
                module["min_interval"] + 1,
            )
        config[name] = module

    return before != dict(config)


def module_enabled(config: Mapping[str, Any], module: str) -> bool:
    return (
        bool(config.get("enabled"))
        and module in config
        and module not in config.get("disabled_modules", [])
    )


def module_has_detectors(config: Mapping[str, Any], module: str) -> bool:
    """Return whether a configured module contains at least one live check."""
    if module not in config:
        return False
    value = config[module]
    if module == "rate_limit":
        return bool(value)
    if not isinstance(value, Mapping):
        return False
    if module == "mass_pings":
        return bool(
            value.get("per_message")
            or value.get("thresholds")
            or value.get("ghost_pings")
        )
    if module == "duplicates":
        return bool(
            value.get("thresholds")
            or any(
                value.get(key)
                for key in (
                    "per_message",
                    "same_link",
                    "same_image",
                    "sticker",
                    "same_sticker",
                    "max_open_threads",
                )
            )
        )
    if module == "inhuman":
        return any(bool(setting) for setting in value.values())
    return True


def set_module_enabled(config: MutableMapping[str, Any], module: str, enabled: bool) -> None:
    if module not in MODULE_ORDER:
        raise ValueError(f"Unknown anti-spam module: {module}")
    if module not in config or (enabled and not module_has_detectors(config, module)):
        config[module] = deepcopy(MODULE_DEFAULTS[module])
    disabled = config.setdefault("disabled_modules", [])
    if enabled:
        while module in disabled:
            disabled.remove(module)
    elif module not in disabled:
        disabled.append(module)


def canonicalize_url(raw: str) -> str:
    """Canonicalize a destination while removing common tracking noise."""
    url = raw.strip("<>[](){}.,!?;:'\"|*_~`")
    if url.lower().startswith("www."):
        url = "https://" + url
    elif url.lower().startswith("discord.gg/"):
        url = "https://" + url
    elif url.lower().startswith("discordapp.com/invite/"):
        url = "https://" + url
    parts = urlsplit(url)
    hostname = (parts.hostname or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    if hostname in {"discordapp.com", "discord.com"} and parts.path.startswith("/invite/"):
        hostname = "discord.gg"
        path = "/" + parts.path.split("/invite/", 1)[1]
    else:
        path = parts.path or "/"
    host = f"[{hostname}]" if ":" in hostname else hostname
    try:
        port = parts.port
    except ValueError:
        port = None
    if port and port not in {80, 443}:
        host = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", path).rstrip("/") or "/"
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_QUERY_KEYS
    ]
    return urlunsplit(("https", host, path, urlencode(sorted(query)), ""))


def extract_urls(content: str) -> list[str]:
    # Match after compatibility folding so full-width URL characters cannot
    # evade destination comparison.
    normalized = unicodedata.normalize("NFKC", content).casefold()
    return list(
        dict.fromkeys(
            canonicalize_url(match.group(0))
            for match in URL_RE.finditer(normalized)
        )
    )


def normalize_content(
    content: str, *, urls: Optional[Sequence[str]] = None
) -> str:
    """Create a conservative fingerprint resistant to common spam mutations."""
    normalized = unicodedata.normalize("NFKC", content).casefold().translate(CONFUSABLES)
    normalized = "".join(
        char
        for char in normalized
        if unicodedata.category(char) not in {"Cf", "Cc", "Cs"} or char in "\n\t"
    )
    urls = list(urls) if urls is not None else extract_urls(normalized)
    normalized = URL_RE.sub(" <url> ", normalized)
    normalized = MARKDOWN_RE.sub("", normalized)
    normalized = REPEATED_PUNCTUATION_RE.sub(r"\1\1", normalized)
    normalized = REPEATED_CHARACTER_RE.sub(r"\1\1\1", normalized)
    normalized = SPACE_RE.sub(" ", normalized).strip()
    if urls:
        normalized += " " + " ".join(f"<url:{url}>" for url in urls)
    return normalized[:1500]


def repeated_segment_score(
    content: str, *, normalized: Optional[str] = None
) -> int:
    """Return the largest adjacent phrase or exact-line repetition count."""
    normalized = normalized if normalized is not None else normalize_content(content)
    words = [
        word
        for word in normalized.split()
        if not word.startswith("<url")
    ]
    highest = 0
    # Look for adjacent repeated phrases up to four words long.  This catches
    # "buy now buy now" without treating ordinary repeated prose words such as
    # "the" as spam, and remains linear for Discord-sized messages.
    for width in range(1, min(4, len(words)) + 1):
        repeats = [1] * len(words)
        for start in range(len(words) - (2 * width), -1, -1):
            phrase = words[start : start + width]
            if sum(len(word) for word in phrase) < 2:
                continue
            if phrase == words[
                start + width : start + (2 * width)
            ]:
                repeats[start] = 1 + repeats[start + width]
                highest = max(highest, repeats[start])
    lines = [
        SPACE_RE.sub(" ", unicodedata.normalize("NFKC", line).casefold()).strip()
        for line in content.splitlines()
    ]
    lines = [line for line in lines if len(line) >= 2]
    line_counts: dict[str, int] = {}
    for line in lines:
        line_counts[line] = line_counts.get(line, 0) + 1
    return max(highest, max(line_counts.values(), default=0))


def evasion_signals(content: str, config: Mapping[str, int]) -> list[tuple[str, str]]:
    """Return high-confidence Unicode/formatting evasion signals."""
    signals = []
    invisible = sum(
        unicodedata.category(char) == "Cf"
        and char not in LEGITIMATE_FORMAT_CONTROLS
        for char in content
    )
    combining = sum(bool(unicodedata.combining(char)) for char in content)
    spoilers = content.count("||") // 2
    zero_width_limit = int(config.get("zero_width", 0) or 0)
    combining_limit = int(config.get("combining_marks", 0) or 0)
    spoiler_limit = int(config.get("spoiler_segments", 0) or 0)
    if zero_width_limit and invisible >= zero_width_limit:
        signals.append(("zero_width", f"{invisible} invisible Unicode characters"))
    if combining_limit and combining >= combining_limit:
        signals.append(("combining_marks", f"{combining} combining/Zalgo marks"))
    if spoiler_limit and spoilers >= spoiler_limit:
        signals.append(("spoiler_segments", f"{spoilers} spoiler segments"))
    return signals


def cadence_is_automated(
    timestamps: Sequence[float],
    *,
    samples: int,
    tolerance_percent: int,
    min_interval: int,
    max_interval: int,
) -> bool:
    """Detect a long cadence with very low timing variance."""
    samples = max(6, min(int(samples), 30))
    if len(timestamps) < samples:
        return False
    recent = list(timestamps[-samples:])
    intervals = [right - left for left, right in zip(recent, recent[1:])]
    mean = statistics.fmean(intervals)
    if mean < min_interval or mean > max_interval or mean <= 0:
        return False
    variation = statistics.pstdev(intervals) / mean * 100
    return variation <= max(1, min(tolerance_percent, 50))


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    units = ((86400, "d"), (3600, "h"), (60, "m"), (1, "s"))
    pieces = []
    for size, suffix in units:
        value, seconds = divmod(seconds, size)
        if value:
            pieces.append(f"{value}{suffix}")
        if len(pieces) == 2:
            break
    return " ".join(pieces) or "0s"


def format_action(action: Any) -> str:
    normalized = normalize_action(action) or "adaptive"
    if normalized.startswith("timeout:"):
        return f"Timeout {format_duration(int(normalized.split(':', 1)[1]))}"
    return ACTION_LABELS.get(normalized, normalized.replace("_", " ").title())


def format_thresholds(rules: Iterable[Mapping[str, int]]) -> str:
    rules = list(rules)
    rendered = " · ".join(
        f"{int(rule['threshold'])} / {format_duration(int(rule['timespan']))}"
        for rule in rules[:8]
    )
    if len(rules) > 8:
        rendered += f" · +{len(rules) - 8} more"
    return rendered or "No rolling limits"


def effective_action(config: Mapping[str, Any], module: str) -> str:
    punishments = config.get("punishments", {})
    if not isinstance(punishments, Mapping):
        return "adaptive"
    parent = DETECTION_PARENT.get(module, module)
    return (
        normalize_action(punishments.get(module))
        or normalize_action(punishments.get(parent))
        or normalize_action(punishments.get("default"))
        or "adaptive"
    )
