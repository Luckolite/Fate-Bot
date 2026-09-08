"""Authenticated LAN control plane for Fate.

This process deliberately lives outside the Discord bot so it remains reachable
while Fate is stopped.  The Android app discovers the public ``/status`` route;
all process and configuration mutations require a bearer token.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import errno
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
import platform
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep, time
from typing import Any, Iterator
from urllib.parse import urlsplit

import aiohttp
import psutil
from aiohttp import web

from botutils.backups import drive_token_path
from botutils.google_drive import (
    GoogleDriveClient,
    GoogleDriveCredentials,
    GoogleDriveError,
    GoogleDriveTokenStore,
)
from botutils.telemetry import (
    MAX_CHART_POINTS,
    SUPPORTED_METRICS,
    WINDOWS,
    TelemetryStore,
    build_metric_payload,
    telemetry_path_for_config,
)
from organism.fate_service import (
    OrganismServiceConfigurationError,
    OrganismServiceSettings,
)

LOGGER = logging.getLogger("fate.control")
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPOSITORY_ROOT / "data" / "config.json"
DEFAULT_TOKEN_PATH = REPOSITORY_ROOT / "data" / "fate-control.token"
DEFAULT_BOT_LOG = REPOSITORY_ROOT / "data" / "logging" / "fate-control.bot.log"
CONTROL_PANEL_ROOT = Path(__file__).resolve().parent / "control_panel"
CONTROL_PANEL_ROUTES = ("/control",)
CONTROL_SESSION_COOKIE = "fate_control_session"
CONTROL_SESSION_TTL_SECONDS = 12 * 60 * 60
CONTROL_LAUNCH_TICKET_TTL_SECONDS = 60
REDACTED = "__FATE_SECRET__"
SENSITIVE_PATHS = (
    ("token",),
    ("token_id",),
    ("top.gg",),
    ("mongodb", "url"),
    ("mysql", "password"),
    ("website", "discord_client_secret"),
    ("website", "session_secret"),
)
BIDI_CONTROL_CHARACTERS = frozenset(
    {"\u061c", "\u200e", "\u200f", *map(chr, range(0x202A, 0x202F)), *map(chr, range(0x2066, 0x206A))}
)
CONFIG_LOCK_TIMEOUT_SECONDS = 10.0
CONFIG_LOCK_POLL_SECONDS = 0.05
LOWERCASE_HEX_CHARACTERS = frozenset("0123456789abcdef")
MAX_CONSOLE_LINES = 500
MAX_CONSOLE_BYTES = 256 * 1024


class ConfigError(ValueError):
    """Raised when a proposed Fate configuration is unsafe or malformed."""


class ConfigConflictError(ConfigError):
    """The config changed after a caller loaded or while it prepared it."""


@dataclass(frozen=True)
class ConfigSnapshot:
    config: dict[str, Any]
    raw: bytes
    fingerprint: str


async def read_json_object(request: web.Request) -> dict[str, Any]:
    """Read a bounded request body and reject valid JSON scalars consistently."""
    try:
        payload = await request.json()
    except (UnicodeError, ValueError) as error:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "Expected a JSON object."}),
            content_type="application/json",
        ) from error
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "Expected a JSON object."}),
            content_type="application/json",
        )
    return payload


def _valid_revision(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in LOWERCASE_HEX_CHARACTERS for character in value)
    )


def _path_get(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = payload
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _path_set(payload: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    target = payload
    for key in path[:-1]:
        child = target.get(key)
        if not isinstance(child, dict):
            child = {}
            target[key] = child
        target = child
    target[path[-1]] = value


def redact_config(payload: dict[str, Any]) -> dict[str, Any]:
    redacted = copy.deepcopy(payload)
    for path in SENSITIVE_PATHS:
        if _path_get(redacted, path) not in (None, ""):
            _path_set(redacted, path, REDACTED)
    return redacted


def control_panel_config(payload: dict[str, Any]) -> tuple[dict[str, Any], list[list[str]]]:
    """Encode integers JavaScript cannot represent without losing precision."""
    encoded = copy.deepcopy(payload)
    unsafe_paths: list[list[str]] = []

    def visit(value: Any, path: list[str]) -> Any:
        if type(value) is int and abs(value) > 9_007_199_254_740_991:
            unsafe_paths.append(path)
            return str(value)
        if isinstance(value, dict):
            return {key: visit(child, [*path, key]) for key, child in value.items()}
        if isinstance(value, list):
            return [visit(child, [*path, str(index)]) for index, child in enumerate(value)]
        return value

    return visit(encoded, []), unsafe_paths


def restore_control_panel_integers(
    payload: Any, raw_paths: Any
) -> Any:
    if not isinstance(payload, dict) or raw_paths is None:
        return payload
    if not isinstance(raw_paths, list) or len(raw_paths) > 1_000:
        raise ConfigError("unsafe_integer_paths must be a bounded list")
    restored = copy.deepcopy(payload)
    for raw_path in raw_paths:
        if (
            not isinstance(raw_path, list)
            or not raw_path
            or any(not isinstance(part, str) or not part for part in raw_path)
        ):
            raise ConfigError("unsafe_integer_paths contains an invalid path")
        cursor: Any = restored
        try:
            for part in raw_path[:-1]:
                cursor = cursor[int(part)] if isinstance(cursor, list) else cursor[part]
            leaf = raw_path[-1]
            value = cursor[int(leaf)] if isinstance(cursor, list) else cursor[leaf]
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise ConfigError("unsafe_integer_paths does not match the configuration") from error
        if not isinstance(value, str) or not value.lstrip("-").isdigit():
            raise ConfigError("A protected large integer is not a valid whole number")
        converted = int(value)
        if isinstance(cursor, list):
            cursor[int(leaf)] = converted
        else:
            cursor[leaf] = converted
    return restored


def preserve_secrets(
    proposed: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    merged = copy.deepcopy(proposed)
    for path in SENSITIVE_PATHS:
        proposed_value = _path_get(merged, path)
        current_value = _path_get(current, path)
        if proposed_value in (None, REDACTED) and current_value is not None:
            _path_set(merged, path, current_value)
    return merged


def _contains_unsafe_display_character(value: str) -> bool:
    return any(
        unicodedata.category(character) in {"Cc", "Cs"}
        or character in BIDI_CONTROL_CHARACTERS
        for character in value
    )


def validate_backup_settings(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError("backups must be an object")
    if type(value.get("enabled", True)) is not bool:
        raise ConfigError("backups.enabled must be true or false")
    location = value.get("location", "./data/backups")
    if (
        not isinstance(location, str)
        or not location.strip()
        or len(location) > 1_024
        or _contains_unsafe_display_character(location)
    ):
        raise ConfigError("backups.location must be a valid non-empty path")
    frequency = value.get("frequency_hours", 12)
    if (
        isinstance(frequency, bool)
        or not isinstance(frequency, (int, float))
        or not 0.25 <= float(frequency) <= 720
    ):
        raise ConfigError("backups.frequency_hours must be between 0.25 and 720")
    retention = value.get("retention_days", 7)
    if retention is not None and (
        type(retention) is not int or not 1 <= retention <= 3_650
    ):
        raise ConfigError("backups.retention_days must be null or between 1 and 3650")
    maximum = value.get("max_backups")
    if maximum is not None and (
        type(maximum) is not int or not 1 <= maximum <= 100_000
    ):
        raise ConfigError("backups.max_backups must be null or between 1 and 100000")
    storage = value.get("max_storage_gb")
    if storage is not None and (
        isinstance(storage, bool)
        or not isinstance(storage, (int, float))
        or not 0.1 <= float(storage) <= 10_240
    ):
        raise ConfigError("backups.max_storage_gb must be null or between 0.1 and 10240")
    if type(value.get("include_local_files", True)) is not bool:
        raise ConfigError("backups.include_local_files must be true or false")
    for executable in ("mysqldump_path", "mongodump_path"):
        path = value.get(executable)
        if path is not None and (
            not isinstance(path, str)
            or not path.strip()
            or len(path) > 1_024
            or _contains_unsafe_display_character(path)
        ):
            raise ConfigError(f"backups.{executable} must be a valid executable path")

    drive = value.get("google_drive", {})
    if not isinstance(drive, dict):
        raise ConfigError("backups.google_drive must be an object")
    if type(drive.get("enabled", False)) is not bool:
        raise ConfigError("backups.google_drive.enabled must be true or false")
    for field, limit in (
        ("folder_id", 256),
        ("folder_path", 2_048),
        ("client_credentials_path", 1_024),
        ("token_path", 1_024),
    ):
        candidate = drive.get(field)
        if candidate is not None and (
            not isinstance(candidate, str)
            or not candidate.strip()
            or len(candidate) > limit
            or _contains_unsafe_display_character(candidate)
        ):
            raise ConfigError(f"backups.google_drive.{field} is invalid")
    redirect_uri = drive.get("redirect_uri")
    if redirect_uri is not None:
        if not isinstance(redirect_uri, str) or len(redirect_uri) > 2_048:
            raise ConfigError("backups.google_drive.redirect_uri must be a valid URL")
        parsed = urlsplit(redirect_uri)
        loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if not parsed.netloc or parsed.scheme not in {"http", "https"} or (
            parsed.scheme == "http" and not loopback
        ):
            raise ConfigError(
                "backups.google_drive.redirect_uri must use HTTPS (HTTP is allowed only on loopback)"
            )
    return value


def validate_config(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ConfigError("config.json must contain one JSON object")
    required = {
        "activity_status": str,
        "max_cached_messages": int,
        "datastore_location": str,
        "extensions": dict,
    }
    for key, expected in required.items():
        if key not in payload or not isinstance(payload[key], expected):
            raise ConfigError(f"{key} must be a {expected.__name__}")
    if _contains_unsafe_display_character(payload["activity_status"]):
        raise ConfigError(
            "activity_status cannot contain control or bidirectional formatting characters"
        )
    if not payload["activity_status"].strip() or len(payload["activity_status"]) > 128:
        raise ConfigError("activity_status must be between 1 and 128 characters")
    cached = payload["max_cached_messages"]
    if isinstance(cached, bool) or not 100 <= cached <= 500_000:
        raise ConfigError("max_cached_messages must be between 100 and 500000")
    if not payload["datastore_location"].strip():
        raise ConfigError("datastore_location cannot be empty")
    for category, extensions in payload["extensions"].items():
        if not isinstance(category, str) or not isinstance(extensions, list):
            raise ConfigError("extensions must map category names to lists")
        if any(not isinstance(item, str) or not item.strip() for item in extensions):
            raise ConfigError(f"extensions.{category} contains an invalid module name")

    local_databases = payload.get("local_databases", {})
    if not isinstance(local_databases, dict):
        raise ConfigError("local_databases must be an object")
    if type(local_databases.get("auto_start", True)) is not bool:
        raise ConfigError("local_databases.auto_start must be true or false")

    validate_backup_settings(payload.get("backups", {}))

    try:
        OrganismServiceSettings.from_mapping(
            payload.get("organism"),
            repository_root=REPOSITORY_ROOT,
        )
    except OrganismServiceConfigurationError as error:
        raise ConfigError(str(error)) from error

    mysql = payload.get("mysql", {})
    if not isinstance(mysql, dict):
        raise ConfigError("mysql must be an object")
    for key in ("host", "user", "db"):
        value = mysql.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ConfigError(f"mysql.{key} must be a non-empty string")
    mysql_port = mysql.get("port", 3306)
    if type(mysql_port) is not int or not 1 <= mysql_port <= 65_535:
        raise ConfigError("mysql.port must be between 1 and 65535")
    if type(mysql.get("autocommit", True)) is not bool:
        raise ConfigError("mysql.autocommit must be true or false")
    minimum_pool = mysql.get("min_pool_size", 1)
    maximum_pool = mysql.get("max_pool_size", 16)
    if (
        type(minimum_pool) is not int
        or type(maximum_pool) is not int
        or not 1 <= minimum_pool <= maximum_pool <= 128
    ):
        raise ConfigError(
            "mysql pool sizes must satisfy 1 <= min_pool_size <= max_pool_size <= 128"
        )

    translation = payload.get("translation", {})
    if not isinstance(translation, dict):
        raise ConfigError("translation must be an object")
    endpoint = translation.get("endpoint")
    if endpoint is not None:
        if not isinstance(endpoint, str) or len(endpoint) > 2_048:
            raise ConfigError("translation.endpoint must be a valid HTTP(S) URL")
        parsed_endpoint = urlsplit(endpoint)
        if parsed_endpoint.scheme not in {"http", "https"} or not parsed_endpoint.netloc:
            raise ConfigError("translation.endpoint must be a valid HTTP(S) URL")
    timeout = translation.get("timeout_seconds")
    if timeout is not None and (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 0.5 <= float(timeout) <= 8.0
    ):
        raise ConfigError("translation.timeout_seconds must be between 0.5 and 8")

    website = payload.get("website", {})
    if not isinstance(website, dict):
        raise ConfigError("website must be an object")
    website_host = website.get("host", "127.0.0.1")
    if not isinstance(website_host, str) or not website_host.strip():
        raise ConfigError("website.host must be a non-empty string")
    website_port = website.get("port", 16420)
    if type(website_port) is not int or not 1 <= website_port <= 65_535:
        raise ConfigError("website.port must be between 1 and 65535")
    for key in ("secure_cookies", "dev_mode"):
        if type(website.get(key, False)) is not bool:
            raise ConfigError(f"website.{key} must be true or false")
    trusted_proxies = website.get("trusted_proxies", [])
    if not isinstance(trusted_proxies, list) or any(
        not isinstance(value, str) for value in trusted_proxies
    ):
        raise ConfigError("website.trusted_proxies must be a list of IP networks")
    try:
        for value in trusted_proxies:
            ipaddress.ip_network(value.strip(), strict=False)
    except ValueError as error:
        raise ConfigError(
            "website.trusted_proxies contains an invalid IP network"
        ) from error

    control_panel = payload.get("control_panel", {})
    if not isinstance(control_panel, dict):
        raise ConfigError("control_panel must be an object")
    for key, default in (
        ("host", "0.0.0.0"),
        ("bot_status_host", "127.0.0.1"),
    ):
        value = control_panel.get(key, default)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"control_panel.{key} must be a non-empty string")
    instance_name = control_panel.get("instance_name", "")
    if (
        not isinstance(instance_name, str)
        or len(instance_name) > 100
        or _contains_unsafe_display_character(instance_name)
    ):
        raise ConfigError("control_panel.instance_name must be at most 100 safe characters")
    for key, default in (("port", 16421), ("bot_status_port", 16420)):
        value = control_panel.get(key, default)
        if type(value) is not int or not 1 <= value <= 65_535:
            raise ConfigError(f"control_panel.{key} must be between 1 and 65535")
    for key, default in (("autostart", True), ("allow_host_reboot", False)):
        if type(control_panel.get(key, default)) is not bool:
            raise ConfigError(f"control_panel.{key} must be true or false")
    allowed_ips = control_panel.get("allowed_ips", [])
    if not isinstance(allowed_ips, list) or any(
        not isinstance(value, str) for value in allowed_ips
    ):
        raise ConfigError("control_panel.allowed_ips must be a list of IP networks")
    try:
        for value in allowed_ips:
            ipaddress.ip_network(value.strip(), strict=False)
    except ValueError as error:
        raise ConfigError(
            "control_panel.allowed_ips contains an invalid IP network"
        ) from error
    return payload


def _config_lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


def _canonical_config_path(path: Path) -> Path:
    candidate = path if path.is_absolute() else Path.cwd() / path
    return candidate.parent.resolve() / candidate.name


@contextmanager
def _exclusive_config_lock(path: Path) -> Iterator[None]:
    """Lock the stable sibling lock file shared with the Dashboard writer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _config_lock_path(path)
    try:
        if lock_path.is_symlink():
            raise ConfigError("The config.json lock cannot be a symbolic link")
    except OSError as error:
        raise ConfigError(f"Unable to inspect config.json lock: {error}") from error
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise ConfigError(f"Unable to lock config.json: {error}") from error
    try:
        lock_stat = os.fstat(descriptor)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
            raise ConfigError("The config.json lock is not a regular file")
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)

        if os.name == "nt":
            import msvcrt

            deadline = monotonic() + CONFIG_LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if monotonic() >= deadline:
                        raise ConfigError(
                            "Timed out waiting for the config.json lock"
                        ) from error
                    sleep(CONFIG_LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            deadline = monotonic() + CONFIG_LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise
                    if monotonic() >= deadline:
                        raise ConfigError(
                            "Timed out waiting for the config.json lock"
                        ) from error
                    sleep(CONFIG_LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _read_regular_bytes(path: Path) -> bytes:
    try:
        path_stat = path.lstat()
        if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
            raise ConfigError("config.json must be a regular file")
        with path.open("rb") as config_file:
            opened_stat = os.fstat(config_file.fileno())
            if (
                opened_stat.st_dev != path_stat.st_dev
                or opened_stat.st_ino != path_stat.st_ino
            ):
                raise ConfigConflictError("config.json changed while it was opened")
            raw = config_file.read()
            final_stat = os.fstat(config_file.fileno())
            if (
                final_stat.st_size != opened_stat.st_size
                or final_stat.st_mtime_ns != opened_stat.st_mtime_ns
            ):
                raise ConfigConflictError("config.json changed while it was read")
            return raw
    except ConfigError:
        raise
    except OSError as error:
        raise ConfigError(f"Unable to read config.json: {error}") from error


def _read_config_snapshot_unlocked(path: Path) -> ConfigSnapshot:
    raw = _read_regular_bytes(path)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ConfigError(f"Unable to read config.json: {error}") from error
    return ConfigSnapshot(
        config=validate_config(payload),
        raw=raw,
        fingerprint=hashlib.sha256(raw).hexdigest(),
    )


def read_config_snapshot(path: Path = DEFAULT_CONFIG_PATH) -> ConfigSnapshot:
    path = _canonical_config_path(path)
    with _exclusive_config_lock(path):
        return _read_config_snapshot_unlocked(path)


def read_config(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    return read_config_snapshot(path).config


def _write_secure_temporary(path: Path, payload: bytes) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        return temporary
    except Exception:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_status_server_restart_request(path: Path) -> None:
    """Atomically signal the matching Fate process to recycle its HTTP listener."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps({"requested_at": datetime.now(timezone.utc).isoformat()}) + "\n"
    ).encode("utf-8")
    temporary = _write_secure_temporary(path, payload)
    try:
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_write_config_unlocked(
    path: Path,
    config: dict[str, Any],
    snapshot: ConfigSnapshot | None,
) -> str:
    try:
        serialized = (
            json.dumps(config, indent=4, ensure_ascii=False) + "\n"
        ).encode("utf-8")
    except (TypeError, UnicodeError, ValueError) as error:
        raise ConfigError(f"Unable to serialize config.json: {error}") from error
    config_temporary = _write_secure_temporary(path, serialized)
    backup_temporary: Path | None = None
    backup = path.with_suffix(path.suffix + ".bak")
    try:
        if snapshot is not None:
            backup_temporary = _write_secure_temporary(backup, snapshot.raw)
            current_fingerprint = hashlib.sha256(_read_regular_bytes(path)).hexdigest()
            if current_fingerprint != snapshot.fingerprint:
                raise ConfigConflictError(
                    "config.json changed while this update was being prepared"
                )
            os.replace(backup_temporary, backup)
            backup_temporary = None
            current_fingerprint = hashlib.sha256(_read_regular_bytes(path)).hexdigest()
            if current_fingerprint != snapshot.fingerprint:
                raise ConfigConflictError(
                    "config.json changed before this update could be committed"
                )
        elif path.exists():
            raise ConfigConflictError(
                "config.json appeared while this update was being prepared"
            )

        os.replace(config_temporary, path)
        config_temporary = None
        _fsync_directory(path.parent)
        return hashlib.sha256(serialized).hexdigest()
    except ConfigError:
        raise
    except OSError as error:
        raise ConfigError(f"Unable to write config.json: {error}") from error
    finally:
        for temporary in (config_temporary, backup_temporary):
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass


def write_config(
    path: Path,
    payload: dict[str, Any],
    *,
    expected_revision: str | None = None,
) -> str:
    validated = validate_config(payload)
    path = _canonical_config_path(path)
    with _exclusive_config_lock(path):
        snapshot = _read_config_snapshot_unlocked(path) if path.exists() else None
        if expected_revision is not None and (
            not _valid_revision(expected_revision)
            or snapshot is None
            or not hmac.compare_digest(snapshot.fingerprint, expected_revision)
        ):
            raise ConfigConflictError("config.json changed after it was loaded")
        return _atomic_write_config_unlocked(path, validated, snapshot)


def update_config(
    path: Path,
    proposed: Any,
    *,
    expected_revision: str,
) -> tuple[dict[str, Any], str, bool]:
    """Preserve secrets and commit a full-config edit in one locked transaction."""
    path = _canonical_config_path(path)
    with _exclusive_config_lock(path):
        snapshot = _read_config_snapshot_unlocked(path)
        if not _valid_revision(expected_revision) or not hmac.compare_digest(
            snapshot.fingerprint, expected_revision
        ):
            raise ConfigConflictError("config.json changed after it was loaded")
        merged = (
            preserve_secrets(proposed, snapshot.config)
            if isinstance(proposed, dict)
            else proposed
        )
        validated = validate_config(merged)
        controller_restart_required = snapshot.config.get(
            "control_panel"
        ) != validated.get("control_panel")
        revision = _atomic_write_config_unlocked(path, validated, snapshot)
        return validated, revision, controller_restart_required


BACKUP_EDITABLE_FIELDS = (
    "enabled",
    "location",
    "frequency_hours",
    "retention_days",
    "max_storage_gb",
    "max_backups",
    "include_local_files",
)


def backup_settings_payload(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("backups", {})
    if not isinstance(raw, dict):
        raw = {}
    drive = raw.get("google_drive", {})
    if not isinstance(drive, dict):
        drive = {}
    return {
        "enabled": raw.get("enabled", True),
        "location": raw.get("location", "./data/backups"),
        "frequency_hours": raw.get("frequency_hours", 12),
        "retention_days": raw.get("retention_days", 7),
        "max_storage_gb": raw.get("max_storage_gb"),
        "max_backups": raw.get("max_backups"),
        "include_local_files": raw.get("include_local_files", True),
        "google_drive": {
            "enabled": drive.get("enabled", False),
            "folder_id": drive.get("folder_id"),
            "folder_path": drive.get("folder_path"),
        },
    }


def update_backup_settings(
    path: Path,
    proposed: dict[str, Any],
    *,
    expected_revision: str | None = None,
    folder_id: str | None = None,
    folder_path: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Commit the backup subsection while preserving advanced/server-owned fields."""
    if not isinstance(proposed, dict):
        raise ConfigError("settings must be an object")
    if expected_revision is not None and not _valid_revision(expected_revision):
        raise ConfigConflictError("Backup settings changed after they were loaded")
    for _attempt in range(3):
        snapshot = read_config_snapshot(path)
        if expected_revision is not None and not hmac.compare_digest(
            snapshot.fingerprint, expected_revision
        ):
            raise ConfigConflictError("Backup settings changed after they were loaded")
        config = copy.deepcopy(snapshot.config)
        current = config.get("backups", {})
        if not isinstance(current, dict):
            current = {}
        updated = copy.deepcopy(current)
        for key in BACKUP_EDITABLE_FIELDS:
            if key in proposed:
                updated[key] = proposed[key]
        current_drive = updated.get("google_drive", {})
        if not isinstance(current_drive, dict):
            current_drive = {}
        drive = copy.deepcopy(current_drive)
        proposed_drive = proposed.get("google_drive", {})
        if proposed_drive is not None and not isinstance(proposed_drive, dict):
            raise ConfigError("settings.google_drive must be an object")
        if isinstance(proposed_drive, dict) and "enabled" in proposed_drive:
            drive["enabled"] = proposed_drive["enabled"]
        if folder_id is not None:
            drive["folder_id"] = folder_id
            drive["folder_path"] = folder_path
            drive["enabled"] = True
        updated["google_drive"] = drive
        validate_backup_settings(updated)
        config["backups"] = updated
        try:
            saved, revision, _restart = update_config(
                path, config, expected_revision=snapshot.fingerprint
            )
            return backup_settings_payload(saved), revision
        except ConfigConflictError:
            if expected_revision is not None:
                raise
            continue
    raise ConfigConflictError("Fate's config changed while backup settings were saved")


def ensure_control_token(path: Path = DEFAULT_TOKEN_PATH) -> tuple[str, bool]:
    configured = os.getenv("FATE_CONTROL_TOKEN", "").strip()
    if configured:
        return configured, False
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 24:
            return token, False
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    return token, True


class FateController:
    def __init__(
        self,
        *,
        config_path: Path = DEFAULT_CONFIG_PATH,
        token_path: Path = DEFAULT_TOKEN_PATH,
        bot_log_path: Path = DEFAULT_BOT_LOG,
        metrics_path: Path | None = None,
        autostart: bool | None = None,
    ) -> None:
        self.config_path = _canonical_config_path(config_path)
        self.token_path = token_path
        self.bot_log_path = bot_log_path
        self.token, self.token_created = ensure_control_token(token_path)
        parsed_config = None
        try:
            candidate_config = json.loads(self.config_path.read_text(encoding="utf-8"))
            if isinstance(candidate_config, dict):
                parsed_config = candidate_config
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
        control_settings = (
            parsed_config.get("control_panel", {})
            if isinstance(parsed_config, dict)
            else {}
        )
        if not isinstance(control_settings, dict):
            control_settings = {}

        configured_allowed_ips = control_settings.get("allowed_ips", [])
        self.allowed_ip_networks = tuple(
            ipaddress.ip_network(value.strip(), strict=False)
            for value in configured_allowed_ips
            if isinstance(value, str) and value.strip()
        )

        configured_autostart = control_settings.get("autostart", True) is True
        self.autostart = configured_autostart if autostart is None else autostart
        self.desired_running = self.autostart
        self.process: subprocess.Popen | None = None
        self.process_log = None
        self.http: aiohttp.ClientSession | None = None
        self.drive_http: aiohttp.ClientSession | None = None
        self.drive_oauth_states: dict[str, float] = {}
        self.control_sessions: dict[str, float] = {}
        self.control_launch_tickets: dict[str, float] = {}
        self.monitor_task: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.started = monotonic()
        self.restart_count = 0
        self.last_exit_code: int | None = None
        self.last_error: str | None = None
        configured_instance_name = control_settings.get("instance_name")
        if not isinstance(configured_instance_name, str) or not configured_instance_name.strip():
            configured_instance_name = socket.gethostname()
        self.instance_name = os.getenv("FATE_INSTANCE_NAME", configured_instance_name)
        configured_bot_host = control_settings.get("bot_status_host", "127.0.0.1")
        if not isinstance(configured_bot_host, str) or not configured_bot_host.strip():
            configured_bot_host = "127.0.0.1"
        self.bot_host = os.getenv("FATE_BOT_STATUS_HOST", configured_bot_host)
        configured_bot_port = control_settings.get("bot_status_port", 16420)
        if type(configured_bot_port) is not int:
            configured_bot_port = 16420
        self.bot_port = int(os.getenv("DASHBOARD_PORT", str(configured_bot_port)))
        self._storage_cache: dict[str, Any] | None = None
        self._storage_cache_at = 0.0
        reboot_environment = os.getenv("FATE_ALLOW_HOST_REBOOT")
        self.allow_host_reboot = (
            reboot_environment.strip().lower() in {"1", "true", "yes", "on"}
            if reboot_environment is not None
            else control_settings.get("allow_host_reboot", False) is True
        )
        self._system_sample_lock = threading.Lock()
        self._network_sample: tuple[float, int, int] | None = None
        self._disk_io_sample: tuple[float, int, int] | None = None
        telemetry_config = parsed_config
        try:
            self.config_path.relative_to(REPOSITORY_ROOT)
            telemetry_root = REPOSITORY_ROOT
        except ValueError:
            telemetry_root = self.config_path.parent
        resolved_metrics_path = metrics_path or telemetry_path_for_config(
            self.config_path,
            telemetry_config,
            repository_root=telemetry_root,
        )
        self.metrics_store = TelemetryStore(resolved_metrics_path)
        self._explicit_metrics_path = metrics_path is not None
        self._metrics_stores = {self.config_path.name: self.metrics_store}
        self._active_metrics_store = self.metrics_store
        self._metrics_profile_checked_at = 0.0
        self._metrics_profile_lock = asyncio.Lock()

    async def startup(self, _app: web.Application) -> None:
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2.5))
        self.drive_http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=45, sock_connect=15, sock_read=45)
        )
        # Prime the relatively expensive project-size breakdown before the LAN
        # listener begins accepting discovery probes.
        await asyncio.to_thread(self.storage_details)
        if self.autostart:
            await self.start_bot()
        self.monitor_task = asyncio.create_task(self.monitor(), name="fate-control-monitor")

    async def cleanup(self, _app: web.Application) -> None:
        self.desired_running = False
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
        if self.owns_running_process():
            await self.stop_bot(update_desired=False)
        if self.http:
            await self.http.close()
            self.http = None
        if self.drive_http:
            await self.drive_http.close()
            self.drive_http = None

    def owns_running_process(self) -> bool:
        return self.process is not None and self.process.poll() is None

    async def bot_status(self) -> dict[str, Any] | None:
        if self.http is None:
            return None
        url = f"http://{self.bot_host}:{self.bot_port}/status"
        try:
            async with self.http.get(url) as response:
                if response.status != 200:
                    return None
                payload = await response.json()
                if isinstance(payload, dict):
                    self.metrics_store_for_status(payload)
                    self._metrics_profile_checked_at = monotonic()
                    return payload
                return None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None

    def metrics_store_for_status(
        self, status: dict[str, Any] | None
    ) -> TelemetryStore:
        """Select the supervised bot profile's store without trusting a path."""
        if self._explicit_metrics_path:
            return self._active_metrics_store
        if not isinstance(status, dict):
            return self._active_metrics_store
        instance = status.get("instance")
        config_name = instance.get("config") if isinstance(instance, dict) else None
        if (
            not isinstance(config_name, str)
            or Path(config_name).name != config_name
            or not config_name.endswith(".json")
        ):
            return self._active_metrics_store
        cached = self._metrics_stores.get(config_name)
        if cached is not None:
            self._active_metrics_store = cached
            return cached
        candidate = self.config_path.with_name(config_name)
        try:
            if not candidate.is_file() or candidate.is_symlink():
                return self._active_metrics_store
            config = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return self._active_metrics_store
        if not isinstance(config, dict):
            return self._active_metrics_store
        store = TelemetryStore(
            telemetry_path_for_config(
                candidate,
                config,
                repository_root=REPOSITORY_ROOT,
            )
        )
        self._metrics_stores[config_name] = store
        self._active_metrics_store = store
        return store

    async def active_metrics_store(self) -> TelemetryStore:
        """Refresh the bot-profile selection at most once per five seconds."""
        if monotonic() - self._metrics_profile_checked_at < 5:
            return self._active_metrics_store
        async with self._metrics_profile_lock:
            if monotonic() - self._metrics_profile_checked_at < 5:
                return self._active_metrics_store
            status = await self.bot_status()
            self._metrics_profile_checked_at = monotonic()
            return self.metrics_store_for_status(status)

    async def start_bot(self) -> dict[str, Any]:
        async with self.lock:
            self.desired_running = True
            if self.owns_running_process():
                return {"changed": False, "message": "Fate is already running."}
            external = await self.bot_status()
            external_process = await asyncio.to_thread(self._bot_process)
            if external or external_process is not None:
                return {
                    "changed": False,
                    "external": True,
                    "message": "A Fate process is already running or starting outside this controller.",
                }
            self.bot_log_path.parent.mkdir(parents=True, exist_ok=True)
            self.process_log = self.bot_log_path.open("a", encoding="utf-8")
            self.process_log.write(
                f"\n[{datetime.now(timezone.utc).isoformat()}] controller starting Fate\n"
            )
            self.process_log.flush()
            command = [sys.executable, str(REPOSITORY_ROOT / "fate.py")]
            kwargs: dict[str, Any] = {
                "cwd": str(REPOSITORY_ROOT),
                "stdout": self.process_log,
                "stderr": subprocess.STDOUT,
            }
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            try:
                # The executable and script path are fixed by this controller;
                # no request data reaches the process argument list.
                self.process = subprocess.Popen(command, **kwargs)  # noqa: S603
            except OSError as error:
                self.last_error = str(error)
                self.process_log.close()
                self.process_log = None
                raise web.HTTPServiceUnavailable(
                    text=json.dumps({"error": f"Could not start Fate: {error}"}),
                    content_type="application/json",
                ) from error
            self.last_error = None
            return {"changed": True, "message": "Fate is starting."}

    async def stop_bot(self, *, update_desired: bool = True) -> dict[str, Any]:
        async with self.lock:
            if update_desired:
                self.desired_running = False
            if not self.owns_running_process():
                external = await self.bot_status()
                if external:
                    raise web.HTTPConflict(
                        text=json.dumps(
                            {
                                "error": "Fate is running outside the controller. Stop that process once, then launch it through FateControl."
                            }
                        ),
                        content_type="application/json",
                    )
                return {"changed": False, "message": "Fate is already stopped."}
            if self.process is None:
                raise web.HTTPConflict(
                    text=json.dumps({"error": "Fate stopped before it could be managed."}),
                    content_type="application/json",
                )
            self.process.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(self.process.wait), timeout=15)
            except asyncio.TimeoutError:
                self.process.kill()
                await asyncio.to_thread(self.process.wait)
            self.last_exit_code = self.process.returncode
            self.process = None
            if self.process_log:
                self.process_log.close()
                self.process_log = None
            return {"changed": True, "message": "Fate is stopped."}

    def _terminate_external_fate(self, process: psutil.Process) -> int | None:
        """Stop only a verified Fate process from this checkout."""
        if not self._is_fate_process(process):
            raise RuntimeError(
                "The external process changed before FateControl could restart it."
            )
        pid = process.pid
        try:
            process.terminate()
            return process.wait(timeout=15)
        except psutil.NoSuchProcess:
            return None
        except psutil.TimeoutExpired:
            try:
                process.kill()
                return process.wait(timeout=5)
            except psutil.NoSuchProcess:
                return None
        except (psutil.AccessDenied, OSError) as error:
            raise RuntimeError(
                f"FateControl could not stop the external Fate process {pid}: {error}"
            ) from error

    async def restart_bot(self) -> dict[str, Any]:
        if not self.owns_running_process():
            external_process = await asyncio.to_thread(self._bot_process)
            if external_process is not None:
                self.desired_running = False
                LOGGER.warning(
                    "Restarting externally launched Fate process %s and transferring "
                    "supervision to FateControl",
                    external_process.pid,
                )
                try:
                    self.last_exit_code = await asyncio.to_thread(
                        self._terminate_external_fate, external_process
                    )
                except RuntimeError as error:
                    self.desired_running = True
                    raise web.HTTPConflict(
                        text=json.dumps({"error": str(error)}),
                        content_type="application/json",
                    ) from error
                self.desired_running = True
                result = await self.start_bot()
                if result.get("external"):
                    raise web.HTTPConflict(
                        text=json.dumps(
                            {
                                "error": "The external Fate process is still shutting down. Try restart again shortly."
                            }
                        ),
                        content_type="application/json",
                    )
                return {
                    **result,
                    "adopted_external": True,
                    "message": "Fate is restarting under FateControl.",
                }
        try:
            await self.stop_bot()
        except web.HTTPConflict:
            raise
        self.desired_running = True
        return await self.start_bot()

    def status_server_restart_path(self) -> Path:
        return REPOSITORY_ROOT / "data" / f"fate-status-server-{self.bot_port}.restart"

    async def restart_status_server(self) -> dict[str, Any]:
        status = await self.bot_status()
        capabilities = status.get("capabilities", {}) if isinstance(status, dict) else {}
        if status is not None and not (
            isinstance(capabilities, dict)
            and capabilities.get("status_server_restart") is True
        ):
            raise web.HTTPConflict(
                text=json.dumps(
                    {
                        "error": "Restart Fate once to activate independent status-server restarts."
                    }
                ),
                content_type="application/json",
            )
        detected_process = await asyncio.to_thread(self._bot_process)
        if detected_process is None and not self.owns_running_process():
            raise web.HTTPConflict(
                text=json.dumps({"error": "Fate is not running."}),
                content_type="application/json",
            )
        request_path = self.status_server_restart_path()
        try:
            await asyncio.to_thread(write_status_server_restart_request, request_path)
        except OSError as error:
            raise web.HTTPServiceUnavailable(
                text=json.dumps(
                    {"error": f"Could not request a status-server restart: {error}"}
                ),
                content_type="application/json",
            ) from error
        for _attempt in range(32):
            await asyncio.sleep(0.25)
            if not request_path.exists():
                return {
                    "accepted": True,
                    "action": "restart_status_server",
                    "message": "Fate Status server restarted.",
                }
        raise web.HTTPGatewayTimeout(
            text=json.dumps(
                {"error": "Fate did not acknowledge the status-server restart request."}
            ),
            content_type="application/json",
        )

    async def monitor(self) -> None:
        monitor_failure: str | None = None
        while True:
            await asyncio.sleep(2)
            try:
                await self._monitor_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                monitor_failure = str(error)
                self.last_error = monitor_failure
                LOGGER.exception("FateControl monitor iteration failed; retrying")
            else:
                if monitor_failure is not None and self.last_error == monitor_failure:
                    self.last_error = None
                monitor_failure = None

    async def _monitor_once(self) -> None:
        """Reconcile the requested bot state with the process we can observe.

        Fate may already be running when this controller starts. In that case
        ``start_bot`` deliberately leaves the external process alone, but the
        controller still needs to take over if that process later disappears.
        Previously the monitor only watched children it had spawned, leaving
        the Start/Stop service alive but permanently unable to recover Fate.
        """
        if self.process is not None:
            if self.process.poll() is None:
                return
            self.last_exit_code = self.process.returncode
            self.process = None
            if self.process_log:
                self.process_log.close()
                self.process_log = None

        if not self.desired_running or await self.bot_status() is not None:
            return

        self.restart_count += 1
        await asyncio.sleep(min(30, 2 ** min(self.restart_count, 4)))
        # A stop request may arrive during the backoff. Do not let the monitor
        # undo an intentional stop by calling start_bot, which sets the desired
        # state back to running.
        if not self.desired_running:
            return
        try:
            await self.start_bot()
        except web.HTTPException as error:
            self.last_error = error.text

    def _prune_control_sessions(self) -> None:
        now = time()
        self.control_sessions = {
            session: expires
            for session, expires in self.control_sessions.items()
            if expires > now
        }
        self.control_launch_tickets = {
            ticket: expires
            for ticket, expires in self.control_launch_tickets.items()
            if expires > now
        }

    def _new_control_session(self) -> str:
        self._prune_control_sessions()
        expires = int(time() + CONTROL_SESSION_TTL_SECONDS)
        nonce = secrets.token_urlsafe(24)
        unsigned = f"v1.{expires}.{nonce}"
        signature = hmac.new(
            self.token.encode("utf-8"),
            unsigned.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        session = f"{unsigned}.{signature}"
        self.control_sessions[session] = float(expires)
        return session

    def _signed_control_session_expiry(self, session: str) -> float | None:
        parts = session.split(".")
        if len(parts) != 4 or parts[0] != "v1":
            return None
        version, expires_text, nonce, supplied_signature = parts
        if not expires_text.isdigit() or not nonce or len(nonce) > 64:
            return None
        unsigned = f"{version}.{expires_text}.{nonce}"
        expected_signature = hmac.new(
            self.token.encode("utf-8"),
            unsigned.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        if not secrets.compare_digest(supplied_signature, expected_signature):
            return None
        return float(expires_text)

    def _new_control_launch_ticket(self) -> str:
        self._prune_control_sessions()
        ticket = secrets.token_urlsafe(24)
        self.control_launch_tickets[ticket] = time() + CONTROL_LAUNCH_TICKET_TTL_SECONDS
        return ticket

    def _consume_control_launch_ticket(self, ticket: str) -> bool:
        self._prune_control_sessions()
        expires = self.control_launch_tickets.pop(ticket, None)
        return expires is not None and expires > time()

    def _valid_control_session(self, request: web.Request) -> bool:
        cookies = getattr(request, "cookies", {})
        supplied = cookies.get(CONTROL_SESSION_COOKIE, "")
        if not supplied:
            return False
        self._prune_control_sessions()
        expires = self.control_sessions.get(supplied)
        if expires is None:
            expires = self._signed_control_session_expiry(supplied)
        if expires is None or expires <= time():
            self.control_sessions.pop(supplied, None)
            return False
        method = getattr(request, "method", "GET").upper()
        if method not in {"GET", "HEAD", "OPTIONS"}:
            headers = getattr(request, "headers", {})
            origin = headers.get("Origin")
            host = headers.get("Host")
            if origin and host:
                expected_origins = {f"http://{host}", f"https://{host}"}
                if origin not in expected_origins:
                    raise web.HTTPForbidden(
                        text=json.dumps({"error": "The control-panel request origin was rejected."}),
                        content_type="application/json",
                    )
        return True

    def require_auth(self, request: web.Request) -> None:
        supplied = request.headers.get("Authorization", "")
        expected = f"Bearer {self.token}"
        if secrets.compare_digest(supplied, expected) or self._valid_control_session(request):
            return
        if not secrets.compare_digest(supplied, expected):
            raise web.HTTPUnauthorized(
                text=json.dumps({"error": "A valid Fate control key is required."}),
                content_type="application/json",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def client_ip_allowed(self, value: str | None) -> bool:
        if not self.allowed_ip_networks:
            return True
        try:
            address = ipaddress.ip_address((value or "").split("%", 1)[0])
        except ValueError:
            return False
        return any(address in network for network in self.allowed_ip_networks)

    @staticmethod
    def _set_control_session_cookie(
        response: web.StreamResponse, request: web.Request, session: str
    ) -> None:
        response.set_cookie(
            CONTROL_SESSION_COOKIE,
            session,
            max_age=CONTROL_SESSION_TTL_SECONDS,
            httponly=True,
            secure=getattr(request, "secure", False),
            samesite="Strict",
            path="/",
        )

    async def control_panel_page(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(CONTROL_PANEL_ROOT / "index.html")

    async def control_manifest(self, request: web.Request) -> web.Response:
        self.require_auth(request)
        return web.json_response(
            {
                "instance_name": self.instance_name,
                "metrics": sorted(SUPPORTED_METRICS),
                "windows": list(WINDOWS),
                "metric_windows": {
                    name: {
                        "duration_seconds": values[0],
                        "default_bucket_seconds": values[1],
                    }
                    for name, values in WINDOWS.items()
                },
                "max_chart_points": MAX_CHART_POINTS,
                "redaction_marker": REDACTED,
                "refresh_seconds": 15,
            }
        )

    async def create_control_session(self, request: web.Request) -> web.Response:
        self.require_auth(request)
        ticket = self._new_control_launch_ticket()
        return web.json_response(
            {
                "launch_url": f"/control#ticket={ticket}",
                "expires_in": CONTROL_LAUNCH_TICKET_TTL_SECONDS,
            }
        )

    async def exchange_control_session(self, request: web.Request) -> web.Response:
        payload = await read_json_object(request)
        ticket = payload.get("ticket")
        if not isinstance(ticket, str) or not self._consume_control_launch_ticket(ticket):
            raise web.HTTPUnauthorized(
                text=json.dumps({"error": "This control-panel launch link expired."}),
                content_type="application/json",
            )
        session = self._new_control_session()
        response = web.json_response({"ok": True, "instance_name": self.instance_name})
        self._set_control_session_cookie(response, request, session)
        return response

    async def login_control_session(self, request: web.Request) -> web.Response:
        payload = await read_json_object(request)
        supplied = payload.get("control_key")
        if not isinstance(supplied, str) or not secrets.compare_digest(
            supplied.strip(), self.token
        ):
            raise web.HTTPUnauthorized(
                text=json.dumps({"error": "The Fate control key is not valid."}),
                content_type="application/json",
            )
        session = self._new_control_session()
        response = web.json_response({"ok": True, "instance_name": self.instance_name})
        self._set_control_session_cookie(response, request, session)
        return response

    async def logout_control_session(self, request: web.Request) -> web.Response:
        cookies = getattr(request, "cookies", {})
        session = cookies.get(CONTROL_SESSION_COOKIE, "")
        if session:
            self.control_sessions.pop(session, None)
        response = web.json_response({"ok": True})
        response.del_cookie(CONTROL_SESSION_COOKIE, path="/")
        return response

    def health_details(self) -> dict[str, Any]:
        config_state = "ok"
        config_error = None
        try:
            read_config(self.config_path)
        except ConfigError as error:
            config_state = "error"
            config_error = str(error)
        usage = shutil.disk_usage(REPOSITORY_ROOT)
        free_gb = round(usage.free / (1024 ** 3), 1)
        recent_errors = 0
        if self.bot_log_path.exists():
            try:
                lines = self.console_tail(MAX_CONSOLE_LINES)["text"].splitlines()
                recent_errors = sum(
                    "error" in line.lower() or "critical" in line.lower()
                    for line in lines
                )
            except web.HTTPException:
                pass
        disk_state = "ok" if free_gb >= 2 else "warning"
        summary = "Healthy"
        if config_state == "error":
            summary = "Configuration needs attention"
        elif disk_state == "warning" or recent_errors:
            summary = "Attention recommended"
        return {
            "summary": summary,
            "config": config_state,
            "config_error": config_error,
            "disk": disk_state,
            "disk_free_gb": free_gb,
            "recent_log_errors": recent_errors,
        }

    def console_tail(self, requested_lines: int = 250) -> dict[str, Any]:
        """Return a bounded UTF-8 tail of Fate's combined stdout and stderr."""
        line_limit = max(1, min(requested_lines, MAX_CONSOLE_LINES))
        if not self.bot_log_path.exists():
            return {
                "text": "",
                "line_count": 0,
                "truncated": False,
                "updated_at": None,
            }
        try:
            size = self.bot_log_path.stat().st_size
            with self.bot_log_path.open("rb") as stream:
                offset = max(0, size - MAX_CONSOLE_BYTES)
                stream.seek(offset)
                raw = stream.read(MAX_CONSOLE_BYTES)
            decoded = raw.decode("utf-8", errors="replace")
            if offset > 0 and "\n" in decoded:
                decoded = decoded.split("\n", 1)[1]
            lines = decoded.splitlines()
            truncated = size > len(raw) or len(lines) > line_limit
            selected = lines[-line_limit:]
            updated_at = datetime.fromtimestamp(
                self.bot_log_path.stat().st_mtime,
                timezone.utc,
            ).isoformat()
        except OSError as error:
            raise web.HTTPServiceUnavailable(
                text=json.dumps({"error": f"Could not read Fate's console: {error}"}),
                content_type="application/json",
            ) from error
        return {
            "text": "\n".join(selected),
            "line_count": len(selected),
            "truncated": truncated,
            "updated_at": updated_at,
        }

    @staticmethod
    def _path_size(path: Path) -> int:
        """Return a symlink-safe recursive byte count for one project path."""
        try:
            if path.is_symlink():
                return 0
            if path.is_file():
                return path.stat().st_size
        except OSError:
            return 0
        total = 0
        try:
            for root, directories, files in os.walk(path, followlinks=False):
                root_path = Path(root)
                directories[:] = [
                    name for name in directories if not (root_path / name).is_symlink()
                ]
                for name in files:
                    candidate = root_path / name
                    try:
                        if not candidate.is_symlink():
                            total += candidate.stat().st_size
                    except OSError:
                        continue
        except OSError:
            return total
        return total

    @staticmethod
    def _process_tree_rss(process: psutil.Process | None) -> int:
        if process is None:
            return 0
        total = 0
        try:
            processes = [process, *process.children(recursive=True)]
        except (psutil.Error, OSError):
            processes = [process]
        for item in processes:
            try:
                total += item.memory_info().rss
            except (psutil.Error, OSError):
                continue
        return total

    @staticmethod
    def _is_fate_process(process: psutil.Process) -> bool:
        """Return whether a process command line launches this checkout's bot."""
        try:
            command = process.cmdline()
            working_directory = Path(process.cwd()).resolve()
        except (psutil.Error, OSError):
            return False
        expected = (REPOSITORY_ROOT / "fate.py").resolve()
        for argument in command[1:]:
            try:
                candidate = Path(argument)
                if not candidate.is_absolute():
                    candidate = working_directory / candidate
                if candidate.resolve() == expected:
                    return True
            except (OSError, RuntimeError):
                continue
        return False

    def _bot_process(self) -> psutil.Process | None:
        """Find Fate whether it was launched by this controller or externally."""
        if self.process is not None and self.process.poll() is None:
            try:
                return psutil.Process(self.process.pid)
            except (psutil.Error, OSError):
                pass
        candidates = []
        for process in psutil.process_iter():
            if process.pid != os.getpid() and self._is_fate_process(process):
                candidates.append(process)
        if not candidates:
            return None
        candidate_ids = {process.pid for process in candidates}
        roots = []
        for process in candidates:
            try:
                if process.ppid() not in candidate_ids:
                    roots.append(process)
            except (psutil.Error, OSError):
                continue
        choices = roots or candidates
        return max(choices, key=self._process_tree_rss)

    @staticmethod
    def _process_tree_memory(process: psutil.Process | None) -> tuple[int, list[dict[str, Any]]]:
        if process is None:
            return 0, []
        try:
            processes = [process, *process.children(recursive=True)]
        except (psutil.Error, OSError):
            processes = [process]
        measured = []
        for item in processes:
            try:
                measured.append((item, item.memory_info().rss))
            except (psutil.Error, OSError):
                continue
        if not measured:
            return 0, []
        runtime_pid = max(measured, key=lambda row: row[1])[0].pid
        categories: dict[str, int] = {}
        for item, size in measured:
            if item.pid == runtime_pid:
                label = "Fate runtime"
            else:
                try:
                    name = Path(item.name()).stem
                except (psutil.Error, OSError):
                    name = "Child process"
                label = "Python helper" if name.casefold().startswith("python") else name
            categories[label] = categories.get(label, 0) + max(0, size)
        breakdown = [
            {"label": label, "bytes": size}
            for label, size in sorted(
                categories.items(), key=lambda item: item[1], reverse=True
            )
            if size > 0
        ]
        return sum(item["bytes"] for item in breakdown), breakdown

    def memory_details(
        self,
        bot_process: psutil.Process | None = None,
        *,
        scan_for_bot: bool = True,
    ) -> dict[str, Any]:
        memory = psutil.virtual_memory()
        try:
            # Controller children include Fate, so report only this process here.
            controller_bytes = psutil.Process(os.getpid()).memory_info().rss
        except (psutil.Error, OSError):
            controller_bytes = 0
        if bot_process is None and scan_for_bot:
            bot_process = self._bot_process()
        bot_bytes, bot_breakdown = self._process_tree_memory(bot_process)
        used_bytes = max(0, memory.total - memory.available)
        other_bytes = max(0, used_bytes - controller_bytes - bot_bytes)
        return {
            "total_bytes": memory.total,
            "used_bytes": used_bytes,
            "available_bytes": memory.available,
            "used_percent": round(memory.percent, 1),
            "bot_total_bytes": bot_bytes,
            "bot_breakdown": bot_breakdown,
            "breakdown": [
                {"label": "Fate bot", "bytes": bot_bytes},
                {"label": "FateControl", "bytes": controller_bytes},
                {"label": "Other system use", "bytes": other_bytes},
                {"label": "Available", "bytes": memory.available},
            ],
        }

    def storage_details(self) -> dict[str, Any]:
        now = monotonic()
        if self._storage_cache is not None and now - self._storage_cache_at < 60:
            return self._storage_cache

        category_names = {
            "data": "Fate data & logs",
            "assets": "Artwork & assets",
            "apps": "App projects & builds",
            "venv": "Python environment",
            ".git": "Git history",
        }
        category_sizes = {label: 0 for label in category_names.values()}
        other_fate = 0
        try:
            children = list(REPOSITORY_ROOT.iterdir())
        except OSError:
            children = []
        for child in children:
            size = self._path_size(child)
            label = category_names.get(child.name)
            if label is None:
                other_fate += size
            else:
                category_sizes[label] += size

        fate_total = sum(category_sizes.values()) + other_fate
        disk = psutil.disk_usage(str(REPOSITORY_ROOT))
        other_drive = max(0, disk.used - fate_total)
        breakdown = [
            {"label": label, "bytes": size}
            for label, size in category_sizes.items()
        ]
        breakdown.extend(
            [
                {"label": "Bot source & other", "bytes": other_fate},
                {"label": "Other drive use", "bytes": other_drive},
                {"label": "Free space", "bytes": disk.free},
            ]
        )
        self._storage_cache = {
            "total_bytes": disk.total,
            "used_bytes": disk.used,
            "free_bytes": disk.free,
            "used_percent": round(disk.percent, 1),
            "fate_total_bytes": fate_total,
            "breakdown": breakdown,
        }
        self._storage_cache_at = now
        return self._storage_cache

    def resource_details(
        self,
        bot_process: psutil.Process | None = None,
        *,
        scan_for_bot: bool = True,
    ) -> dict[str, Any]:
        return {
            "memory": self.memory_details(bot_process, scan_for_bot=scan_for_bot),
            "storage": self.storage_details(),
            "sampled_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _temperature_details() -> dict[str, Any] | None:
        sensors = getattr(psutil, "sensors_temperatures", None)
        if sensors is None:
            return None
        try:
            readings = sensors(fahrenheit=False)
        except (AttributeError, OSError, RuntimeError):
            return None
        candidates = []
        for group, values in readings.items():
            for value in values:
                current = getattr(value, "current", None)
                if isinstance(current, (int, float)) and -20 <= current <= 150:
                    candidates.append(
                        {
                            "label": getattr(value, "label", "") or group,
                            "celsius": round(float(current), 1),
                        }
                    )
        return max(candidates, key=lambda value: value["celsius"]) if candidates else None

    def system_details(self) -> dict[str, Any]:
        """Return authenticated host telemetry without exposing command lines or secrets."""
        cpu_frequency = psutil.cpu_freq()
        network = psutil.net_io_counters()
        disk_io = psutil.disk_io_counters()
        now = monotonic()
        receive_rate = 0.0
        send_rate = 0.0
        disk_read_rate = 0.0
        disk_write_rate = 0.0
        with self._system_sample_lock:
            previous = self._network_sample
            if previous is not None:
                elapsed = max(0.001, now - previous[0])
                receive_rate = max(0.0, (network.bytes_recv - previous[1]) / elapsed)
                send_rate = max(0.0, (network.bytes_sent - previous[2]) / elapsed)
            self._network_sample = (now, network.bytes_recv, network.bytes_sent)
            previous_disk = self._disk_io_sample
            if disk_io is not None:
                if previous_disk is not None:
                    elapsed = max(0.001, now - previous_disk[0])
                    disk_read_rate = max(
                        0.0,
                        (disk_io.read_bytes - previous_disk[1]) / elapsed,
                    )
                    disk_write_rate = max(
                        0.0,
                        (disk_io.write_bytes - previous_disk[2]) / elapsed,
                    )
                self._disk_io_sample = (now, disk_io.read_bytes, disk_io.write_bytes)
            else:
                self._disk_io_sample = None

        try:
            load_average = [round(value, 2) for value in psutil.getloadavg()]
        except (AttributeError, OSError):
            load_average = []

        addresses = set()
        try:
            for interface_addresses in psutil.net_if_addrs().values():
                for address in interface_addresses:
                    if address.family == socket.AF_INET and not address.address.startswith("127."):
                        addresses.add(address.address)
        except (psutil.Error, OSError):
            pass

        return {
            "hostname": socket.gethostname(),
            "os_name": platform.system() or os.name,
            "os_version": platform.release(),
            "architecture": platform.machine(),
            "python_version": platform.python_version(),
            "uptime_seconds": max(0, round(time() - psutil.boot_time())),
            "process_count": len(psutil.pids()),
            "cpu": {
                "used_percent": round(psutil.cpu_percent(interval=None), 1),
                "logical_cores": psutil.cpu_count(logical=True) or 0,
                "physical_cores": psutil.cpu_count(logical=False) or 0,
                "frequency_mhz": round(cpu_frequency.current, 0) if cpu_frequency else None,
                "load_average": load_average,
            },
            "network": {
                "addresses": sorted(addresses),
                "received_bytes": network.bytes_recv,
                "sent_bytes": network.bytes_sent,
                "receive_bytes_per_second": round(receive_rate),
                "send_bytes_per_second": round(send_rate),
                "packets_received": network.packets_recv,
                "packets_sent": network.packets_sent,
            },
            "disk_io": {
                "read_bytes": disk_io.read_bytes if disk_io else 0,
                "written_bytes": disk_io.write_bytes if disk_io else 0,
                "read_bytes_per_second": round(disk_read_rate),
                "write_bytes_per_second": round(disk_write_rate),
            },
            "temperature": self._temperature_details(),
            "capabilities": {"reboot": self.allow_host_reboot},
        }

    @staticmethod
    def _process_details(process: psutil.Process | None) -> dict[str, Any]:
        if process is None:
            return {
                "pid": None,
                "cpu_percent": 0.0,
                "thread_count": 0,
                "started_at": None,
                "state": "stopped",
            }
        try:
            return {
                "pid": process.pid,
                "cpu_percent": round(process.cpu_percent(interval=None), 1),
                "thread_count": process.num_threads(),
                "started_at": datetime.fromtimestamp(
                    process.create_time(), timezone.utc
                ).isoformat(),
                "state": process.status(),
            }
        except (psutil.Error, OSError, ValueError):
            return {
                "pid": process.pid,
                "cpu_percent": 0.0,
                "thread_count": 0,
                "started_at": None,
                "state": "unknown",
            }

    def reboot_host(self) -> dict[str, Any]:
        if not self.allow_host_reboot:
            raise web.HTTPForbidden(
                text=json.dumps(
                    {
                        "error": "Host reboot is disabled. Set FATE_ALLOW_HOST_REBOOT=1 for the FateControl service, then restart it."
                    }
                ),
                content_type="application/json",
            )
        if os.name == "nt":
            command = [
                "shutdown.exe",
                "/r",
                "/t",
                "10",
                "/d",
                "p:0:0",
                "/c",
                "Reboot requested by Control Panel",
            ]
            delay_seconds = 10
        else:
            command = ["shutdown", "-r", "+1", "Control Panel reboot"]
            delay_seconds = 60
        try:
            # The command and every argument are controller constants; request data
            # is used only for validation and never reaches the process invocation.
            result = subprocess.run(  # noqa: S603
                command,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise web.HTTPServiceUnavailable(
                text=json.dumps({"error": f"Could not schedule the host reboot: {error}"}),
                content_type="application/json",
            ) from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "permission denied").strip()
            raise web.HTTPServiceUnavailable(
                text=json.dumps(
                    {"error": f"The operating system refused the reboot request: {detail[:240]}"}
                ),
                content_type="application/json",
            )
        LOGGER.warning("Host reboot scheduled through authenticated Control Panel")
        return {
            "accepted": True,
            "action": "reboot",
            "delay_seconds": delay_seconds,
            "message": f"{self.instance_name} will reboot shortly.",
        }

    def _snapshot_host_details(self) -> tuple[
        dict[str, Any], psutil.Process | None, dict[str, Any], dict[str, Any]
    ]:
        """Collect blocking host state in one worker with one Fate process scan."""
        detected_process = self._bot_process()
        return (
            self.resource_details(detected_process, scan_for_bot=False),
            detected_process,
            self.system_details(),
            self.health_details(),
        )

    async def snapshot(self, *, include_private: bool = False) -> dict[str, Any]:
        discord_status, host_details = await asyncio.gather(
            self.bot_status(),
            asyncio.to_thread(self._snapshot_host_details),
        )
        resources, detected_process, system, health = host_details
        owned_running = self.owns_running_process()
        external_running = detected_process is not None and not owned_running
        bot_health = (discord_status or {}).get("health", {})
        if isinstance(bot_health, dict):
            health["discord"] = bot_health.get(
                "discord", (discord_status or {}).get("status", "offline")
            )
            health["database"] = bot_health.get("database", "unknown")
            health["extensions_loaded"] = bot_health.get("extensions_loaded")
            health["extensions_configured"] = bot_health.get("extensions_configured")
            health["commands"] = bot_health.get("commands")
            if bot_health.get("summary") not in (None, "Healthy"):
                health["summary"] = bot_health["summary"]
        if self.desired_running and not discord_status:
            health["summary"] = (
                "Fate is starting"
                if owned_running or external_running
                else "Fate is offline"
            )
        online = bool(discord_status and discord_status.get("online"))
        process_details = self._process_details(detected_process)
        snapshot = {
            "service": "fate-control",
            "api_version": 1,
            "instance_name": self.instance_name,
            "instance": (discord_status or {}).get(
                "instance",
                {"role": "unknown", "debug_mode": None, "config": None},
            ),
            "controller_online": True,
            "controller_uptime_seconds": round(monotonic() - self.started),
            "control_available": True,
            "online": online,
            "status": (discord_status or {}).get(
                "status", "starting" if owned_running or external_running else "offline"
            ),
            "servers": (discord_status or {}).get("servers", 0),
            "users": (discord_status or {}).get("users", 0),
            "shards": (discord_status or {}).get("shards", 1),
            "latency_ms": (discord_status or {}).get("latency_ms"),
            "uptime_seconds": (discord_status or {}).get("uptime_seconds", 0),
            "commands_this_month": (discord_status or {}).get(
                "commands_this_month", 0
            ),
            "bot": {
                "desired_running": self.desired_running,
                "process_running": owned_running or external_running,
                "owned": owned_running,
                "external": external_running,
                **process_details,
                "restart_count": self.restart_count,
                "last_exit_code": self.last_exit_code,
                "last_error": self.last_error,
            },
            "discord": discord_status,
            "health": health,
            "resources": resources,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if include_private:
            snapshot["system"] = system
        else:
            snapshot["system"] = {
                "capabilities": {"reboot": False},
            }
        return snapshot

    async def public_status(self, _request: web.Request) -> web.Response:
        return web.json_response(
            await self.snapshot(include_private=False),
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-store",
            },
        )

    async def api_status(self, request: web.Request) -> web.Response:
        self.require_auth(request)
        return web.json_response(await self.snapshot(include_private=True))

    async def get_metrics(self, request: web.Request) -> web.Response:
        """Return one authenticated, bounded telemetry chart series."""
        self.require_auth(request)
        metric = request.query.get("metric", "servers").strip().lower()
        window = request.query.get("window", "1h").strip().lower()
        command = request.query.get("command")
        raw_bucket = request.query.get("bucket_seconds")
        try:
            bucket_seconds = int(raw_bucket) if raw_bucket is not None else None
        except ValueError as error:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "bucket_seconds must be an integer."}),
                content_type="application/json",
            ) from error
        store = await self.active_metrics_store()
        try:
            payload = await asyncio.to_thread(
                build_metric_payload,
                store,
                metric,
                window,
                command=command,
                bucket_seconds=bucket_seconds,
            )
        except ValueError as error:
            raise web.HTTPBadRequest(
                text=json.dumps(
                    {
                        "error": str(error),
                        "metrics": sorted(SUPPORTED_METRICS),
                        "windows": list(WINDOWS),
                    }
                ),
                content_type="application/json",
            ) from error
        payload["metrics"] = sorted(SUPPORTED_METRICS)
        payload["windows"] = list(WINDOWS)
        return web.json_response(payload)

    async def get_command_metrics(self, request: web.Request) -> web.Response:
        """Convenience route for a tapped command's individual breakdown."""
        self.require_auth(request)
        window = request.query.get("window", "1h").strip().lower()
        command = request.match_info.get("command")
        raw_bucket = request.query.get("bucket_seconds")
        try:
            bucket_seconds = int(raw_bucket) if raw_bucket is not None else None
        except ValueError as error:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "bucket_seconds must be an integer."}),
                content_type="application/json",
            ) from error
        store = await self.active_metrics_store()
        try:
            payload = await asyncio.to_thread(
                build_metric_payload,
                store,
                "commands",
                window,
                command=command,
                bucket_seconds=bucket_seconds,
            )
        except ValueError as error:
            raise web.HTTPBadRequest(
                text=json.dumps(
                    {
                        "error": str(error),
                        "metrics": sorted(SUPPORTED_METRICS),
                        "windows": list(WINDOWS),
                    }
                ),
                content_type="application/json",
            ) from error
        payload["metrics"] = sorted(SUPPORTED_METRICS)
        payload["windows"] = list(WINDOWS)
        return web.json_response(payload)

    async def auth_check(self, request: web.Request) -> web.Response:
        self.require_auth(request)
        return web.json_response({"ok": True, "instance_name": self.instance_name})

    async def get_console(self, request: web.Request) -> web.Response:
        self.require_auth(request)
        try:
            requested_lines = int(request.query.get("lines", "250"))
        except ValueError as error:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "lines must be an integer."}),
                content_type="application/json",
            ) from error
        return web.json_response(await asyncio.to_thread(self.console_tail, requested_lines))

    async def set_bot_state(self, request: web.Request) -> web.Response:
        self.require_auth(request)
        payload = await read_json_object(request)
        running = payload.get("running")
        if not isinstance(running, bool):
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "running must be true or false."}),
                content_type="application/json",
            )
        result = await (self.start_bot() if running else self.stop_bot())
        return web.json_response(
            {**result, "status": await self.snapshot(include_private=True)}
        )

    async def bot_action(self, request: web.Request) -> web.Response:
        self.require_auth(request)
        payload = await read_json_object(request)
        if payload.get("action") != "restart":
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "action must be restart."}),
                content_type="application/json",
            )
        result = await self.restart_bot()
        return web.json_response(
            {**result, "action": "restart", "status": await self.snapshot(include_private=True)}
        )

    async def status_server_action(self, request: web.Request) -> web.Response:
        self.require_auth(request)
        payload = await read_json_object(request)
        if payload.get("action") != "restart":
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "action must be restart."}),
                content_type="application/json",
            )
        result = await self.restart_status_server()
        return web.json_response(result)

    async def host_action(self, request: web.Request) -> web.Response:
        self.require_auth(request)
        payload = await read_json_object(request)
        if payload.get("action") != "reboot":
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "action must be reboot."}),
                content_type="application/json",
            )
        expected_confirmation = f"REBOOT {self.instance_name}"
        confirmation = payload.get("confirm")
        if not isinstance(confirmation, str) or not hmac.compare_digest(
            confirmation, expected_confirmation
        ):
            raise web.HTTPBadRequest(
                text=json.dumps(
                    {"error": f"Confirmation must exactly match {expected_confirmation}."}
                ),
                content_type="application/json",
            )
        return web.json_response(await asyncio.to_thread(self.reboot_host), status=202)

    async def _google_drive_client(self) -> tuple[GoogleDriveClient, dict[str, Any]]:
        if self.drive_http is None:
            raise GoogleDriveError("FateControl's Google Drive client is not running.")
        snapshot = await asyncio.to_thread(read_config_snapshot, self.config_path)
        settings = snapshot.config.get("backups", {})
        if not isinstance(settings, dict):
            settings = {}
        credentials = await asyncio.to_thread(
            GoogleDriveCredentials.load, settings, REPOSITORY_ROOT
        )
        client = GoogleDriveClient(
            self.drive_http,
            credentials,
            GoogleDriveTokenStore(drive_token_path(settings)),
        )
        return client, snapshot.config

    async def get_backup_settings(self, request: web.Request) -> web.Response:
        self.require_auth(request)
        try:
            snapshot = await asyncio.to_thread(read_config_snapshot, self.config_path)
        except ConfigError as error:
            raise web.HTTPServiceUnavailable(
                text=json.dumps({"error": str(error)}), content_type="application/json"
            ) from error
        configured = True
        linked = False
        setup_error = None
        try:
            client, _config = await self._google_drive_client()
            linked = await client.linked()
        except GoogleDriveError as error:
            configured = False
            setup_error = str(error)
        return web.json_response(
            {
                "settings": backup_settings_payload(snapshot.config),
                "revision": snapshot.fingerprint,
                "drive": {
                    "configured": configured,
                    "linked": linked,
                    "error": setup_error,
                },
            }
        )

    async def save_backup_settings(self, request: web.Request) -> web.Response:
        self.require_auth(request)
        payload = await read_json_object(request)
        proposed = payload.get("settings")
        expected_revision = payload.get("revision")
        try:
            settings, revision = await asyncio.to_thread(
                update_backup_settings,
                self.config_path,
                proposed,
                expected_revision=expected_revision,
            )
        except ConfigConflictError as error:
            raise web.HTTPConflict(
                text=json.dumps({"error": "Backup settings changed. Reload and try again."}),
                content_type="application/json",
            ) from error
        except ConfigError as error:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": str(error)}), content_type="application/json"
            ) from error
        return web.json_response(
            {"saved": True, "settings": settings, "revision": revision}
        )

    async def start_google_drive_authorization(self, request: web.Request) -> web.Response:
        self.require_auth(request)
        try:
            client, _config = await self._google_drive_client()
        except GoogleDriveError as error:
            raise web.HTTPServiceUnavailable(
                text=json.dumps({"error": str(error)}), content_type="application/json"
            ) from error
        now = time()
        self.drive_oauth_states = {
            state: expires
            for state, expires in self.drive_oauth_states.items()
            if expires > now
        }
        authorization_url, state = client.authorization_url()
        self.drive_oauth_states[state] = now + 10 * 60
        return web.json_response({"authorization_url": authorization_url, "expires_in": 600})

    async def google_drive_callback(self, request: web.Request) -> web.Response:
        state = request.query.get("state", "")
        expires = self.drive_oauth_states.pop(state, None)
        oauth_error = request.query.get("error")
        code = request.query.get("code", "")
        if not state or expires is None or expires <= time():
            return self._drive_callback_page(
                "Link expired",
                "This Google Drive link request expired. Return to Fate Control and try again.",
                success=False,
                status=400,
            )
        if oauth_error:
            return self._drive_callback_page(
                "Drive was not linked",
                f"Google returned: {oauth_error}",
                success=False,
                status=400,
            )
        if not code:
            return self._drive_callback_page(
                "Drive was not linked",
                "Google did not return an authorization code.",
                success=False,
                status=400,
            )
        try:
            client, _config = await self._google_drive_client()
            await client.exchange_code(code)
        except GoogleDriveError as error:
            LOGGER.warning("Google Drive OAuth callback failed: %s", error)
            return self._drive_callback_page(
                "Drive was not linked", str(error), success=False, status=502
            )
        return self._drive_callback_page(
            "Google Drive linked",
            "Return to Fate Control to choose the remote backup folder.",
            success=True,
        )

    @staticmethod
    def _drive_callback_page(
        title: str, message: str, *, success: bool, status: int = 200
    ) -> web.Response:
        safe_title = html.escape(title)
        safe_message = html.escape(message)
        return_link = (
            '<a href="fatecontrol://backups/linked">Return to Fate Control</a>'
            if success
            else ""
        )
        auto_return = (
            '<script>setTimeout(function(){location.href="fatecontrol://backups/linked"},800);</script>'
            if success
            else ""
        )
        page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{safe_title}</title><style>
body{{font:16px system-ui;background:#111318;color:#f5f1e8;display:grid;place-items:center;min-height:100vh;margin:0}}
main{{max-width:34rem;padding:2rem;background:#20242c;border-radius:1rem}}a{{color:#e8bd67}}
</style></head><body><main><h1>{safe_title}</h1><p>{safe_message}</p>{return_link}</main>{auto_return}</body></html>"""
        return web.Response(text=page, content_type="text/html", status=status)

    async def list_google_drive_folders(self, request: web.Request) -> web.Response:
        self.require_auth(request)
        parent_id = request.query.get("parent_id", "root")
        try:
            client, _config = await self._google_drive_client()
            folders = await client.list_folders(parent_id)
        except GoogleDriveError as error:
            raise web.HTTPBadGateway(
                text=json.dumps({"error": str(error)}), content_type="application/json"
            ) from error
        return web.json_response({"parent_id": parent_id, "folders": folders})

    async def select_google_drive_folder(self, request: web.Request) -> web.Response:
        self.require_auth(request)
        payload = await read_json_object(request)
        folder_id = payload.get("folder_id")
        if not isinstance(folder_id, str) or not folder_id:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "folder_id is required."}),
                content_type="application/json",
            )
        try:
            client, _config = await self._google_drive_client()
            folder_path = await client.resolve_folder_path(folder_id)
            settings, revision = await asyncio.to_thread(
                update_backup_settings,
                self.config_path,
                {},
                folder_id=folder_id,
                folder_path=folder_path,
            )
        except GoogleDriveError as error:
            raise web.HTTPBadGateway(
                text=json.dumps({"error": str(error)}), content_type="application/json"
            ) from error
        return web.json_response(
            {
                "selected": True,
                "folder_id": folder_id,
                "folder_path": folder_path,
                "settings": settings,
                "revision": revision,
            }
        )

    async def get_config(self, request: web.Request) -> web.Response:
        self.require_auth(request)
        try:
            snapshot = await asyncio.to_thread(
                read_config_snapshot,
                self.config_path,
            )
            config = redact_config(snapshot.config)
            browser_config, unsafe_integer_paths = control_panel_config(config)
        except ConfigError as error:
            raise web.HTTPServiceUnavailable(
                text=json.dumps({"error": str(error)}),
                content_type="application/json",
            ) from error
        return web.json_response(
            {
                "config": config,
                "control_config": browser_config,
                "unsafe_integer_paths": unsafe_integer_paths,
                "revision": snapshot.fingerprint,
                "redaction_marker": REDACTED,
                "requires_restart": True,
            }
        )

    async def save_config(self, request: web.Request) -> web.Response:
        self.require_auth(request)
        payload = await read_json_object(request)
        proposed = restore_control_panel_integers(
            payload.get("config"), payload.get("unsafe_integer_paths")
        )
        revision = payload.get("revision")
        restart = payload.get("restart", False)
        if not isinstance(restart, bool):
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "restart must be true or false."}),
                content_type="application/json",
            )
        if not _valid_revision(revision):
            raise web.HTTPBadRequest(
                text=json.dumps(
                    {"error": "A valid config revision is required. Reload and try again."}
                ),
                content_type="application/json",
            )
        try:
            _saved, new_revision, controller_restart_required = await asyncio.to_thread(
                update_config,
                self.config_path,
                proposed,
                expected_revision=revision,
            )
        except ConfigConflictError as error:
            raise web.HTTPConflict(
                text=json.dumps(
                    {"error": "Fate's config changed. Reload it before saving again."}
                ),
                content_type="application/json",
            ) from error
        except ConfigError as error:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": str(error)}),
                content_type="application/json",
            ) from error
        restart_result = None
        if restart:
            restart_result = await self.restart_bot()
        return web.json_response(
            {
                "saved": True,
                "revision": new_revision,
                "backup": str(self.config_path.with_suffix(self.config_path.suffix + ".bak")),
                "restart": restart_result,
                "controller_restart_required": controller_restart_required,
            }
        )


@web.middleware
async def response_headers(request: web.Request, handler):
    try:
        response = await handler(request)
    except ConfigError as error:
        response = web.json_response({"error": str(error)}, status=400)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    control_request = (
        request.path in CONTROL_PANEL_ROUTES
        or any(request.path.startswith(f"{route}/") for route in CONTROL_PANEL_ROUTES)
    )
    if control_request:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        response.headers["Cache-Control"] = "no-cache"
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@web.middleware
async def source_ip_allowlist(request: web.Request, handler):
    controller = request.app[CONTROLLER_KEY]
    if not controller.client_ip_allowed(request.remote):
        return web.Response(status=403, text="This FateControl address is private.")
    return await handler(request)


CONTROLLER_KEY = web.AppKey("controller", FateController)


def create_app(controller: FateController | None = None) -> web.Application:
    controller = controller or FateController()
    app = web.Application(
        middlewares=[response_headers, source_ip_allowlist],
        client_max_size=512_000,
    )
    app[CONTROLLER_KEY] = controller
    app.on_startup.append(controller.startup)
    app.on_cleanup.append(controller.cleanup)
    app.router.add_get("/", controller.public_status)
    app.router.add_get("/status", controller.public_status)
    app.router.add_get("/control", controller.control_panel_page)
    app.router.add_get("/control/", controller.control_panel_page)
    app.router.add_static("/control/assets", CONTROL_PANEL_ROOT, append_version=True)
    app.router.add_get("/api/v1/status", controller.api_status)
    app.router.add_get("/api/v1/metrics", controller.get_metrics)
    app.router.add_get(
        "/api/v1/metrics/commands/{command}", controller.get_command_metrics
    )
    app.router.add_get("/api/v1/auth/check", controller.auth_check)
    app.router.add_get("/api/v1/control/manifest", controller.control_manifest)
    app.router.add_post("/api/v1/control/session", controller.create_control_session)
    app.router.add_post(
        "/api/v1/control/session/exchange", controller.exchange_control_session
    )
    app.router.add_post("/api/v1/control/session/login", controller.login_control_session)
    app.router.add_post("/api/v1/control/session/logout", controller.logout_control_session)
    app.router.add_get("/api/v1/console", controller.get_console)
    app.router.add_post("/api/v1/bot/state", controller.set_bot_state)
    app.router.add_post("/api/v1/bot/action", controller.bot_action)
    app.router.add_post(
        "/api/v1/status-server/action", controller.status_server_action
    )
    app.router.add_post("/api/v1/host/action", controller.host_action)
    app.router.add_get("/api/v1/backups/settings", controller.get_backup_settings)
    app.router.add_put("/api/v1/backups/settings", controller.save_backup_settings)
    app.router.add_post(
        "/api/v1/backups/google/authorize",
        controller.start_google_drive_authorization,
    )
    app.router.add_get(
        "/api/v1/backups/google/callback", controller.google_drive_callback
    )
    app.router.add_get(
        "/api/v1/backups/google/folders", controller.list_google_drive_folders
    )
    app.router.add_post(
        "/api/v1/backups/google/folder", controller.select_google_drive_folder
    )
    app.router.add_get("/api/v1/config", controller.get_config)
    app.router.add_put("/api/v1/config", controller.save_config)
    return app


def parse_args() -> argparse.Namespace:
    control_settings: dict[str, Any] = {}
    try:
        config = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
        candidate = config.get("control_panel", {}) if isinstance(config, dict) else {}
        if isinstance(candidate, dict):
            control_settings = candidate
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    configured_host = control_settings.get("host", "0.0.0.0")
    if not isinstance(configured_host, str) or not configured_host.strip():
        configured_host = "0.0.0.0"
    configured_port = control_settings.get("port", 16421)
    if type(configured_port) is not int or not 1 <= configured_port <= 65535:
        configured_port = 16421
    parser = argparse.ArgumentParser(description="Run the Fate LAN control service")
    parser.add_argument(
        "--host", default=os.getenv("FATE_CONTROL_HOST", configured_host.strip())
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("FATE_CONTROL_PORT", str(configured_port))),
    )
    parser.add_argument("--no-autostart", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    controller = FateController(autostart=False if args.no_autostart else None)
    if controller.token_created:
        LOGGER.warning("Created the Android pairing key at %s", controller.token_path)
    LOGGER.info("FateControl listening on %s:%s as %s", args.host, args.port, controller.instance_name)
    web.run_app(create_app(controller), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
