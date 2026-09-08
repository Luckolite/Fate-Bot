"""Persistent rolling uptime accounting for Fate."""

from __future__ import annotations

import json
import math
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

Timestamp = Union[datetime, float, int]


class UptimeTracker:
    """Record process sessions and calculate observed rolling availability."""

    SCHEMA_VERSION = 1

    def __init__(self, path: Union[str, Path], *, window_days: int = 30) -> None:
        if window_days <= 0:
            raise ValueError("window_days must be positive")
        self.path = Path(path)
        self.window_seconds = window_days * 24 * 60 * 60
        self.observed_since: Optional[float] = None
        self.sessions: list[dict[str, Optional[float]]] = []
        self._active_session: Optional[dict[str, Optional[float]]] = None
        self._write_lock = threading.Lock()
        self._load()

    @staticmethod
    def _timestamp(value: Timestamp) -> float:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            result = value.timestamp()
        elif isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("timestamp must be a datetime or number")
        else:
            result = float(value)
        if not math.isfinite(result):
            raise ValueError("timestamp must be finite")
        return result

    @staticmethod
    def _optional_timestamp(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return UptimeTracker._timestamp(value)
        except (TypeError, ValueError, OverflowError):
            return None

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return

        self.observed_since = self._optional_timestamp(data.get("observed_since"))
        raw_sessions = data.get("sessions", [])
        if not isinstance(raw_sessions, list):
            return
        for raw in raw_sessions:
            if not isinstance(raw, dict):
                continue
            started_at = self._optional_timestamp(raw.get("started_at"))
            last_seen_at = self._optional_timestamp(raw.get("last_seen_at"))
            ended_at = self._optional_timestamp(raw.get("ended_at"))
            if started_at is None or last_seen_at is None:
                continue
            last_seen_at = max(started_at, last_seen_at)
            if ended_at is not None:
                ended_at = max(started_at, ended_at)
            self.sessions.append(
                {
                    "started_at": started_at,
                    "last_seen_at": last_seen_at,
                    "ended_at": ended_at,
                }
            )

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        self.sessions = [
            session
            for session in self.sessions
            if (
                session["ended_at"]
                if session["ended_at"] is not None
                else session["last_seen_at"]
            ) >= cutoff
        ]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_name(f".{self.path.name}.tmp")
        payload = {
            "version": self.SCHEMA_VERSION,
            "observed_since": self.observed_since,
            "sessions": self.sessions,
        }
        temporary_path.write_text(
            json.dumps(payload, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary_path.replace(self.path)

    def start(self, started_at: Timestamp) -> None:
        """Start a session, closing any unclean prior session at its heartbeat."""
        with self._write_lock:
            now = self._timestamp(started_at)
            for session in self.sessions:
                if session["ended_at"] is None:
                    session["ended_at"] = max(
                        session["started_at"], session["last_seen_at"]
                    )
            if self.observed_since is None:
                self.observed_since = now
            else:
                self.observed_since = min(self.observed_since, now)
            self._active_session = {
                "started_at": now,
                "last_seen_at": now,
                "ended_at": None,
            }
            self.sessions.append(self._active_session)
            self._prune(now)
            self._save()

    def heartbeat(self, seen_at: Timestamp) -> None:
        """Persist the latest point at which this process was known to be alive."""
        with self._write_lock:
            if self._active_session is None:
                return
            now = self._timestamp(seen_at)
            self._active_session["last_seen_at"] = max(
                self._active_session["started_at"], now
            )
            self._prune(now)
            self._save()

    def stop(self, ended_at: Timestamp) -> None:
        """Close and persist the current session."""
        with self._write_lock:
            if self._active_session is None:
                return
            now = self._timestamp(ended_at)
            now = max(self._active_session["started_at"], now)
            self._active_session["last_seen_at"] = now
            self._active_session["ended_at"] = now
            self._active_session = None
            self._prune(now)
            self._save()

    def percentage(self, at: Timestamp) -> float:
        """Return observed online time as a percentage of the rolling window."""
        now = self._timestamp(at)
        if self.observed_since is None:
            return 100.0
        window_start = max(self.observed_since, now - self.window_seconds)
        observed_seconds = now - window_start
        if observed_seconds <= 0:
            return 100.0

        intervals = []
        for session in self.sessions:
            started_at = session["started_at"]
            if started_at is None:
                continue
            if session is self._active_session:
                ended_at = now
            else:
                ended_at = (
                    session["ended_at"]
                    if session["ended_at"] is not None
                    else session["last_seen_at"]
                )
            if ended_at is None:
                continue
            start = max(window_start, started_at)
            end = min(now, max(started_at, ended_at))
            if end > start:
                intervals.append((start, end))

        online_seconds = 0.0
        merged_end: Optional[float] = None
        for start, end in sorted(intervals):
            if merged_end is None or start > merged_end:
                online_seconds += end - start
                merged_end = end
            elif end > merged_end:
                online_seconds += end - merged_end
                merged_end = end
        return max(0.0, min(100.0, online_seconds / observed_seconds * 100))
