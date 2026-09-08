"""Cooperative single-instance handoff for Fate bot processes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import tempfile
from contextlib import contextmanager
from pathlib import Path
from time import monotonic, sleep, time
from typing import Awaitable, Callable, Iterator

import psutil

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
INSTANCE_DIRECTORY = REPOSITORY_ROOT / "data" / "instances"


def _read_json(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(payload, file, separators=(",", ":"))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as file:
        file.seek(0, os.SEEK_END)
        if file.tell() == 0:
            file.write(b"\0")
            file.flush()
        file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


class InstanceHandoff:
    """Let the newest process replace an older instance cleanly.

    The lease is scoped to the selected config file, allowing the intentionally
    separate primary and test/secondary profiles to run at the same time.
    """

    def __init__(
        self,
        config_path: Path,
        *,
        runtime_directory: Path | None = None,
        pid: int | None = None,
        process_started_at: float | None = None,
    ) -> None:
        canonical = config_path.resolve()
        identity = hashlib.sha256(str(canonical).casefold().encode()).hexdigest()[:16]
        directory = runtime_directory or INSTANCE_DIRECTORY
        stem = f"fate-instance-{identity}"
        self.owner_path = directory / f"{stem}.json"
        self.request_path = directory / f"{stem}.handoff.json"
        self.lock_path = directory / f"{stem}.lock"
        self.pid = pid if pid is not None else os.getpid()
        self.process_started_at = (
            process_started_at
            if process_started_at is not None
            else psutil.Process(self.pid).create_time()
        )
        self.nonce = secrets.token_hex(16)
        self.replacement_requested = False
        self._claimed = False

    @staticmethod
    def _process_matches(pid: object, started_at: object) -> bool:
        if type(pid) is not int or not isinstance(started_at, (int, float)):
            return False
        try:
            process = psutil.Process(pid)
            return process.is_running() and abs(process.create_time() - started_at) < 1.0
        except (psutil.Error, OSError):
            return False

    def _record(self) -> dict:
        return {
            "pid": self.pid,
            "process_started_at": self.process_started_at,
            "nonce": self.nonce,
            "claimed_at": time(),
        }

    def claim(self, *, timeout: float = 45.0, poll_interval: float = 0.25) -> bool:
        """Claim the profile, requesting graceful exit from its current owner."""
        deadline = monotonic() + timeout
        requested_owner: str | None = None
        while True:
            with _exclusive_lock(self.lock_path):
                owner = _read_json(self.owner_path)
                if owner and owner.get("nonce") == self.nonce:
                    self._claimed = True
                    return False
                if not owner or not self._process_matches(
                    owner.get("pid"), owner.get("process_started_at")
                ):
                    _write_json(self.owner_path, self._record())
                    self._claimed = True
                    return requested_owner is not None

                owner_nonce = owner.get("nonce")
                if not isinstance(owner_nonce, str):
                    self.owner_path.unlink(missing_ok=True)
                    continue
                requested_owner = owner_nonce
                _write_json(
                    self.request_path,
                    {
                        "target_nonce": owner_nonce,
                        "requester_pid": self.pid,
                        "requester_started_at": self.process_started_at,
                        "requester_nonce": self.nonce,
                        "requested_at": time(),
                    },
                )

            if monotonic() >= deadline:
                raise TimeoutError(
                    f"The existing Fate instance (PID {owner.get('pid')}) did not exit "
                    f"within {timeout:.0f} seconds."
                )
            sleep(poll_interval)

    async def watch_for_replacement(
        self,
        callback: Callable[[dict], Awaitable[None]],
        *,
        poll_interval: float = 0.5,
    ) -> None:
        """Call ``callback`` when a live newer process targets this lease."""
        while self._claimed:
            request = _read_json(self.request_path)
            if (
                request
                and request.get("target_nonce") == self.nonce
                and request.get("requester_nonce") != self.nonce
                and self._process_matches(
                    request.get("requester_pid"), request.get("requester_started_at")
                )
            ):
                self.replacement_requested = True
                await callback(request)
                return
            await asyncio.sleep(poll_interval)

    def release(self) -> None:
        if not self._claimed:
            return
        with _exclusive_lock(self.lock_path):
            owner = _read_json(self.owner_path)
            if owner and owner.get("nonce") == self.nonce:
                self.owner_path.unlink(missing_ok=True)
            request = _read_json(self.request_path)
            if request and request.get("target_nonce") == self.nonce:
                self.request_path.unlink(missing_ok=True)
        self._claimed = False
