"""Bounded local persistence for Fate's Logging module."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
from collections import OrderedDict
from contextlib import suppress
from pathlib import Path
from time import time
from typing import Any, Callable

MAX_GUILD_BYTES = 1024**3
MAX_ATTACHMENT_BYTES = 25 * 1024**2
MAX_MESSAGE_ATTACHMENT_BYTES = 50 * 1024**2
MAX_PENDING_ATTACHMENT_BYTES = 256 * 1024**2
MAX_HOT_ATTACHMENT_BYTES = 128 * 1024**2
DEFAULT_RETENTION_DAYS = 30
MAX_RETENTION_DAYS = 365
FUZZY_MIN_TOKEN_LENGTH = 4
FUZZY_MAX_TOKEN_LENGTH = 24
FUZZY_MAX_QUERY_TOKENS = 6
FUZZY_MAX_VARIANTS = 6_000
FUZZY_MAX_CANDIDATE_IDS = 2_000
FUZZY_VARIANT_BATCH_SIZE = 192
_FUZZY_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789_"


def _encoded_size(*values: Any) -> int:
    """Estimate stored payload bytes, including a small row/index allowance."""
    size = 256
    for value in values:
        if isinstance(value, (bytes, bytearray, memoryview)):
            size += len(value)
        else:
            size += len(str(value or "").encode("utf-8"))
    return size


def _fts_tokens(query: str) -> list[str]:
    return [
        token
        for token in re.findall(r"\w+", query, flags=re.UNICODE)
        if any(character.isalnum() for character in token)
    ][:12]


def _fts_term(token: str, *, prefix: bool = False) -> str:
    escaped = token.replace('"', '""')
    return f'"{escaped}"' + ("*" if prefix else "")


def _single_edit_variants(token: str) -> list[str]:
    """Return bounded one-edit spelling variants for an FTS fallback."""
    if not FUZZY_MIN_TOKEN_LENGTH <= len(token) <= FUZZY_MAX_TOKEN_LENGTH:
        return []
    if not any(character.isalpha() for character in token):
        return []

    variants: dict[str, None] = {}

    def add(value: str) -> None:
        if value and value != token:
            variants.setdefault(value, None)

    for index in range(len(token)):
        add(token[:index] + token[index + 1 :])
    for index in range(len(token) - 1):
        if token[index] != token[index + 1]:
            add(
                token[:index]
                + token[index + 1]
                + token[index]
                + token[index + 2 :]
            )
    for index, original in enumerate(token):
        for replacement in _FUZZY_ALPHABET:
            if replacement != original:
                add(token[:index] + replacement + token[index + 1 :])
    for index in range(len(token) + 1):
        for insertion in _FUZZY_ALPHABET:
            add(token[:index] + insertion + token[index:])
    return list(variants)


class LocalLogArchive:
    """Write guild-isolated log records and message snapshots to SQLite."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        report_error: Callable[[str], None] | None = None,
        queue_size: int = 10_000,
        max_guild_bytes: int = MAX_GUILD_BYTES,
    ):
        self.path = Path(database_path)
        self.report_error = report_error or (lambda _message: None)
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self.task: asyncio.Task | None = None
        self.connection: sqlite3.Connection | None = None
        self.ready = asyncio.Event()
        self.max_guild_bytes = max(1, int(max_guild_bytes))
        self._database_lock = threading.RLock()
        self._hot_lock = threading.RLock()
        self._closing = False
        self._hot_messages: OrderedDict[
            tuple[str, str], dict[str, Any]
        ] = OrderedDict()
        self._hot_message_limit = max(queue_size * 2, 10_000)
        self._hot_file_bytes = 0
        self._pending_file_bytes = 0
        self._pending_lock = threading.RLock()
        self.fts_available = False

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> asyncio.Task:
        if self.task and not self.task.done():
            return self.task
        if self._closing:
            raise RuntimeError("The local Logging archive is closing")
        loop = loop or asyncio.get_running_loop()
        self.task = loop.create_task(self._writer(), name="logging:local-archive")
        return self.task

    async def close(self) -> None:
        if self._closing:
            if self.task:
                await asyncio.gather(self.task, return_exceptions=True)
            return
        self._closing = True
        if not self.task:
            await asyncio.to_thread(self._close_connection)
            self.ready.clear()
            return

        writer_task = self.task
        if not writer_task.done():
            sentinel_task = asyncio.create_task(
                self.queue.put(None), name="logging:local-archive-close"
            )
            done, _pending = await asyncio.wait(
                (writer_task, sentinel_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if writer_task in done and not sentinel_task.done():
                sentinel_task.cancel()
            await asyncio.gather(sentinel_task, return_exceptions=True)
        await asyncio.gather(writer_task, return_exceptions=True)
        self.task = None
        await asyncio.to_thread(self._close_connection)
        self.ready.clear()

    async def flush(self) -> None:
        if self.task:
            await self.queue.join()

    def enqueue_log(
        self,
        *,
        archive_key: str,
        guild_id: str,
        event_type: str,
        created_at: float,
        channel_id: int | None,
        channel_name: str | None,
        payload: dict[str, Any],
        search_text: str,
        retention_days: int,
        status: str = "pending",
    ) -> bool:
        payload_json = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), default=str
        )
        search_text = search_text.casefold()[:200_000]
        retention_days = min(max(int(retention_days), 1), MAX_RETENTION_DAYS)
        return self._enqueue(
            {
                "operation": "log",
                "archive_key": archive_key,
                "guild_id": str(guild_id),
                "event_type": event_type,
                "created_at": float(created_at),
                "channel_id": str(channel_id) if channel_id else None,
                "channel_name": channel_name,
                "payload_json": payload_json,
                "search_text": search_text,
                "status": status,
                "retention_days": retention_days,
                "expires_at": float(created_at) + retention_days * 86_400,
                "stored_bytes": _encoded_size(
                    archive_key,
                    guild_id,
                    event_type,
                    channel_id,
                    channel_name,
                    payload_json,
                    search_text,
                    search_text,
                    status,
                ),
            }
        )

    def update_delivery(
        self,
        archive_key: str,
        *,
        status: str,
        discord_message_id: int | None = None,
        jump_url: str | None = None,
    ) -> bool:
        return self._enqueue(
            {
                "operation": "delivery",
                "archive_key": archive_key,
                "status": status,
                "discord_message_id": (
                    str(discord_message_id) if discord_message_id else None
                ),
                "jump_url": jump_url,
            }
        )

    def enqueue_message(
        self,
        *,
        guild_id: str,
        message_id: int,
        channel_id: int,
        created_at: float,
        payload: dict[str, Any],
        retention_days: int,
        files: list[dict[str, Any]] | None = None,
    ) -> bool:
        if self._closing or not self.task or self.task.done():
            return False
        payload_json = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), default=str
        )
        stored_files = []
        stored_file_bytes = 0
        for item in files or []:
            data = item.get("data")
            if (
                not isinstance(data, bytes)
                or len(data) > MAX_ATTACHMENT_BYTES
                or stored_file_bytes + len(data) > MAX_MESSAGE_ATTACHMENT_BYTES
            ):
                continue
            stored_file_bytes += len(data)
            stored_files.append(
                {
                    "id": str(item.get("id") or ""),
                    "filename": str(item.get("filename") or "attachment")[:255],
                    "content_type": item.get("content_type"),
                    "data": data,
                }
            )
        key = (str(guild_id), str(message_id))
        hot_payload = json.loads(payload_json)
        if stored_files:
            hot_payload["stored_files"] = stored_files
        retention_days = min(max(int(retention_days), 1), MAX_RETENTION_DAYS)
        hot_payload["_archive_cached_at"] = float(created_at)
        hot_payload["_archive_expires_at"] = (
            float(created_at) + retention_days * 86_400
        )
        with self._hot_lock:
            previous = self._hot_messages.pop(key, None)
            if previous:
                self._hot_file_bytes -= self._stored_file_size(previous)
            self._hot_messages[key] = hot_payload
            self._hot_file_bytes += self._stored_file_size(hot_payload)
            self._hot_messages.move_to_end(key)
            while (
                len(self._hot_messages) > self._hot_message_limit
                or self._hot_file_bytes > MAX_HOT_ATTACHMENT_BYTES
            ):
                _old_key, old_payload = self._hot_messages.popitem(last=False)
                self._hot_file_bytes -= self._stored_file_size(old_payload)
        return self._enqueue(
            {
                "operation": "message",
                "guild_id": str(guild_id),
                "message_id": str(message_id),
                "channel_id": str(channel_id),
                "created_at": float(created_at),
                "payload_json": payload_json,
                "retention_days": retention_days,
                "expires_at": float(created_at) + retention_days * 86_400,
                "files": stored_files,
                "stored_bytes": _encoded_size(
                    guild_id,
                    message_id,
                    channel_id,
                    payload_json,
                ),
            }
        )

    def forget_messages(self, guild_id: str, message_ids) -> bool:
        """Forget consumed source-message snapshots in memory and on disk."""
        ids = list(dict.fromkeys(str(value) for value in message_ids))[:1_000]
        if not ids:
            return True
        guild_id = str(guild_id)
        with self._hot_lock:
            for message_id in ids:
                self._remove_hot((guild_id, message_id))
        return self._enqueue(
            {
                "operation": "delete_messages",
                "guild_id": guild_id,
                "message_ids": ids,
            }
        )

    async def search_logs(
        self,
        guild_id: str,
        *,
        before: int | str | None = None,
        limit: int = 50,
        query: str | None = None,
        channel_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        before_id = None
        forced_mode = None
        if before is not None:
            cursor = str(before)
            if ":" in cursor:
                candidate_mode, cursor = cursor.rsplit(":", 1)
                if candidate_mode not in {"exact", "prefix", "fuzzy"}:
                    raise ValueError("Invalid local Logging search cursor")
                forced_mode = candidate_mode
            if not cursor.isdigit():
                raise ValueError("Invalid local Logging search cursor")
            before_id = int(cursor)
        await self._wait_ready()
        return await asyncio.to_thread(
            self._search_logs,
            str(guild_id),
            before_id,
            min(max(limit, 1), 100),
            (query or "").strip().casefold()[:200],
            [str(value) for value in channel_ids] if channel_ids is not None else None,
            forced_mode,
        )

    async def get_message(
        self,
        guild_id: str,
        message_id: int,
        *,
        include_files: bool = False,
    ) -> dict[str, Any] | None:
        messages = await self.get_messages(
            guild_id, [message_id], include_files=include_files
        )
        return messages.get(str(message_id))

    async def get_messages(
        self,
        guild_id: str,
        message_ids,
        *,
        include_files: bool = False,
    ) -> dict[str, dict[str, Any]]:
        await self._wait_ready()
        guild_id = str(guild_id)
        ids = list(dict.fromkeys(str(value) for value in message_ids))[:1_000]
        if not ids:
            return {}
        hot = {}
        now = time()
        with self._hot_lock:
            for message_id in ids:
                key = (guild_id, message_id)
                cached = self._hot_messages.get(key)
                if cached is None:
                    continue
                if cached.get("_archive_expires_at", 0) <= now:
                    self._remove_hot(key)
                    continue
                cached = dict(cached)
                cached.pop("_archive_cached_at", None)
                cached.pop("_archive_expires_at", None)
                if not include_files:
                    cached.pop("stored_files", None)
                hot[message_id] = cached
        missing = [message_id for message_id in ids if message_id not in hot]
        stored = (
            await asyncio.to_thread(
                self._get_messages, guild_id, missing, include_files
            )
            if missing
            else {}
        )
        stored.update(hot)
        return stored

    async def stats(self, guild_id: str) -> dict[str, Any]:
        await self._wait_ready()
        return await asyncio.to_thread(self._stats, str(guild_id))

    async def clear_guild(self, guild_id: str) -> None:
        await self._wait_ready()
        await self.flush()
        guild_id = str(guild_id)
        with self._hot_lock:
            for key in [key for key in self._hot_messages if key[0] == guild_id]:
                self._remove_hot(key)
        await asyncio.to_thread(self._clear_guild, guild_id)

    async def prune_guild(self, guild_id: str, retention_days: int) -> None:
        await self._wait_ready()
        await self.flush()
        await asyncio.to_thread(
            self._enforce_limits,
            str(guild_id),
            retention_days,
        )
        guild_id = str(guild_id)
        retention_days = min(max(int(retention_days), 1), MAX_RETENTION_DAYS)
        cutoff = time() - retention_days * 86_400
        with self._hot_lock:
            for key, payload in list(self._hot_messages.items()):
                if key[0] != guild_id:
                    continue
                cached_at = payload.get("_archive_cached_at", 0)
                if cached_at < cutoff:
                    self._remove_hot(key)
                else:
                    payload["_archive_expires_at"] = (
                        cached_at + retention_days * 86_400
                    )

    def _enqueue(self, item: dict[str, Any]) -> bool:
        if self._closing or not self.task or self.task.done():
            return False
        pending_bytes = sum(
            len(file.get("data", b"")) for file in item.get("files", [])
        )
        item["_pending_file_bytes"] = pending_bytes
        with self._pending_lock:
            if self._pending_file_bytes + pending_bytes > MAX_PENDING_ATTACHMENT_BYTES:
                self.report_error(
                    "Local Logging file queue reached its memory safety limit; "
                    "attachment bodies were not queued"
                )
                return False
            self._pending_file_bytes += pending_bytes
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            with self._pending_lock:
                self._pending_file_bytes -= pending_bytes
            self.report_error("Local Logging archive queue is full; a record was dropped")
            return False
        return True

    async def _wait_ready(self) -> None:
        if not self.task:
            self.start()
        await self.ready.wait()

    async def _writer(self) -> None:
        try:
            await asyncio.to_thread(self._initialize)
            self.ready.set()
            stopping = False
            while True:
                try:
                    first = await asyncio.wait_for(self.queue.get(), timeout=3_600)
                except asyncio.TimeoutError:
                    await asyncio.to_thread(self._prune_expired)
                    continue
                if first is None:
                    self.queue.task_done()
                    break
                batch = [first]
                while len(batch) < 200:
                    try:
                        item = self.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if item is None:
                        self.queue.task_done()
                        stopping = True
                        break
                    batch.append(item)
                try:
                    for attempt in range(3):
                        try:
                            await asyncio.to_thread(self._write_batch, batch)
                            break
                        except sqlite3.OperationalError as error:
                            if attempt == 2 or not any(
                                word in str(error).casefold()
                                for word in ("busy", "locked")
                            ):
                                raise
                            await asyncio.sleep(0.05 * (attempt + 1))
                except Exception as error:
                    self.report_error(f"Local Logging archive write failed: {error}")
                finally:
                    for _item in batch:
                        with self._pending_lock:
                            self._pending_file_bytes -= _item.get(
                                "_pending_file_bytes", 0
                            )
                        self.queue.task_done()
                if stopping:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.report_error(f"Local Logging archive stopped: {error}")
            self.ready.set()

    def _initialize(self) -> None:
        with self._database_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(
                self.path, timeout=30, check_same_thread=False
            )
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=NORMAL")
            self.connection.execute("PRAGMA busy_timeout=30000")
            self.connection.execute("PRAGMA wal_autocheckpoint=1000")
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                archive_key TEXT NOT NULL UNIQUE,
                guild_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                channel_id TEXT,
                channel_name TEXT,
                payload_json TEXT NOT NULL,
                search_text TEXT NOT NULL,
                delivery_status TEXT NOT NULL,
                discord_message_id TEXT,
                jump_url TEXT,
                stored_bytes INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS logs_guild_created
                ON logs (guild_id, id DESC);
            CREATE TABLE IF NOT EXISTS message_cache (
                guild_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                payload_json TEXT NOT NULL,
                stored_bytes INTEGER NOT NULL,
                PRIMARY KEY (guild_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS messages_guild_created
                ON message_cache (guild_id, created_at);
            CREATE TABLE IF NOT EXISTS message_files (
                guild_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                attachment_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                content_type TEXT,
                data BLOB NOT NULL,
                stored_bytes INTEGER NOT NULL,
                PRIMARY KEY (guild_id, message_id, attachment_id),
                FOREIGN KEY (guild_id, message_id)
                    REFERENCES message_cache (guild_id, message_id)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS message_files_guild
                ON message_files (guild_id, message_id);
            """
            )
            for table in ("logs", "message_cache"):
                columns = {
                    row["name"]
                    for row in self.connection.execute(
                        f"PRAGMA table_info({table})"
                    ).fetchall()
                }
                if "expires_at" not in columns:
                    self.connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN expires_at REAL"
                    )
                    self.connection.execute(
                        f"""
                        UPDATE {table}
                        SET expires_at = created_at + ?
                        WHERE expires_at IS NULL
                        """,
                        (DEFAULT_RETENTION_DAYS * 86_400,),
                    )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS logs_guild_expires ON logs (guild_id, expires_at)"
            )
            self.connection.execute(
                """
                CREATE INDEX IF NOT EXISTS messages_guild_expires
                ON message_cache (guild_id, expires_at)
                """
            )
            self.connection.execute(
                """
                UPDATE logs SET delivery_status = 'uncertain'
                WHERE delivery_status = 'pending'
                """
            )
            fts_exists = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'logs_fts'"
            ).fetchone()
            try:
                self.connection.executescript(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS logs_fts USING fts5(
                        search_text,
                        content='logs',
                        content_rowid='id',
                        tokenize='unicode61 remove_diacritics 2'
                    );
                    CREATE TRIGGER IF NOT EXISTS logs_fts_insert
                    AFTER INSERT ON logs BEGIN
                        INSERT INTO logs_fts(rowid, search_text)
                        VALUES (new.id, new.search_text);
                    END;
                    CREATE TRIGGER IF NOT EXISTS logs_fts_delete
                    AFTER DELETE ON logs BEGIN
                        INSERT INTO logs_fts(logs_fts, rowid, search_text)
                        VALUES ('delete', old.id, old.search_text);
                    END;
                    """
                )
                if not fts_exists:
                    self.connection.execute(
                        "INSERT INTO logs_fts(logs_fts) VALUES ('rebuild')"
                    )
                self.fts_available = True
            except sqlite3.OperationalError as error:
                self.report_error(
                    f"Fast local Logging search is unavailable; using basic search: {error}"
                )
                self.fts_available = False
            self._prune_expired(commit=False)
            self.connection.commit()

    def _close_connection(self) -> None:
        with self._database_lock:
            if self.connection is not None:
                with suppress(sqlite3.Error):
                    self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self.connection.close()
                self.connection = None

    def _write_batch(self, batch: list[dict[str, Any]]) -> None:
        with self._database_lock:
            connection = self.connection
            if connection is None:
                raise RuntimeError("Archive database is not open")
            affected: dict[str, int] = {}
            with connection:
                for item in batch:
                    operation = item["operation"]
                    if operation == "log":
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO logs (
                            archive_key, guild_id, event_type, created_at,
                                expires_at, channel_id, channel_name, payload_json, search_text,
                                delivery_status, stored_bytes
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                item["archive_key"],
                                item["guild_id"],
                                item["event_type"],
                                item["created_at"],
                                item["expires_at"],
                                item["channel_id"],
                                item["channel_name"],
                                item["payload_json"],
                                item["search_text"],
                                item["status"],
                                item["stored_bytes"],
                            ),
                        )
                        affected[item["guild_id"]] = item["retention_days"]
                    elif operation == "delivery":
                        connection.execute(
                            """
                            UPDATE logs
                            SET delivery_status = ?, discord_message_id = ?, jump_url = ?
                            WHERE archive_key = ?
                            """,
                            (
                                item["status"],
                                item["discord_message_id"],
                                item["jump_url"],
                                item["archive_key"],
                            ),
                        )
                    elif operation == "message":
                        connection.execute(
                            """
                            INSERT INTO message_cache (
                                guild_id, message_id, channel_id, created_at,
                                expires_at, payload_json, stored_bytes
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(guild_id, message_id) DO UPDATE SET
                                channel_id = excluded.channel_id,
                                created_at = excluded.created_at,
                                expires_at = excluded.expires_at,
                                payload_json = excluded.payload_json,
                                stored_bytes = excluded.stored_bytes
                            """,
                            (
                                item["guild_id"],
                                item["message_id"],
                                item["channel_id"],
                                item["created_at"],
                                item["expires_at"],
                                item["payload_json"],
                                item["stored_bytes"],
                            ),
                        )
                        connection.execute(
                            "DELETE FROM message_files WHERE guild_id = ? AND message_id = ?",
                            (item["guild_id"], item["message_id"]),
                        )
                        for position, file in enumerate(item.get("files", [])):
                            attachment_id = file["id"] or f"position:{position}"
                            connection.execute(
                                """
                                INSERT INTO message_files (
                                    guild_id, message_id, attachment_id, filename,
                                    content_type, data, stored_bytes
                                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    item["guild_id"],
                                    item["message_id"],
                                    attachment_id,
                                    file["filename"],
                                    file["content_type"],
                                    file["data"],
                                    _encoded_size(
                                        attachment_id,
                                        file["filename"],
                                        file["content_type"],
                                        file["data"],
                                    ),
                                ),
                            )
                        affected[item["guild_id"]] = item["retention_days"]
                    elif operation == "delete_messages":
                        ids = item["message_ids"]
                        connection.execute(
                            f"""
                            DELETE FROM message_cache
                            WHERE guild_id = ?
                              AND message_id IN ({','.join('?' for _ in ids)})
                            """,
                            (item["guild_id"], *ids),
                        )
                for guild_id, retention_days in affected.items():
                    self._enforce_limits(guild_id, retention_days, commit=False)

    def _enforce_limits(
        self,
        guild_id: str,
        retention_days: int,
        *,
        commit: bool = True,
    ) -> None:
        with self._database_lock:
            connection = self.connection
            if connection is None:
                return
            retention_days = min(max(int(retention_days), 1), MAX_RETENTION_DAYS)
            cutoff = time() - retention_days * 86_400
            connection.execute(
                "UPDATE logs SET expires_at = created_at + ? WHERE guild_id = ?",
                (retention_days * 86_400, guild_id),
            )
            connection.execute(
                "UPDATE message_cache SET expires_at = created_at + ? WHERE guild_id = ?",
                (retention_days * 86_400, guild_id),
            )
            connection.execute(
            "DELETE FROM logs WHERE guild_id = ? AND created_at < ?",
            (guild_id, cutoff),
            )
            connection.execute(
            "DELETE FROM message_cache WHERE guild_id = ? AND created_at < ?",
            (guild_id, cutoff),
            )
            usage = connection.execute(
            """
            SELECT
                COALESCE((SELECT SUM(stored_bytes) FROM logs WHERE guild_id = ?), 0)
                + COALESCE((SELECT SUM(stored_bytes) FROM message_cache WHERE guild_id = ?), 0)
                + COALESCE((SELECT SUM(stored_bytes) FROM message_files WHERE guild_id = ?), 0)
            """,
            (guild_id, guild_id, guild_id),
            ).fetchone()[0]
            while usage > self.max_guild_bytes:
                oldest = connection.execute(
                """
                SELECT source, row_key, stored_bytes FROM (
                    SELECT 'log' AS source, CAST(id AS TEXT) AS row_key,
                           stored_bytes, created_at
                    FROM logs WHERE guild_id = ?
                    UNION ALL
                    SELECT 'message' AS source, message_id AS row_key,
                           stored_bytes + COALESCE((
                               SELECT SUM(message_files.stored_bytes)
                               FROM message_files
                               WHERE message_files.guild_id = message_cache.guild_id
                                 AND message_files.message_id = message_cache.message_id
                           ), 0) AS stored_bytes, created_at
                    FROM message_cache WHERE guild_id = ?
                ) ORDER BY created_at ASC LIMIT 500
                """,
                (guild_id, guild_id),
                ).fetchall()
                if not oldest:
                    break
                for row in oldest:
                    if row["source"] == "log":
                        connection.execute(
                            "DELETE FROM logs WHERE id = ?", (int(row["row_key"]),)
                        )
                    else:
                        connection.execute(
                            "DELETE FROM message_cache WHERE guild_id = ? AND message_id = ?",
                            (guild_id, row["row_key"]),
                        )
                        with self._hot_lock:
                            self._remove_hot((guild_id, row["row_key"]))
                    usage -= row["stored_bytes"]
                    if usage <= self.max_guild_bytes:
                        break
            if commit:
                connection.commit()

    def _search_logs(
        self,
        guild_id: str,
        before: int | None,
        limit: int,
        query: str,
        channel_ids: list[str] | None,
        forced_mode: str | None,
    ) -> dict[str, Any]:
        with self._database_lock:
            connection = self.connection
            if connection is None:
                return {
                    "entries": [],
                    "has_more": False,
                    "next_before": None,
                    "match_mode": "none" if query else "all",
                }
            clauses = ["logs.guild_id = ?"]
            parameters: list[Any] = [guild_id]
            if before:
                clauses.append("logs.id < ?")
                parameters.append(before)
            clauses.append("logs.expires_at > ?")
            parameters.append(time())
            if channel_ids is not None:
                if not channel_ids:
                    return {
                        "entries": [],
                        "has_more": False,
                        "next_before": None,
                        "match_mode": "none" if query else "all",
                    }
                clauses.append(
                    f"logs.channel_id IN ({','.join('?' for _ in channel_ids)})"
                )
                parameters.extend(channel_ids)

            def fetch_fts(match_query: str) -> list[sqlite3.Row]:
                return connection.execute(
                    f"""
                    SELECT logs.*
                    FROM logs JOIN logs_fts ON logs_fts.rowid = logs.id
                    WHERE {' AND '.join(clauses)} AND logs_fts MATCH ?
                    ORDER BY logs.id DESC LIMIT ?
                    """,
                    (*parameters, match_query, limit + 1),
                ).fetchall()

            def fetch_fts_ids(match_query: str) -> list[int]:
                return [
                    int(row["id"])
                    for row in connection.execute(
                        f"""
                        SELECT logs.id
                        FROM logs JOIN logs_fts ON logs_fts.rowid = logs.id
                        WHERE {' AND '.join(clauses)} AND logs_fts MATCH ?
                        ORDER BY logs.id DESC LIMIT ?
                        """,
                        (*parameters, match_query, limit + 1),
                    ).fetchall()
                ]

            tokens = _fts_tokens(query) if query else []
            rows: list[sqlite3.Row] = []
            match_mode = "all" if not query else "none"
            if not query:
                rows = connection.execute(
                    f"""
                    SELECT logs.* FROM logs
                    WHERE {' AND '.join(clauses)}
                    ORDER BY logs.id DESC LIMIT ?
                    """,
                    (*parameters, limit + 1),
                ).fetchall()
            elif self.fts_available and tokens:
                modes = (
                    [forced_mode]
                    if forced_mode is not None
                    else ["exact", "prefix", "fuzzy"]
                )
                for mode in modes:
                    if mode == "exact":
                        rows = fetch_fts(
                            " AND ".join(_fts_term(token) for token in tokens)
                        )
                    elif mode == "prefix":
                        rows = fetch_fts(
                            " AND ".join(
                                _fts_term(token, prefix=True) for token in tokens
                            )
                        )
                    elif mode == "fuzzy":
                        fuzzy_ids: set[int] = set()
                        eligible = []
                        remaining_variants = FUZZY_MAX_VARIANTS
                        for index, token in enumerate(tokens):
                            variants = _single_edit_variants(token)
                            if not variants:
                                continue
                            variants = variants[:remaining_variants]
                            if not variants:
                                break
                            eligible.append((index, variants))
                            remaining_variants -= len(variants)
                            if (
                                len(eligible) >= FUZZY_MAX_QUERY_TOKENS
                                or remaining_variants <= 0
                            ):
                                break
                        for fuzzy_index, variants in eligible:
                            for start in range(
                                0, len(variants), FUZZY_VARIANT_BATCH_SIZE
                            ):
                                batch = variants[
                                    start : start + FUZZY_VARIANT_BATCH_SIZE
                                ]
                                terms = []
                                for index, token in enumerate(tokens):
                                    if index == fuzzy_index:
                                        terms.append(
                                            "("
                                            + " OR ".join(
                                                _fts_term(value, prefix=True)
                                                for value in batch
                                            )
                                            + ")"
                                        )
                                    else:
                                        terms.append(_fts_term(token))
                                fuzzy_ids.update(fetch_fts_ids(" AND ".join(terms)))
                                if len(fuzzy_ids) > FUZZY_MAX_CANDIDATE_IDS:
                                    fuzzy_ids = set(
                                        sorted(fuzzy_ids, reverse=True)[
                                            :FUZZY_MAX_CANDIDATE_IDS
                                        ]
                                    )
                        selected_ids = sorted(fuzzy_ids, reverse=True)[: limit + 1]
                        if selected_ids:
                            rows = connection.execute(
                                f"""
                                SELECT logs.* FROM logs
                                WHERE logs.guild_id = ?
                                  AND logs.id IN ({','.join('?' for _ in selected_ids)})
                                ORDER BY logs.id DESC
                                """,
                                (guild_id, *selected_ids),
                            ).fetchall()
                    if rows:
                        match_mode = mode
                        break
            else:
                escaped = (
                    query.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                rows = connection.execute(
                    f"""
                    SELECT logs.* FROM logs
                    WHERE {' AND '.join(clauses)}
                      AND logs.search_text LIKE ? ESCAPE '\\'
                    ORDER BY logs.id DESC LIMIT ?
                    """,
                    (*parameters, f"%{escaped}%", limit + 1),
                ).fetchall()
                if rows:
                    match_mode = "exact"
        has_more = len(rows) > limit
        rows = rows[:limit]
        entries = []
        for row in rows:
            entry = dict(row)
            entry["payload"] = json.loads(entry.pop("payload_json"))
            entries.append(entry)
        return {
            "entries": entries,
            "has_more": has_more,
            "next_before": (
                (
                    f"{match_mode}:{rows[-1]['id']}"
                    if query
                    else rows[-1]["id"]
                )
                if has_more and rows
                else None
            ),
            "match_mode": match_mode,
        }

    def _get_messages(
        self,
        guild_id: str,
        message_ids: list[str],
        include_files: bool,
    ):
        if not message_ids:
            return {}
        with self._database_lock:
            connection = self.connection
            if connection is None:
                return {}
            rows = connection.execute(
            f"""
            SELECT message_id, payload_json FROM message_cache
            WHERE guild_id = ? AND expires_at > ?
              AND message_id IN ({','.join('?' for _ in message_ids)})
            """,
            (guild_id, time(), *message_ids),
            ).fetchall()
        messages = {
            row["message_id"]: json.loads(row["payload_json"]) for row in rows
        }
        if include_files and messages:
            file_rows = connection.execute(
                f"""
                SELECT message_id, attachment_id, filename, content_type, data
                FROM message_files
                WHERE guild_id = ?
                  AND message_id IN ({','.join('?' for _ in messages)})
                ORDER BY message_id, rowid
                """,
                (guild_id, *messages.keys()),
            ).fetchall()
            for row in file_rows:
                messages[row["message_id"]].setdefault("stored_files", []).append(
                    {
                        "id": row["attachment_id"],
                        "filename": row["filename"],
                        "content_type": row["content_type"],
                        "data": bytes(row["data"]),
                    }
                )
        return messages

    def _stats(self, guild_id: str) -> dict[str, Any]:
        with self._database_lock:
            connection = self.connection
            if connection is None:
                return {
                    "log_count": 0,
                    "message_count": 0,
                    "file_count": 0,
                    "stored_bytes": 0,
                    "oldest_at": None,
                    "newest_at": None,
                    "limit_bytes": self.max_guild_bytes,
                }
            self._prune_expired(commit=True)
            row = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM logs WHERE guild_id = ?) AS log_count,
                (SELECT COUNT(*) FROM message_cache WHERE guild_id = ?) AS message_count,
                (SELECT COUNT(*) FROM message_files WHERE guild_id = ?) AS file_count,
                COALESCE((SELECT SUM(stored_bytes) FROM logs WHERE guild_id = ?), 0)
                    + COALESCE((SELECT SUM(stored_bytes) FROM message_cache WHERE guild_id = ?), 0)
                    + COALESCE((SELECT SUM(stored_bytes) FROM message_files WHERE guild_id = ?), 0)
                    AS stored_bytes,
                MIN(
                    COALESCE((SELECT MIN(created_at) FROM logs WHERE guild_id = ?), 9e99),
                    COALESCE((SELECT MIN(created_at) FROM message_cache WHERE guild_id = ?), 9e99)
                ) AS oldest_at,
                MAX(
                    COALESCE((SELECT MAX(created_at) FROM logs WHERE guild_id = ?), 0),
                    COALESCE((SELECT MAX(created_at) FROM message_cache WHERE guild_id = ?), 0)
                ) AS newest_at
            """,
            (
                guild_id, guild_id, guild_id, guild_id, guild_id, guild_id,
                guild_id, guild_id, guild_id, guild_id,
            ),
            ).fetchone()
        oldest_at = row["oldest_at"]
        if oldest_at is not None and oldest_at >= 9e99:
            oldest_at = None
        return {
            "log_count": row["log_count"],
            "message_count": row["message_count"],
            "file_count": row["file_count"],
            "stored_bytes": row["stored_bytes"],
            "oldest_at": oldest_at,
            "newest_at": row["newest_at"] or None,
            "limit_bytes": self.max_guild_bytes,
        }

    def _clear_guild(self, guild_id: str) -> None:
        with self._database_lock:
            connection = self.connection
            if connection is None:
                return
            with connection:
                connection.execute("DELETE FROM logs WHERE guild_id = ?", (guild_id,))
                connection.execute("DELETE FROM message_files WHERE guild_id = ?", (guild_id,))
                connection.execute("DELETE FROM message_cache WHERE guild_id = ?", (guild_id,))
            with suppress(sqlite3.Error):
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("VACUUM")

    def _prune_expired(self, *, commit: bool = True) -> None:
        with self._database_lock:
            connection = self.connection
            if connection is None:
                return
            cutoff = time()
            connection.execute("DELETE FROM logs WHERE expires_at <= ?", (cutoff,))
            connection.execute(
                "DELETE FROM message_cache WHERE expires_at <= ?", (cutoff,)
            )
            with self._hot_lock:
                for key, payload in list(self._hot_messages.items()):
                    if payload.get("_archive_expires_at", 0) <= cutoff:
                        self._remove_hot(key)
            if commit:
                connection.commit()

    @staticmethod
    def _stored_file_size(payload: dict[str, Any]) -> int:
        return sum(
            len(file.get("data", b""))
            for file in payload.get("stored_files", [])
        )

    def _remove_hot(self, key: tuple[str, str]) -> None:
        with self._hot_lock:
            payload = self._hot_messages.pop(key, None)
            if payload:
                self._hot_file_bytes = max(
                    0, self._hot_file_bytes - self._stored_file_size(payload)
                )
