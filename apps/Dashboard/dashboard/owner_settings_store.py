"""Bot-owner-only settings backed by Fate's process configuration file."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterator

from .validation import ValidationError

OWNER_SETTING_KEYS = frozenset(
    {
        "activity_status",
        "theme_color",
        "debug_mode",
        "debug_logging",
        "max_cached_messages",
    }
)
RESTART_REQUIRED_FIELDS = ("max_cached_messages",)
THEME_COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")
ASCII_SNOWFLAKE_PATTERN = re.compile(r"^[0-9]{1,20}$")
REVISION_PATTERN = re.compile(r"^[0-9a-f]{64}$")
DISCORD_SNOWFLAKE_MAX = (1 << 64) - 1
CONFIG_LOCK_TIMEOUT_SECONDS = 10.0
CONFIG_LOCK_POLL_SECONDS = 0.05
BIDI_CONTROL_CHARACTERS = frozenset(
    {"\u061c", "\u200e", "\u200f", *map(chr, range(0x202A, 0x202F)), *map(chr, range(0x2066, 0x206A))}
)
MEMORY_DEFAULTS = {
    "activity_status": ".help",
    "theme_color": "#001269",
    "debug_mode": False,
    "debug_logging": False,
    "max_cached_messages": 64_000,
}


class ConfigConflictError(RuntimeError):
    """The config changed outside the shared lock while a write was prepared."""


class OwnerAccessRevokedError(PermissionError):
    """The actor was no longer an owner when the config transaction began."""


@dataclass(frozen=True)
class OwnerSettingsSnapshot:
    settings: dict[str, Any]
    revision: str


@dataclass(frozen=True)
class OwnerSettingsSaveResult:
    settings: dict[str, Any]
    revision: str
    presence_synced: bool | None = None
    presence_warning: str | None = None


@dataclass(frozen=True)
class _ConfigSnapshot:
    config: dict[str, Any]
    raw: bytes
    fingerprint: str


def _contains_unsafe_display_character(value: str) -> bool:
    return any(
        unicodedata.category(character) in {"Cc", "Cs"}
        or character in BIDI_CONTROL_CHARACTERS
        for character in value
    )


def validate_owner_settings(payload: Any) -> dict[str, Any]:
    """Validate the complete, deliberately small public owner-settings shape."""
    if not isinstance(payload, dict):
        raise ValidationError("Owner settings must be a JSON object.")

    supplied = set(payload)
    if supplied != OWNER_SETTING_KEYS:
        missing = sorted(OWNER_SETTING_KEYS - supplied)
        unknown = sorted(supplied - OWNER_SETTING_KEYS)
        if unknown:
            raise ValidationError(
                f"Unknown owner setting{'s' if len(unknown) != 1 else ''}: "
                f"{', '.join(unknown)}."
            )
        raise ValidationError(
            f"Missing owner setting{'s' if len(missing) != 1 else ''}: "
            f"{', '.join(missing)}."
        )

    activity_status = payload["activity_status"]
    if not isinstance(activity_status, str):
        raise ValidationError("Activity status must be text.")
    if _contains_unsafe_display_character(activity_status):
        raise ValidationError(
            "Activity status cannot contain control or bidirectional formatting characters."
        )
    activity_status = activity_status.strip()
    if not 1 <= len(activity_status) <= 128:
        raise ValidationError("Activity status must be between 1 and 128 characters.")

    theme_color = payload["theme_color"]
    if not isinstance(theme_color, str) or not THEME_COLOR_PATTERN.fullmatch(theme_color):
        raise ValidationError("Theme color must use the #RRGGBB format.")

    debug_mode = payload["debug_mode"]
    if type(debug_mode) is not bool:
        raise ValidationError("Debug mode must be true or false.")

    debug_logging = payload["debug_logging"]
    if type(debug_logging) is not bool:
        raise ValidationError("Debug logging must be true or false.")

    max_cached_messages = payload["max_cached_messages"]
    if type(max_cached_messages) is not int or not 100 <= max_cached_messages <= 500_000:
        raise ValidationError("Cached messages must be between 100 and 500000.")

    return {
        "activity_status": activity_status,
        "theme_color": theme_color.upper(),
        "debug_mode": debug_mode,
        "debug_logging": debug_logging,
        "max_cached_messages": max_cached_messages,
    }


def _valid_owner_id(raw_id: Any) -> int | None:
    if type(raw_id) is int:
        return raw_id if 0 < raw_id <= DISCORD_SNOWFLAKE_MAX else None
    if isinstance(raw_id, str) and ASCII_SNOWFLAKE_PATTERN.fullmatch(raw_id):
        parsed = int(raw_id)
        return parsed if 0 < parsed <= DISCORD_SNOWFLAKE_MAX else None
    return None


def _valid_revision(value: Any) -> bool:
    return isinstance(value, str) and REVISION_PATTERN.fullmatch(value) is not None


def owner_ids_from_config(config: dict[str, Any]) -> set[int]:
    """Return only valid Discord snowflakes from Fate's owner configuration."""
    raw_ids: list[Any] = [config.get("bot_owner_id")]
    additional = config.get("bot_owner_ids", [])
    if isinstance(additional, list):
        raw_ids.extend(additional)

    return {
        owner_id
        for raw_id in raw_ids
        if (owner_id := _valid_owner_id(raw_id)) is not None
    }


def resolve_config_path(repository_root: Path, bot=None) -> Path:
    """Return the canonical owner-settings file shared by every profile."""
    return (repository_root / "data" / "config.json").resolve()


def _canonical_config_path(path: Path) -> Path:
    candidate = path if path.is_absolute() else Path.cwd() / path
    return candidate.parent.resolve() / candidate.name


def _config_lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


@contextmanager
def _exclusive_config_lock(path: Path) -> Iterator[None]:
    """Lock the stable sibling lock file shared with FateControl.

    The lock file is intentionally retained. Deleting it could let a new process
    lock a different inode while an existing process still owns the old lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _config_lock_path(path)
    try:
        if lock_path.is_symlink():
            raise RuntimeError("Fate's configuration lock cannot be a symbolic link.")
    except OSError as error:
        raise RuntimeError("Fate's configuration lock is unavailable.") from error
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        lock_stat = os.fstat(descriptor)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
            raise RuntimeError("Fate's configuration lock is not a regular file.")
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)

        if os.name == "nt":
            import msvcrt

            deadline = time.monotonic() + CONFIG_LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            "Timed out waiting for Fate's configuration lock."
                        ) from error
                    time.sleep(CONFIG_LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            deadline = time.monotonic() + CONFIG_LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            "Timed out waiting for Fate's configuration lock."
                        ) from error
                    time.sleep(CONFIG_LOCK_POLL_SECONDS)
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
            raise RuntimeError("Fate's owner settings configuration is not a regular file.")
        with path.open("rb") as config_file:
            opened_stat = os.fstat(config_file.fileno())
            if (
                opened_stat.st_dev != path_stat.st_dev
                or opened_stat.st_ino != path_stat.st_ino
            ):
                raise ConfigConflictError("Fate's configuration changed while it was opened.")
            raw = config_file.read()
            final_stat = os.fstat(config_file.fileno())
            if (
                final_stat.st_size != opened_stat.st_size
                or final_stat.st_mtime_ns != opened_stat.st_mtime_ns
            ):
                raise ConfigConflictError("Fate's configuration changed while it was read.")
            return raw
    except (ConfigConflictError, RuntimeError):
        raise
    except OSError as error:
        raise RuntimeError("Fate's owner settings are temporarily unavailable.") from error


def _read_config_snapshot(path: Path) -> _ConfigSnapshot:
    raw = _read_regular_bytes(path)
    try:
        config = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as error:
        raise RuntimeError("Fate's owner settings are temporarily unavailable.") from error
    if not isinstance(config, dict):
        raise RuntimeError("Fate's owner settings configuration is invalid.")
    return _ConfigSnapshot(config, raw, hashlib.sha256(raw).hexdigest())


def _read_config(path: Path) -> dict[str, Any]:
    with _exclusive_config_lock(path):
        return _read_config_snapshot(path).config


def _settings_from_config(config: dict[str, Any]) -> dict[str, Any]:
    color = config.get("theme_color")
    if type(color) is not int or not 0 <= color <= 0xFFFFFF:
        raise RuntimeError("Fate's configured theme color is invalid.")
    try:
        return validate_owner_settings(
            {
                "activity_status": config["activity_status"],
                "theme_color": f"#{color:06X}",
                "debug_mode": config["debug_mode"],
                "debug_logging": config.get("debug_logging", False),
                "max_cached_messages": config["max_cached_messages"],
            }
        )
    except (KeyError, ValidationError) as error:
        raise RuntimeError("Fate's owner settings configuration is invalid.") from error


def _merge_settings(config: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(config)
    merged.update(
        {
            "activity_status": settings["activity_status"],
            "theme_color": int(settings["theme_color"][1:], 16),
            "debug_mode": settings["debug_mode"],
            "debug_logging": settings["debug_logging"],
            "max_cached_messages": settings["max_cached_messages"],
        }
    )
    return merged


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


def _atomic_write_config_locked(
    path: Path, config: dict[str, Any], snapshot: _ConfigSnapshot | None
) -> str:
    """Publish a config and its backup without following either destination."""
    try:
        serialized = (
            json.dumps(config, indent=4, ensure_ascii=False) + "\n"
        ).encode("utf-8")
    except (TypeError, UnicodeError, ValueError) as error:
        raise RuntimeError("Fate's owner settings configuration is invalid.") from error
    config_temporary = _write_secure_temporary(path, serialized)
    backup_temporary: Path | None = None
    backup = path.with_suffix(path.suffix + ".bak")
    try:
        if snapshot is not None:
            backup_temporary = _write_secure_temporary(backup, snapshot.raw)
            current_fingerprint = hashlib.sha256(_read_regular_bytes(path)).hexdigest()
            if current_fingerprint != snapshot.fingerprint:
                raise ConfigConflictError(
                    "Fate's configuration changed while this update was being prepared."
                )
            os.replace(backup_temporary, backup)
            backup_temporary = None
            # Narrow the remaining race with writers that deliberately ignore the lock.
            current_fingerprint = hashlib.sha256(_read_regular_bytes(path)).hexdigest()
            if current_fingerprint != snapshot.fingerprint:
                raise ConfigConflictError(
                    "Fate's configuration changed before this update could be committed."
                )
        elif path.exists():
            raise ConfigConflictError(
                "Fate's configuration appeared while this update was being prepared."
            )

        os.replace(config_temporary, path)
        config_temporary = None
        _fsync_directory(path.parent)
        return hashlib.sha256(serialized).hexdigest()
    finally:
        for temporary in (config_temporary, backup_temporary):
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass


def _atomic_write_config(path: Path, config: dict[str, Any]) -> None:
    """Atomically replace config.json under the shared cross-process lock."""
    path = _canonical_config_path(path)
    with _exclusive_config_lock(path):
        snapshot = _read_config_snapshot(path) if path.exists() else None
        _atomic_write_config_locked(path, config, snapshot)


def _owner_save_transaction(
    path: Path,
    settings: dict[str, Any],
    actor_id: int,
    expected_revision: str,
) -> str:
    with _exclusive_config_lock(path):
        snapshot = _read_config_snapshot(path)
        if _valid_owner_id(actor_id) not in owner_ids_from_config(snapshot.config):
            raise OwnerAccessRevokedError(
                "Bot-owner access was revoked before these settings could be saved."
            )
        if not _valid_revision(expected_revision) or not hmac.compare_digest(
            snapshot.fingerprint, expected_revision
        ):
            raise ConfigConflictError(
                "Fate's configuration changed after these settings were loaded."
            )
        merged = _merge_settings(snapshot.config, settings)
        return _atomic_write_config_locked(path, merged, snapshot)


def _owner_get_transaction(
    path: Path,
    actor_id: int | None = None,
) -> OwnerSettingsSnapshot:
    with _exclusive_config_lock(path):
        snapshot = _read_config_snapshot(path)
        if actor_id is not None and (
            _valid_owner_id(actor_id) not in owner_ids_from_config(snapshot.config)
        ):
            raise OwnerAccessRevokedError(
                "Bot-owner access was revoked before these settings could be loaded."
            )
        return OwnerSettingsSnapshot(
            settings=_settings_from_config(snapshot.config),
            revision=snapshot.fingerprint,
        )


class FileOwnerSettingsStore:
    """Owner settings for a standalone dashboard sharing Fate's config file."""

    def __init__(self, config_path: Path):
        self.config_path = _canonical_config_path(config_path)
        self.lock = asyncio.Lock()

    async def close(self) -> None:
        return None

    async def is_owner(self, user_id: int) -> bool:
        config = await asyncio.to_thread(_read_config, self.config_path)
        return _valid_owner_id(user_id) in owner_ids_from_config(config)

    async def get(self) -> dict[str, Any]:
        return (await self.get_snapshot()).settings

    async def get_snapshot(self) -> OwnerSettingsSnapshot:
        return await asyncio.to_thread(_owner_get_transaction, self.config_path)

    async def get_snapshot_for_owner(self, actor_id: int) -> OwnerSettingsSnapshot:
        return await asyncio.to_thread(
            _owner_get_transaction,
            self.config_path,
            actor_id,
        )

    async def save(
        self,
        payload: Any,
        *,
        actor_id: int,
        expected_revision: str,
    ) -> OwnerSettingsSaveResult:
        settings = validate_owner_settings(payload)
        async with self.lock:
            try:
                revision = await asyncio.to_thread(
                    _owner_save_transaction,
                    self.config_path,
                    settings,
                    actor_id,
                    expected_revision,
                )
            except OwnerAccessRevokedError:
                raise
            except OSError as error:
                raise RuntimeError("Fate's owner settings could not be saved.") from error
        return OwnerSettingsSaveResult(
            settings=deepcopy(settings),
            revision=revision,
        )


class BotOwnerSettingsStore(FileOwnerSettingsStore):
    """File-backed owner settings mirrored into a mounted Fate process."""

    def __init__(self, bot, config_path: Path):
        super().__init__(config_path)
        self.bot = bot
        self.live_sync_lock = asyncio.Lock()

    async def is_owner(self, user_id: int) -> bool:
        # Disk is authoritative so revocations do not wait for a process restart.
        return await super().is_owner(user_id)

    async def save(
        self,
        payload: Any,
        *,
        actor_id: int,
        expected_revision: str,
    ) -> OwnerSettingsSaveResult:
        async with self.live_sync_lock:
            result = await super().save(
                payload,
                actor_id=actor_id,
                expected_revision=expected_revision,
            )
            settings = result.settings
            self.bot.config.update(
                {
                    "activity_status": settings["activity_status"],
                    "theme_color": int(settings["theme_color"][1:], 16),
                    "max_cached_messages": settings["max_cached_messages"],
                }
            )
            self.bot.theme_color = self.bot.config["theme_color"]
            self.bot.debug_mode = settings["debug_mode"]
            self.bot.debug_logging = settings["debug_logging"]

            change_presence = getattr(self.bot, "change_presence", None)
            is_ready = getattr(self.bot, "is_ready", None)
            ready = is_ready() if callable(is_ready) else True
            if not callable(change_presence) or not ready:
                return result
            try:
                import discord

                await change_presence(
                    status=discord.Status.online,
                    activity=discord.Game(name=settings["activity_status"]),
                )
            except Exception:
                return replace(
                    result,
                    presence_synced=False,
                    presence_warning=(
                        "Settings were saved, but Fate could not update its Discord presence."
                    ),
                )
            return replace(result, presence_synced=True)


class MemoryOwnerSettingsStore:
    """Explicitly non-persistent owner settings used only by local demo mode."""

    def __init__(self, owner_ids: set[int] | None = None):
        self.owner_ids = set(owner_ids or ())
        self.settings = deepcopy(MEMORY_DEFAULTS)
        self.revision = self._revision()
        self.lock = asyncio.Lock()

    def _revision(self) -> str:
        serialized = json.dumps(
            self.settings,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()

    async def close(self) -> None:
        return None

    async def is_owner(self, user_id: int) -> bool:
        return _valid_owner_id(user_id) in self.owner_ids

    async def get(self) -> dict[str, Any]:
        return deepcopy(self.settings)

    async def get_snapshot(self) -> OwnerSettingsSnapshot:
        async with self.lock:
            return OwnerSettingsSnapshot(
                settings=deepcopy(self.settings),
                revision=self.revision,
            )

    async def get_snapshot_for_owner(self, actor_id: int) -> OwnerSettingsSnapshot:
        async with self.lock:
            if _valid_owner_id(actor_id) not in self.owner_ids:
                raise OwnerAccessRevokedError(
                    "Bot-owner access was revoked before these settings could be loaded."
                )
            return OwnerSettingsSnapshot(
                settings=deepcopy(self.settings),
                revision=self.revision,
            )

    async def save(
        self,
        payload: Any,
        *,
        actor_id: int,
        expected_revision: str,
    ) -> OwnerSettingsSaveResult:
        settings = validate_owner_settings(payload)
        async with self.lock:
            if _valid_owner_id(actor_id) not in self.owner_ids:
                raise OwnerAccessRevokedError(
                    "Bot-owner access was revoked before these settings could be saved."
                )
            if not _valid_revision(expected_revision) or not hmac.compare_digest(
                self.revision, expected_revision
            ):
                raise ConfigConflictError(
                    "Fate's settings changed after they were loaded."
                )
            self.settings = settings
            self.revision = self._revision()
            return OwnerSettingsSaveResult(
                settings=deepcopy(self.settings),
                revision=self.revision,
            )
