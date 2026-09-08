"""Non-blocking database and local-state backups for Fate.

One archive represents one complete backup run.  Database dump programs run as
async subprocesses, while filesystem traversal, compression, and retention are
sent to worker threads so Discord's event loop stays responsive.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

import aiohttp

from botutils.google_drive import (
    GoogleDriveClient,
    GoogleDriveCredentials,
    GoogleDriveError,
    GoogleDriveTokenStore,
)
from botutils.local_databases import mysql_settings

GIBIBYTE = 1024 ** 3
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BACKUP_ROOT = REPOSITORY_ROOT / "data" / "backups"
DEFAULT_TOKEN_PATH = REPOSITORY_ROOT / "data" / "google-drive-token.json"


@dataclass(frozen=True)
class BackupSettings:
    enabled: bool
    location: Path
    frequency_hours: float
    retention_days: int | None
    max_storage_bytes: int | None
    max_backups: int | None
    include_local_files: bool
    mysqldump_path: str
    mongodump_path: str
    drive_enabled: bool
    drive_folder_id: str | None

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "BackupSettings":
        raw = config.get("backups", {})
        if not isinstance(raw, dict):
            raw = {}
        raw_location = raw.get("location", str(DEFAULT_BACKUP_ROOT))
        location = Path(os.path.expandvars(os.path.expanduser(str(raw_location))))
        if not location.is_absolute():
            location = REPOSITORY_ROOT / location
        max_storage_gb = raw.get("max_storage_gb")
        drive = raw.get("google_drive", {})
        if not isinstance(drive, dict):
            drive = {}
        folder_id = drive.get("folder_id")
        return cls(
            enabled=raw.get("enabled", True) is True,
            location=location,
            frequency_hours=float(raw.get("frequency_hours", 12)),
            retention_days=_optional_int(raw.get("retention_days", 7)),
            max_storage_bytes=(
                None
                if max_storage_gb is None
                else max(1, round(float(max_storage_gb) * GIBIBYTE))
            ),
            max_backups=_optional_int(raw.get("max_backups")),
            include_local_files=raw.get("include_local_files", True) is True,
            mysqldump_path=str(raw.get("mysqldump_path", "mysqldump")),
            mongodump_path=str(raw.get("mongodump_path", "mongodump")),
            drive_enabled=drive.get("enabled", False) is True,
            drive_folder_id=(
                str(folder_id) if isinstance(folder_id, str) and folder_id else None
            ),
        )


@dataclass(frozen=True)
class BackupResult:
    path: Path
    size: int
    duration_seconds: float
    drive_file_id: str | None
    removed_local: tuple[str, ...]
    removed_remote: tuple[str, ...]


class BackupManager:
    def __init__(self, bot: Any):
        self.bot = bot
        self._run_lock = asyncio.Lock()
        self._active_process: asyncio.subprocess.Process | None = None

    @property
    def running(self) -> bool:
        return self._run_lock.locked()

    async def run(self) -> BackupResult:
        if self._run_lock.locked():
            raise RuntimeError("A backup is already running.")
        async with self._run_lock:
            started = monotonic()
            settings = BackupSettings.from_config(self.bot.config)
            if not settings.enabled:
                raise RuntimeError("Automatic backups are disabled.")
            archive = await self._create_archive(settings)
            drive_file_id: str | None = None
            removed_remote: list[str] = []
            removed_local: list[str] = []
            try:
                if settings.drive_enabled:
                    if not settings.drive_folder_id:
                        raise GoogleDriveError(
                            "Google Drive backups are enabled, but no remote folder is selected."
                        )
                    drive_file_id, removed_remote = await self._upload_to_drive(
                        archive, settings
                    )
            finally:
                removed_local = await asyncio.to_thread(
                    prune_local_backups,
                    archive.parent,
                    retention_days=settings.retention_days,
                    max_backups=settings.max_backups,
                    max_storage_bytes=settings.max_storage_bytes,
                )
            size = await asyncio.to_thread(lambda: archive.stat().st_size if archive.exists() else 0)
            return BackupResult(
                path=archive,
                size=size,
                duration_seconds=monotonic() - started,
                drive_file_id=drive_file_id,
                removed_local=tuple(removed_local),
                removed_remote=tuple(removed_remote),
            )

    async def close(self) -> None:
        process = self._active_process
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    async def _create_archive(self, settings: BackupSettings) -> Path:
        archives = settings.location / "archives"
        staging = settings.location / ".staging"
        await asyncio.to_thread(_ensure_directories, archives, staging)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        final_path = archives / f"fate-backup-{stamp}.zip"
        workspace = Path(
            await asyncio.to_thread(
                tempfile.mkdtemp, prefix="fate-backup-", dir=str(staging)
            )
        )
        try:
            mysql_output = workspace / "mysql.sql"
            mongo_output = workspace / "mongo.archive.gz"
            await self._dump_mysql(settings, mysql_output)
            await self._dump_mongo(settings, mongo_output)
            local_output: Path | None = None
            if settings.include_local_files:
                local_output = workspace / "local-files.zip"
                await asyncio.to_thread(
                    _archive_local_files,
                    _datastore_path(self.bot.config),
                    local_output,
                    settings.location,
                    _backup_secret_paths(self.bot.config),
                )
            manifest = {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "format": 1,
                "contents": {
                    "mysql": mysql_output.name,
                    "mongodb": mongo_output.name,
                    "local_files": local_output.name if local_output else None,
                },
            }
            await asyncio.to_thread(
                (workspace / "manifest.json").write_text,
                json.dumps(manifest, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary_archive = workspace / final_path.name
            members = [mysql_output, mongo_output, workspace / "manifest.json"]
            if local_output is not None:
                members.append(local_output)
            await asyncio.to_thread(_build_package, temporary_archive, members)
            await asyncio.to_thread(os.replace, temporary_archive, final_path)
        finally:
            await asyncio.to_thread(shutil.rmtree, workspace, True)
        return final_path

    async def _dump_mysql(self, settings: BackupSettings, output: Path) -> None:
        credentials = mysql_settings(self.bot.auth, self.bot.config)
        required = ("user", "password", "db")
        missing = [key for key in required if credentials.get(key) in (None, "")]
        if missing:
            raise RuntimeError(f"MySQL backup credentials are missing: {', '.join(missing)}")
        environment = os.environ.copy()
        environment["MYSQL_PWD"] = str(credentials["password"])
        command = [
            settings.mysqldump_path,
            "--single-transaction",
            "--quick",
            "--routines",
            "--events",
            "--triggers",
            "--hex-blob",
            "--default-character-set=utf8mb4",
            f"--host={credentials.get('host', '127.0.0.1')}",
            f"--port={credentials.get('port', 3306)}",
            f"--user={credentials['user']}",
            f"--result-file={output}",
            str(credentials["db"]),
        ]
        await self._run_process(command, environment=environment, label="mysqldump")
        if not await asyncio.to_thread(output.is_file):
            raise RuntimeError("mysqldump completed without creating its output file.")

    async def _dump_mongo(self, settings: BackupSettings, output: Path) -> None:
        auth = self.bot.auth.get("MongoDB", {})
        public = self.bot.config.get("mongodb", {})
        if not isinstance(auth, dict):
            auth = {}
        if not isinstance(public, dict):
            public = {}
        uri = str(auth.get("url") or public.get("url") or "mongodb://127.0.0.1:27017")
        if "://" not in uri:
            uri = "mongodb://" + uri
        database = str(public.get("db") or auth.get("db") or "fate")
        private_config = output.parent / "mongodump-config.yml"
        await asyncio.to_thread(
            _write_private_text,
            private_config,
            "uri: " + json.dumps(uri) + "\n",
        )
        command = [
            settings.mongodump_path,
            f"--config={private_config}",
            f"--db={database}",
            f"--archive={output}",
            "--gzip",
        ]
        await self._run_process(command, label="mongodump")
        if not await asyncio.to_thread(output.is_file):
            raise RuntimeError("mongodump completed without creating its output file.")

    async def _run_process(
        self,
        command: list[str],
        *,
        environment: dict[str, str] | None = None,
        label: str,
    ) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
        except OSError as error:
            raise RuntimeError(f"Could not start {label}: {error}") from error
        self._active_process = process
        try:
            _stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            raise
        finally:
            self._active_process = None
        if process.returncode:
            detail = stderr.decode("utf-8", errors="replace")[-2000:].strip()
            raise RuntimeError(
                f"{label} failed with exit code {process.returncode}"
                + (f": {detail}" if detail else ".")
            )

    async def _upload_to_drive(
        self, archive: Path, settings: BackupSettings
    ) -> tuple[str, list[str]]:
        raw = self.bot.config.get("backups", {})
        credentials = await asyncio.to_thread(
            GoogleDriveCredentials.load,
            raw if isinstance(raw, dict) else {},
            REPOSITORY_ROOT,
        )
        token_path = drive_token_path(raw)
        timeout = aiohttp.ClientTimeout(total=60, sock_connect=15, sock_read=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            drive = GoogleDriveClient(
                session, credentials, GoogleDriveTokenStore(token_path)
            )
            uploaded = await drive.upload_backup(archive, settings.drive_folder_id or "root")
            removed = await drive.prune_backups(
                settings.drive_folder_id or "root",
                retention_days=settings.retention_days,
                max_backups=settings.max_backups,
                max_storage_bytes=settings.max_storage_bytes,
            )
        return str(uploaded.get("id", "")), removed


def prune_local_backups(
    directory: Path,
    *,
    retention_days: int | None,
    max_backups: int | None,
    max_storage_bytes: int | None,
    now: float | None = None,
) -> list[str]:
    """Apply age, count, and aggregate-size caps, always oldest first."""
    if not directory.exists():
        return []
    candidates = []
    for path in directory.glob("fate-backup-*.zip"):
        try:
            if path.is_file() and not path.is_symlink():
                status = path.stat()
                candidates.append([path, status.st_mtime, status.st_size])
        except OSError:
            continue
    candidates.sort(key=lambda item: (item[1], item[0].name))
    current_time = datetime.now(timezone.utc).timestamp() if now is None else now
    remove: set[Path] = set()
    if retention_days is not None:
        cutoff = current_time - retention_days * 86400
        remove.update(item[0] for item in candidates if item[1] < cutoff)
    remaining = [item for item in candidates if item[0] not in remove]
    while max_backups is not None and len(remaining) > max_backups:
        remove.add(remaining.pop(0)[0])
    total = sum(item[2] for item in remaining)
    while max_storage_bytes is not None and total > max_storage_bytes and remaining:
        oldest = remaining.pop(0)
        total -= oldest[2]
        remove.add(oldest[0])
    removed: list[str] = []
    for path, _modified, _size in candidates:
        if path not in remove:
            continue
        try:
            path.unlink()
            removed.append(path.name)
        except OSError:
            continue
    return removed


def _ensure_directories(*paths: Path) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def _write_private_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


def _archive_local_files(
    source: Path,
    output: Path,
    backup_root: Path,
    excluded_paths: set[Path],
) -> None:
    source = source.resolve()
    backup_root = backup_root.resolve()
    excluded_names = {
        "fate-control.token",
        "google-drive-client.json",
        "google-drive-token.json",
    }
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
        if not source.is_dir():
            return
        for root, directories, files in os.walk(source):
            root_path = Path(root)
            directories[:] = [
                directory
                for directory in directories
                if not _is_within((root_path / directory).resolve(), backup_root)
                and directory not in {"__pycache__", ".staging"}
            ]
            for filename in files:
                path = root_path / filename
                if filename in excluded_names or filename.endswith((".bak", ".tmp")):
                    continue
                try:
                    if (
                        path.resolve() not in excluded_paths
                        and path.is_file()
                        and not path.is_symlink()
                    ):
                        archive.write(path, path.relative_to(source))
                except OSError:
                    continue


def _build_package(output: Path, members: list[Path]) -> None:
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
        for member in members:
            archive.write(member, member.name)


def _datastore_path(config: dict[str, Any]) -> Path:
    raw = str(config.get("datastore_location", "./data"))
    path = Path(os.path.expandvars(os.path.expanduser(raw)))
    return path if path.is_absolute() else REPOSITORY_ROOT / path


def drive_token_path(settings: dict[str, Any]) -> Path:
    drive = settings.get("google_drive", {}) if isinstance(settings, dict) else {}
    raw = drive.get("token_path") if isinstance(drive, dict) else None
    path = Path(os.path.expandvars(os.path.expanduser(str(raw or DEFAULT_TOKEN_PATH))))
    return path if path.is_absolute() else REPOSITORY_ROOT / path


def _backup_secret_paths(config: dict[str, Any]) -> set[Path]:
    settings = config.get("backups", {})
    if not isinstance(settings, dict):
        settings = {}
    drive = settings.get("google_drive", {})
    if not isinstance(drive, dict):
        drive = {}
    credentials = drive.get(
        "client_credentials_path", REPOSITORY_ROOT / "data" / "google-drive-client.json"
    )
    credentials_path = Path(os.path.expandvars(os.path.expanduser(str(credentials))))
    if not credentials_path.is_absolute():
        credentials_path = REPOSITORY_ROOT / credentials_path
    return {
        credentials_path.resolve(),
        drive_token_path(settings).resolve(),
        (REPOSITORY_ROOT / "data" / "fate-control.token").resolve(),
    }


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
