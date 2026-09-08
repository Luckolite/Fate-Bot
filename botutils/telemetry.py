"""Bounded runtime telemetry for Fate and FateControl.

The Discord bot records small numeric aggregates into a local SQLite database.
FateControl reads the same database to serve authenticated chart data.  No
Message text, query text, database names, credentials, and secret URL tokens are
never stored. Discord rate-limit records may retain channel and message IDs for
incident tracing.
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
import threading
from collections import defaultdict
from contextlib import suppress
from pathlib import Path
from time import monotonic, time
from typing import Any, Callable, Iterable, Mapping

from pymongo import monitoring

RECENT_BUCKET_SECONDS = 5
HISTORY_BUCKET_SECONDS = 300
RECENT_RETENTION_SECONDS = 26 * 60 * 60
HISTORY_RETENTION_SECONDS = 365 * 24 * 60 * 60
MAX_RECENT_ROWS = 400_000
MAX_HISTORY_ROWS = 2_000_000
MAX_PENDING_ROWS = 4_096
MAX_COMMAND_LENGTH = 96
MAX_DISCORD_ROUTE_LENGTH = 240
MAX_DATABASE_BYTES = 512 * 1024 * 1024
MAX_WAL_BYTES = 4 * 1024 * 1024
MAX_CHART_POINTS = 720

COUNTER_METRICS = frozenset(
    {
        "commands",
        "mongo_calls",
        "mysql_calls",
        "messages_sent",
        "antispam_triggers",
        "chatfilter_triggers",
        "uno_games",
        "uno_players",
        "connect_four_games",
        "tictactoe_games",
        "dashboard_signins",
        "logger_events",
        "selfrole_activity",
        "welcome_leave_messages",
        "autorole_activity",
        "discord_rate_limits",
        "discord_global_rate_limits",
        "discord_invalid_requests",
        "discord_api_blocks",
    }
)
GAUGE_METRICS = frozenset({"servers", "users", "active_servers"})
LEVEL_GAUGE_METRICS = frozenset({"active_servers"})
SUPPORTED_METRICS = COUNTER_METRICS | GAUGE_METRICS
DISCORD_ROUTE_METRICS = frozenset(
    {
        "discord_rate_limits",
        "discord_global_rate_limits",
        "discord_invalid_requests",
        "discord_api_blocks",
    }
)

WINDOWS: Mapping[str, tuple[int, int]] = {
    "1m": (60, 5),
    "5m": (5 * 60, 15),
    "15m": (15 * 60, 30),
    "1h": (60 * 60, 60),
    "6h": (6 * 60 * 60, 5 * 60),
    "12h": (12 * 60 * 60, 10 * 60),
    "24h": (24 * 60 * 60, 15 * 60),
    "7d": (7 * 24 * 60 * 60, 60 * 60),
    "2w": (14 * 24 * 60 * 60, 2 * 60 * 60),
    "30d": (30 * 24 * 60 * 60, 4 * 60 * 60),
    "2mo": (60 * 24 * 60 * 60, 8 * 60 * 60),
    "3mo": (90 * 24 * 60 * 60, 12 * 60 * 60),
    "6mo": (180 * 24 * 60 * 60, 24 * 60 * 60),
    "1y": (365 * 24 * 60 * 60, 48 * 60 * 60),
}

_COMMAND_CHARACTER = re.compile(r"[^a-z0-9 _-]+")
_DISCORD_ROUTE = re.compile(
    r"^(?:GET|POST|PUT|PATCH|DELETE|OTHER) /[a-z0-9_@:/.*-]+$"
)
_TABLES = frozenset({"telemetry_recent", "telemetry_history"})


def sanitize_command_name(value: Any) -> str | None:
    """Return a bounded registered-command label, never user-provided content."""
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.strip().lower().split())
    normalized = _COMMAND_CHARACTER.sub("", normalized)[:MAX_COMMAND_LENGTH].strip()
    return normalized or None


def sanitize_discord_route(value: Any) -> str | None:
    """Accept only normalized Discord endpoint labels, never query data."""
    if not isinstance(value, str):
        return None
    route = value.strip()[:MAX_DISCORD_ROUTE_LENGTH]
    if "?" in route or not _DISCORD_ROUTE.fullmatch(route):
        return None
    path_segments = route.split(" ", 1)[1].strip("/").split("/")
    if (
        path_segments
        and path_segments[0] in {"webhooks", "interactions"}
        and len(path_segments) >= 3
        and path_segments[2] != ":token"
    ):
        return None
    if (
        path_segments
        and path_segments[0] in {"invites", "templates"}
        and len(path_segments) >= 2
        and path_segments[1] != ":code"
    ):
        return None
    return route


def telemetry_path_for_config(
    config_path: Path,
    config: Mapping[str, Any] | None = None,
    *,
    repository_root: Path | None = None,
) -> Path:
    """Resolve the shared metrics path used by Fate and FateControl."""
    configured_path = os.getenv("FATE_TELEMETRY_PATH", "").strip()
    if configured_path:
        return Path(os.path.expandvars(os.path.expanduser(configured_path))).resolve()

    root = (repository_root or config_path.resolve().parent).resolve()
    configured_path = config.get("telemetry_path") if config else None
    if isinstance(configured_path, str) and configured_path.strip():
        path = Path(os.path.expandvars(os.path.expanduser(configured_path.strip())))
        if not path.is_absolute():
            path = root / path
        return path.resolve()

    datastore = config.get("datastore_location") if config else None
    if isinstance(datastore, str) and datastore.strip():
        directory = Path(os.path.expandvars(os.path.expanduser(datastore.strip())))
        if not directory.is_absolute():
            directory = root / directory
    else:
        directory = config_path.resolve().parent
    return directory.resolve() / f"telemetry-{config_path.stem}.sqlite3"


class TelemetryUnavailable(RuntimeError):
    """Raised internally when the local metrics store cannot be used."""


class TelemetryStore:
    """Small multi-process SQLite store with recent and 30-day rollups."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._last_prune = 0.0
        self._setup_lock = threading.Lock()
        self._initialized = False

    def _prepare_path(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_symlink():
                raise TelemetryUnavailable("telemetry database cannot be a symbolic link")
        except OSError as error:
            raise TelemetryUnavailable("telemetry path is unavailable") from error

    def _connect(self) -> sqlite3.Connection:
        self._prepare_path()
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=0.5,
                isolation_level=None,
            )
            connection.execute("PRAGMA busy_timeout = 500")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("PRAGMA temp_store = MEMORY")
            connection.execute(f"PRAGMA journal_size_limit = {MAX_WAL_BYTES}")
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            pages = max(1, MAX_DATABASE_BYTES // max(1, page_size))
            connection.execute(f"PRAGMA max_page_count = {pages}")
            return connection
        except (OSError, sqlite3.Error) as error:
            raise TelemetryUnavailable("telemetry database is unavailable") from error

    def ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._setup_lock:
            if self._initialized:
                return
            connection = self._connect()
            try:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS telemetry_recent (
                        bucket INTEGER NOT NULL,
                        metric TEXT NOT NULL,
                        dimension TEXT NOT NULL DEFAULT '',
                        kind TEXT NOT NULL CHECK(kind IN ('counter', 'gauge')),
                        value REAL NOT NULL,
                        PRIMARY KEY (bucket, metric, dimension)
                    );
                    CREATE INDEX IF NOT EXISTS telemetry_recent_metric_bucket
                        ON telemetry_recent(metric, bucket);
                    CREATE TABLE IF NOT EXISTS telemetry_history (
                        bucket INTEGER NOT NULL,
                        metric TEXT NOT NULL,
                        dimension TEXT NOT NULL DEFAULT '',
                        kind TEXT NOT NULL CHECK(kind IN ('counter', 'gauge')),
                        value REAL NOT NULL,
                        PRIMARY KEY (bucket, metric, dimension)
                    );
                    CREATE INDEX IF NOT EXISTS telemetry_history_metric_bucket
                        ON telemetry_history(metric, bucket);
                    """
                )
                if os.name != "nt":
                    os.chmod(self.path, 0o600)
            except (OSError, sqlite3.Error) as error:
                raise TelemetryUnavailable("telemetry schema is unavailable") from error
            finally:
                connection.close()
            self._initialized = True

    @staticmethod
    def _upsert_sql(table: str, kind: str) -> str:
        if table not in _TABLES or kind not in {"counter", "gauge"}:
            raise ValueError("invalid telemetry aggregate")
        update = (
            "value = value + excluded.value"
            if kind == "counter"
            else "value = excluded.value"
        )
        return (
            f"INSERT INTO {table} (bucket, metric, dimension, kind, value) "
            "VALUES (?, ?, ?, ?, ?) "
            f"ON CONFLICT(bucket, metric, dimension) DO UPDATE SET {update}, kind = excluded.kind"
        )

    def write_batch(
        self,
        rows: Mapping[tuple[str, int, str, str, str], float],
    ) -> None:
        if not rows:
            return
        self.ensure_schema()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            grouped: dict[tuple[str, str], list[tuple[int, str, str, str, float]]] = (
                defaultdict(list)
            )
            for (table, bucket, metric, dimension, kind), value in rows.items():
                if table not in _TABLES or metric not in SUPPORTED_METRICS:
                    continue
                if kind not in {"counter", "gauge"} or not math.isfinite(value):
                    continue
                grouped[(table, kind)].append(
                    (bucket, metric, dimension, kind, float(value))
                )
            for (table, kind), values in grouped.items():
                connection.executemany(self._upsert_sql(table, kind), values)
            connection.commit()
        except (OSError, sqlite3.Error) as error:
            with suppress(sqlite3.Error):
                connection.rollback()
            raise TelemetryUnavailable("telemetry write failed") from error
        finally:
            connection.close()

        now = monotonic()
        if now - self._last_prune >= 60:
            try:
                self.prune()
            except TelemetryUnavailable:
                # The aggregate write is already committed. A transient prune
                # lock must never make the collector replay and double-count it.
                pass
            else:
                self._last_prune = now

    def prune(self, *, now: int | None = None) -> None:
        self.ensure_schema()
        current = int(time() if now is None else now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for table, retention, maximum in (
                ("telemetry_recent", RECENT_RETENTION_SECONDS, MAX_RECENT_ROWS),
                ("telemetry_history", HISTORY_RETENTION_SECONDS, MAX_HISTORY_ROWS),
            ):
                connection.execute(
                    f"DELETE FROM {table} WHERE bucket < ?", (current - retention,)
                )
                count = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                overflow = count - maximum
                if overflow > 0:
                    connection.execute(
                        f"DELETE FROM {table} WHERE rowid IN "
                        f"(SELECT rowid FROM {table} ORDER BY bucket ASC LIMIT ?)",
                        (overflow,),
                    )
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except (OSError, sqlite3.Error) as error:
            with suppress(sqlite3.Error):
                connection.rollback()
            raise TelemetryUnavailable("telemetry pruning failed") from error
        finally:
            connection.close()

    def read_rows(
        self,
        metric: str,
        *,
        start: int,
        end: int,
        history: bool,
        dimension: str | None = None,
    ) -> list[tuple[int, str, str, float]]:
        if metric not in SUPPORTED_METRICS:
            raise ValueError("unsupported metric")
        self.ensure_schema()
        table = "telemetry_history" if history else "telemetry_recent"
        params: list[Any] = [metric, start, end]
        where = "metric = ? AND bucket >= ? AND bucket <= ?"
        if dimension is not None:
            where += " AND dimension = ?"
            params.append(dimension)
        connection = self._connect()
        try:
            rows = connection.execute(
                f"SELECT bucket, dimension, kind, value FROM {table} "
                f"WHERE {where} ORDER BY bucket ASC",
                params,
            ).fetchall()
            return [
                (int(bucket), str(label), str(kind), float(value))
                for bucket, label, kind, value in rows
            ]
        except (OSError, sqlite3.Error) as error:
            raise TelemetryUnavailable("telemetry read failed") from error
        finally:
            connection.close()

    def latest_gauge_before(
        self, metric: str, *, before: int, history: bool
    ) -> float | None:
        if metric not in GAUGE_METRICS:
            return None
        self.ensure_schema()
        table = "telemetry_history" if history else "telemetry_recent"
        connection = self._connect()
        try:
            row = connection.execute(
                f"SELECT value FROM {table} WHERE metric = ? AND bucket < ? "
                "ORDER BY bucket DESC LIMIT 1",
                (metric, before),
            ).fetchone()
            return float(row[0]) if row else None
        except (OSError, sqlite3.Error) as error:
            raise TelemetryUnavailable("telemetry read failed") from error
        finally:
            connection.close()

class TelemetryCollector:
    """Non-blocking in-process aggregator flushed by one daemon thread."""

    def __init__(
        self,
        store: TelemetryStore,
        *,
        flush_interval: float = 5.0,
        clock: Callable[[], float] = time,
    ) -> None:
        self.store = store
        self.flush_interval = max(0.25, float(flush_interval))
        self.clock = clock
        self._pending: dict[tuple[str, int, str, str, str], float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="fate-telemetry-writer",
            daemon=True,
        )
        self._thread.start()

    def close(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        self._thread = None
        self.flush()

    def _run(self) -> None:
        while not self._stop.wait(self.flush_interval):
            self.flush()

    @staticmethod
    def _bucket(timestamp: int, width: int) -> int:
        return timestamp - (timestamp % width)

    def _record(self, metric: str, dimension: str, kind: str, value: float) -> None:
        if metric not in SUPPORTED_METRICS or kind not in {"counter", "gauge"}:
            return
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return
        if not math.isfinite(numeric) or numeric < 0:
            return
        timestamp = int(self.clock())
        with self._lock:
            for table, width in (
                ("telemetry_recent", RECENT_BUCKET_SECONDS),
                ("telemetry_history", HISTORY_BUCKET_SECONDS),
            ):
                key = (table, self._bucket(timestamp, width), metric, dimension, kind)
                if key not in self._pending and len(self._pending) >= MAX_PENDING_ROWS:
                    continue
                if kind == "counter":
                    self._pending[key] = self._pending.get(key, 0.0) + numeric
                else:
                    self._pending[key] = numeric

    def increment(self, metric: str, amount: float = 1, *, dimension: str = "") -> None:
        if metric == "commands":
            command = sanitize_command_name(dimension)
            if command is None:
                return
            dimension = command
        elif metric in DISCORD_ROUTE_METRICS and dimension:
            route = sanitize_discord_route(dimension)
            if route is None:
                return
            dimension = route
        elif dimension:
            return
        self._record(metric, dimension, "counter", amount)

    def gauge(self, metric: str, value: float) -> None:
        if metric not in GAUGE_METRICS:
            return
        self._record(metric, "", "gauge", value)

    def flush(self) -> bool:
        with self._lock:
            if not self._pending:
                return True
            pending = self._pending
            self._pending = {}
        try:
            self.store.write_batch(pending)
        except (OSError, TelemetryUnavailable, sqlite3.Error) as error:
            self.last_error = str(error)
            with self._lock:
                for key, value in pending.items():
                    if key in self._pending:
                        if key[-1] == "counter":
                            self._pending[key] += value
                    elif len(self._pending) < MAX_PENDING_ROWS:
                        self._pending[key] = value
            return False
        self.last_error = None
        return True


class MongoTelemetryListener(monitoring.CommandListener):
    """Count application Mongo commands without retaining commands or payloads."""

    IGNORED_COMMANDS = frozenset(
        {"hello", "ismaster", "saslstart", "saslcontinue", "authenticate", "endsessions"}
    )

    def __init__(self, collector: TelemetryCollector) -> None:
        self.collector = collector

    def started(self, event: Any) -> None:
        command_name = str(getattr(event, "command_name", "")).lower()
        if command_name and command_name not in self.IGNORED_COMMANDS:
            self.collector.increment("mongo_calls")

    def succeeded(self, _event: Any) -> None:
        return None

    def failed(self, _event: Any) -> None:
        return None


class InstrumentedCursor:
    def __init__(self, cursor: Any, collector: TelemetryCollector) -> None:
        self._cursor = cursor
        self._collector = collector

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        self._collector.increment("mysql_calls")
        return await self._cursor.execute(*args, **kwargs)

    async def executemany(self, *args: Any, **kwargs: Any) -> Any:
        self._collector.increment("mysql_calls")
        return await self._cursor.executemany(*args, **kwargs)

    async def callproc(self, *args: Any, **kwargs: Any) -> Any:
        self._collector.increment("mysql_calls")
        return await self._cursor.callproc(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class InstrumentedCursorContext:
    def __init__(self, context: Any, collector: TelemetryCollector) -> None:
        self._context = context
        self._collector = collector

    async def _await_cursor(self) -> InstrumentedCursor:
        return InstrumentedCursor(await self._context, self._collector)

    def __await__(self):
        return self._await_cursor().__await__()

    async def __aenter__(self) -> InstrumentedCursor:
        cursor = await self._context.__aenter__()
        return InstrumentedCursor(cursor, self._collector)

    async def __aexit__(self, *args: Any) -> Any:
        return await self._context.__aexit__(*args)


class InstrumentedConnection:
    def __init__(self, connection: Any, collector: TelemetryCollector) -> None:
        self._connection = connection
        self._collector = collector

    def cursor(self, *args: Any, **kwargs: Any) -> InstrumentedCursorContext:
        return InstrumentedCursorContext(
            self._connection.cursor(*args, **kwargs), self._collector
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class InstrumentedAcquireContext:
    def __init__(self, context: Any, collector: TelemetryCollector) -> None:
        self._context = context
        self._collector = collector

    async def _await_connection(self) -> InstrumentedConnection:
        return InstrumentedConnection(await self._context, self._collector)

    def __await__(self):
        return self._await_connection().__await__()

    async def __aenter__(self) -> InstrumentedConnection:
        connection = await self._context.__aenter__()
        return InstrumentedConnection(connection, self._collector)

    async def __aexit__(self, *args: Any) -> Any:
        return await self._context.__aexit__(*args)


class InstrumentedPool:
    """Transparent aiomysql pool proxy that counts actual query calls."""

    def __init__(self, pool: Any, collector: TelemetryCollector) -> None:
        self._pool = pool
        self._collector = collector

    def acquire(self) -> InstrumentedAcquireContext:
        return InstrumentedAcquireContext(self._pool.acquire(), self._collector)

    def release(self, connection: Any) -> Any:
        raw = (
            connection._connection
            if isinstance(connection, InstrumentedConnection)
            else connection
        )
        return self._pool.release(raw)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pool, name)


def instrument_mysql_pool(pool: Any, collector: TelemetryCollector) -> InstrumentedPool:
    if isinstance(pool, InstrumentedPool):
        return pool
    return InstrumentedPool(pool, collector)


def merge_mongo_listeners(
    connection_args: Mapping[str, Any], listener: MongoTelemetryListener
) -> dict[str, Any]:
    """Append Fate's listener while preserving configured PyMongo listeners."""
    merged = dict(connection_args)
    configured = merged.get("event_listeners")
    if configured is None:
        existing: Iterable[Any] = ()
    elif isinstance(configured, (list, tuple)):
        existing = configured
    else:
        existing = (configured,)
    merged["event_listeners"] = [*existing, listener]
    return merged


def _display_number(value: float) -> int | float:
    rounded = round(value)
    return rounded if math.isclose(value, rounded, abs_tol=1e-9) else round(value, 3)


def _empty_metric_payload(
    metric: str,
    window: str,
    bucket_seconds: int,
    *,
    command: str | None,
    available: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "metric": metric,
        "window": window,
        "bucket_seconds": bucket_seconds,
        "points": [],
        "summary": {
            "current": 0,
            "total": 0,
            "change": 0,
            "change_percent": 0.0,
        },
        "available": available,
    }
    if metric == "commands":
        payload["command"] = command
        payload["top_commands"] = []
    elif metric in DISCORD_ROUTE_METRICS:
        payload["top_discord_routes"] = []
    return payload


def build_metric_payload(
    store: TelemetryStore,
    metric: str,
    window: str,
    *,
    command: str | None = None,
    bucket_seconds: int | None = None,
    now: int | None = None,
    top_limit: int = 8,
) -> dict[str, Any]:
    """Build one bounded chart payload for FateControl's authenticated API."""
    if metric not in SUPPORTED_METRICS:
        raise ValueError("unsupported metric")
    if window not in WINDOWS:
        raise ValueError("unsupported window")
    selected_command = sanitize_command_name(command) if command is not None else None
    if command is not None and metric != "commands":
        raise ValueError("command is only valid for the commands metric")
    if command is not None and selected_command is None:
        raise ValueError("invalid command")

    duration, default_bucket_seconds = WINDOWS[window]
    history = duration > RECENT_RETENTION_SECONDS
    if bucket_seconds is None:
        bucket_seconds = default_bucket_seconds
    if type(bucket_seconds) is not int:
        raise ValueError("bucket_seconds must be an integer")
    minimum_bucket = max(1, math.ceil(duration / MAX_CHART_POINTS))
    if bucket_seconds < minimum_bucket or bucket_seconds > duration:
        raise ValueError(
            f"bucket_seconds for {window} must be between {minimum_bucket} and {duration}"
        )
    sampled_at = int(time() if now is None else now)
    end_bucket = sampled_at - (sampled_at % bucket_seconds)
    start_bucket = end_bucket - duration + bucket_seconds
    point_buckets = list(range(start_bucket, end_bucket + 1, bucket_seconds))
    if len(point_buckets) > MAX_CHART_POINTS:
        point_buckets = point_buckets[-MAX_CHART_POINTS:]
        start_bucket = point_buckets[0]
    query_end = end_bucket + bucket_seconds - 1

    try:
        rows = store.read_rows(
            metric,
            start=start_bucket,
            end=query_end,
            history=history,
        )
        seed = (
            store.latest_gauge_before(metric, before=start_bucket, history=history)
            if metric in GAUGE_METRICS
            else None
        )
    except TelemetryUnavailable:
        payload = _empty_metric_payload(
            metric,
            window,
            bucket_seconds,
            command=selected_command,
            available=False,
        )
        payload["sampled_at"] = sampled_at
        return payload

    by_bucket: dict[int, float] = {}
    top_commands: list[dict[str, Any]] = []
    top_discord_routes: list[dict[str, Any]] = []
    if metric in GAUGE_METRICS:
        last_by_bucket: dict[int, float] = {}
        for timestamp, _dimension, _kind, value in rows:
            output_bucket = timestamp - (timestamp % bucket_seconds)
            last_by_bucket[output_bucket] = value
        current = seed
        previous = seed
        for bucket in point_buckets:
            if bucket in last_by_bucket:
                current = last_by_bucket[bucket]
            if current is not None:
                if metric in LEVEL_GAUGE_METRICS:
                    by_bucket[bucket] = current
                else:
                    # General population gauges show the signed activity
                    # observed in each bucket. The first sample establishes a
                    # baseline instead of looking like an influx of users.
                    by_bucket[bucket] = (
                        0.0 if previous is None else current - previous
                    )
                previous = current
    elif metric == "commands":
        command_buckets: dict[str, dict[int, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        command_totals: dict[str, float] = defaultdict(float)
        for timestamp, dimension, _kind, value in rows:
            if not dimension:
                continue
            output_bucket = timestamp - (timestamp % bucket_seconds)
            command_buckets[dimension][output_bucket] += value
            command_totals[dimension] += value

        selected_dimensions = (
            [selected_command] if selected_command is not None else list(command_buckets)
        )
        for dimension in selected_dimensions:
            for bucket, value in command_buckets.get(dimension, {}).items():
                by_bucket[bucket] = by_bucket.get(bucket, 0.0) + value

        overall_total = sum(command_totals.values())
        ranked = sorted(command_totals.items(), key=lambda item: (-item[1], item[0]))[
            : max(1, min(int(top_limit), 10))
        ]
        for name, count in ranked:
            command_points = [
                {
                    "timestamp": bucket,
                    "value": _display_number(command_buckets[name].get(bucket, 0.0)),
                }
                for bucket in point_buckets
            ]
            top_commands.append(
                {
                    "name": name,
                    "count": _display_number(count),
                    "share": round((count / overall_total) * 100, 2)
                    if overall_total
                    else 0.0,
                    "points": command_points,
                }
            )
    else:
        route_totals: dict[str, float] = defaultdict(float)
        for timestamp, dimension, _kind, value in rows:
            output_bucket = timestamp - (timestamp % bucket_seconds)
            by_bucket[output_bucket] = by_bucket.get(output_bucket, 0.0) + value
            if metric in DISCORD_ROUTE_METRICS and dimension:
                route_totals[dimension] += value
        if metric in DISCORD_ROUTE_METRICS:
            overall_total = sum(route_totals.values())
            ranked_routes = sorted(
                route_totals.items(), key=lambda item: (-item[1], item[0])
            )[: max(1, min(int(top_limit), 10))]
            top_discord_routes = [
                {
                    "route": route,
                    "count": _display_number(count),
                    "share": round((count / overall_total) * 100, 2)
                    if overall_total
                    else 0.0,
                }
                for route, count in ranked_routes
            ]

    if metric in GAUGE_METRICS:
        points = [
            {"timestamp": bucket, "value": _display_number(by_bucket[bucket])}
            for bucket in point_buckets
            if bucket in by_bucket
        ]
    else:
        points = [
            {
                "timestamp": bucket,
                "value": _display_number(by_bucket.get(bucket, 0.0)),
            }
            for bucket in point_buckets
        ]

    values = [float(point["value"]) for point in points]
    current_value = values[-1] if values else 0.0
    first_value = values[0] if values else 0.0
    change = current_value - first_value
    if first_value:
        change_percent: float | None = round((change / first_value) * 100, 2)
    elif change:
        change_percent = None
    else:
        change_percent = 0.0
    total_value = sum(values)

    payload = {
        "metric": metric,
        "window": window,
        "bucket_seconds": bucket_seconds,
        "points": points,
        "summary": {
            "current": _display_number(current_value),
            "total": _display_number(total_value),
            "change": _display_number(change),
            "change_percent": change_percent,
        },
        "available": True,
        "sampled_at": sampled_at,
    }
    if metric == "commands":
        payload["command"] = selected_command
        payload["top_commands"] = top_commands
    elif metric in DISCORD_ROUTE_METRICS:
        payload["top_discord_routes"] = top_discord_routes
    return payload
