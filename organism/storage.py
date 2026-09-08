"""Crash-safe JSON storage for bounded organism state and evidence."""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import weakref
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator, TypeVar

STATE_SCHEMA = "organism.state.v5"
LEGACY_STATE_SCHEMA = "organism.state.v4"
T = TypeVar("T")
Migration = Callable[[dict], dict]
Validator = Callable[[dict], None]
ConditionalOperation = Callable[[dict], tuple[T, bool]]


class _ProcessPathLock:
    """Serialize same-process users before entering the platform file lock."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.depth = 0


_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: weakref.WeakValueDictionary[str, _ProcessPathLock] = (
    weakref.WeakValueDictionary()
)


def _resolved_path_key(path: Path) -> str:
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        resolved = path.absolute()
    return os.path.normcase(os.path.abspath(str(resolved)))


def _process_lock_for(path: Path) -> _ProcessPathLock:
    key = _resolved_path_key(path)
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = _ProcessPathLock()
            _PROCESS_LOCKS[key] = lock
        return lock


class StateStoreError(RuntimeError):
    """Raised when persistent state cannot be safely read or written."""


def _validate_payload(payload: dict) -> None:
    def valid_timestamp(value: object) -> bool:
        if not isinstance(value, str):
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return parsed.tzinfo is not None and parsed.utcoffset() is not None

    def valid_number(value: object) -> bool:
        return (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(float(value))
        )

    mapping_fields = ("states", "dominant_modes", "state_access", "learned")
    if any(not isinstance(payload.get(name), dict) for name in mapping_fields):
        raise StateStoreError("organism state has an invalid mapping structure")
    if not isinstance(payload.get("recent_observations"), dict):
        raise StateStoreError("organism observation history is invalid")
    if not isinstance(payload.get("used_authorizations"), list):
        raise StateStoreError("organism authorization history is invalid")
    if not isinstance(payload.get("evidence"), list):
        raise StateStoreError("organism evidence ledger is invalid")
    if payload.get("last_state_key") is not None and not isinstance(
        payload.get("last_state_key"), str
    ):
        raise StateStoreError("organism last_state_key is invalid")
    if not valid_timestamp(payload.get("updated_at")):
        raise StateStoreError("organism updated_at is invalid")
    for key_name in (
        "identity_key_id",
        "learning_key_id",
        "owner_key_id",
        "evidence_key_id",
        "state_integrity_key_id",
    ):
        if payload.get(key_name) is not None and not isinstance(
            payload.get(key_name), str
        ):
            raise StateStoreError(f"organism {key_name} is invalid")
    if payload.get("state_authentication") is not None and not isinstance(
        payload.get("state_authentication"), str
    ):
        raise StateStoreError("organism state authentication is invalid")
    if not isinstance(payload.get("learning_audience"), str):
        raise StateStoreError("organism learning audience is invalid")

    learned = payload["learned"]
    for name in ("cue_weights", "strategy_weights", "capacities"):
        if not isinstance(learned.get(name), dict):
            raise StateStoreError(f"organism learned {name} is invalid")
        if any(not valid_number(value) for value in learned[name].values()):
            raise StateStoreError(f"organism learned {name} values are invalid")
    revision = learned.get("weight_revision")
    if type(revision) is not int or revision < 0:
        raise StateStoreError("organism weight_revision is invalid")

    state_fields = {
        "activation",
        "pleasantness",
        "interaction_safety",
        "agency",
        "uncertainty",
        "load",
        "updated_at",
        "revision",
    }
    for state_key, state in payload["states"].items():
        if (
            not isinstance(state_key, str)
            or not isinstance(state, dict)
            or not state_fields <= set(state)
            or any(
                not valid_number(state.get(name))
                for name in state_fields - {"updated_at", "revision"}
            )
            or type(state.get("revision")) is not int
            or state["revision"] < 0
            or not valid_timestamp(state.get("updated_at"))
        ):
            raise StateStoreError("organism transient state entry is invalid")
    for state_key, mode in payload["dominant_modes"].items():
        if state_key not in payload["states"] or not isinstance(mode, str):
            raise StateStoreError("organism dominant mode entry is invalid")
    for state_key, accessed_at in payload["state_access"].items():
        if state_key not in payload["states"] or not valid_timestamp(accessed_at):
            raise StateStoreError("organism state access entry is invalid")
    for state_key, observations in payload["recent_observations"].items():
        if not isinstance(state_key, str) or not isinstance(observations, list):
            raise StateStoreError("organism recent observation entry is invalid")
        for observation in observations:
            if (
                not isinstance(observation, dict)
                or not isinstance(observation.get("event_key"), str)
                or (
                    observation.get("fingerprint") is not None
                    and not isinstance(observation.get("fingerprint"), str)
                )
            ):
                raise StateStoreError("organism observation fingerprint is invalid")

    for authorization in payload["used_authorizations"]:
        if (
            not isinstance(authorization, dict)
            or authorization.get("authority") not in {"learning", "owner"}
            or not isinstance(authorization.get("token"), str)
            or not valid_timestamp(authorization.get("expires_at"))
        ):
            raise StateStoreError("organism authorization token is invalid")

    evidence_fields = {
        "event_key",
        "event_fingerprint",
        "kind",
        "reason",
        "source",
        "authority",
        "subject_key",
        "scope_key",
        "actor_scoped",
        "global_calibration_eligible",
        "capability",
        "episode_key",
        "severity",
        "confidence",
        "learning_strength",
        "reward",
        "strategies",
        "features",
        "observed_at",
        "ingested_at",
        "expires_at",
        "retracts_event_key",
        "record_authentication",
    }
    for record in payload["evidence"]:
        if not isinstance(record, dict) or not evidence_fields <= set(record):
            raise StateStoreError("organism evidence record is invalid")
        if not all(
            isinstance(record.get(name), str)
            for name in (
                "event_key",
                "event_fingerprint",
                "kind",
                "reason",
                "source",
                "authority",
                "scope_key",
                "capability",
                "record_authentication",
            )
        ):
            raise StateStoreError("organism evidence labels are invalid")
        if record.get("authority") not in {"learning", "owner"}:
            raise StateStoreError("organism evidence authority is invalid")
        if any(
            not valid_number(record.get(name))
            for name in ("severity", "confidence", "learning_strength", "reward")
        ):
            raise StateStoreError("organism evidence weights are invalid")
        if any(
            not valid_timestamp(record.get(name))
            for name in ("observed_at", "ingested_at", "expires_at")
        ):
            raise StateStoreError("organism evidence timestamps are invalid")
        if (
            type(record.get("actor_scoped")) is not bool
            or type(record.get("global_calibration_eligible")) is not bool
        ):
            raise StateStoreError("organism evidence scope flags are invalid")
        if any(
            value is not None and not isinstance(value, str)
            for value in (
                record.get("subject_key"),
                record.get("episode_key"),
                record.get("retracts_event_key"),
            )
        ):
            raise StateStoreError("organism evidence links are invalid")
        if not isinstance(record.get("strategies"), list) or any(
            not isinstance(value, str) for value in record["strategies"]
        ):
            raise StateStoreError("organism evidence strategies are invalid")
        if not isinstance(record.get("features"), dict) or any(
            not valid_number(value) for value in record["features"].values()
        ):
            raise StateStoreError("organism evidence features are invalid")


def _validate_current_payload(payload: object) -> dict:
    if not isinstance(payload, dict) or payload.get("schema_version") != STATE_SCHEMA:
        raise StateStoreError("organism state uses an unsupported schema")
    _validate_payload(payload)
    return payload


def migrate_v4_to_v5(payload: dict) -> dict:
    """Convert the v4 observation history shape without authenticating it.

    The caller must authenticate the v4 document before calling this helper and
    must reseal the returned v5 document before returning it to ``read`` or
    ``update`` as a migration result.  Storage deliberately has no access to
    application keys.
    """
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != LEGACY_STATE_SCHEMA
    ):
        raise StateStoreError("organism migration requires a v4 state document")
    migrated = deepcopy(payload)
    observations = migrated.get("recent_observations")
    if not isinstance(observations, dict):
        raise StateStoreError("organism v4 observation history is invalid")
    normalized: dict[str, list[dict[str, str | None]]] = {}
    for state_key, entries in observations.items():
        if not isinstance(state_key, str) or not isinstance(entries, list):
            raise StateStoreError("organism v4 observation history is invalid")
        converted: list[dict[str, str | None]] = []
        for entry in entries:
            if isinstance(entry, str):
                converted.append({"event_key": entry, "fingerprint": None})
                continue
            if not isinstance(entry, dict) or not isinstance(
                entry.get("event_key"), str
            ):
                raise StateStoreError("organism v4 observation history is invalid")
            fingerprint = entry.get("fingerprint")
            if fingerprint is not None and not isinstance(fingerprint, str):
                raise StateStoreError("organism v4 observation history is invalid")
            converted.append(
                {"event_key": entry["event_key"], "fingerprint": fingerprint}
            )
        normalized[state_key] = converted
    migrated["recent_observations"] = normalized
    migrated["schema_version"] = STATE_SCHEMA
    return _validate_current_payload(migrated)


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    process_lock = _process_lock_for(path)
    with process_lock.lock:
        if process_lock.depth:
            process_lock.depth += 1
            try:
                yield
            finally:
                process_lock.depth -= 1
            return

        process_lock.depth = 1
        try:
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
        finally:
            process_lock.depth = 0


class JsonStateStore:
    def __init__(
        self,
        path: str | Path,
        validator: Validator = _validate_payload,
    ) -> None:
        if not callable(validator):
            raise TypeError("validator must be callable")
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.validator = validator
        self._uses_organism_schema = validator is _validate_payload

    def _validated_payload(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise StateStoreError("state document must be a JSON object")
        if self._uses_organism_schema and payload.get("schema_version") != STATE_SCHEMA:
            raise StateStoreError("organism state uses an unsupported schema")
        try:
            self.validator(payload)
        except StateStoreError:
            raise
        except Exception as error:
            raise StateStoreError(
                f"state document validation failed: {error}"
            ) from error
        return payload

    def _read_unlocked(
        self,
        fresh: Callable[[], dict],
        *,
        migrate: Migration | None = None,
        create_if_missing: bool = False,
    ) -> dict:
        if not self.path.exists():
            payload = self._validated_payload(fresh())
            if create_if_missing:
                self._write_unlocked(payload)
            return payload
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise StateStoreError(
                f"organism state is unreadable at {self.path}: {error}"
            ) from error
        if not isinstance(payload, dict):
            raise StateStoreError("state document must be a JSON object")
        if not self._uses_organism_schema:
            return self._validated_payload(payload)
        schema = payload.get("schema_version")
        if schema == STATE_SCHEMA:
            return self._validated_payload(payload)
        if schema != LEGACY_STATE_SCHEMA or migrate is None:
            raise StateStoreError("organism state uses an unsupported schema")
        try:
            migrated = migrate(deepcopy(payload))
        except StateStoreError:
            raise
        except Exception as error:
            raise StateStoreError(
                f"unable to migrate organism v4 state: {error}"
            ) from error
        migrated = self._validated_payload(migrated)
        self._write_unlocked(migrated)
        return migrated

    def _write_unlocked(self, payload: dict) -> None:
        self._validated_payload(payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
                # State is authenticated and not safely hand-editable. Compact,
                # deterministic JSON materially reduces encode and fsync cost for
                # bounded high-history documents while remaining inspectable.
                json.dump(
                    payload,
                    file,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        except (OSError, TypeError, ValueError) as error:
            raise StateStoreError(
                f"unable to persist organism state at {self.path}: {error}"
            ) from error
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold this store's process-safe and cross-process transaction lock.

        This is intentionally a narrow coordination primitive. Callers that
        need to serialize a larger operation with a small marker document can
        hold the marker lock while ordinary state stores perform their own
        nested atomic writes.
        """

        with _exclusive_lock(self.lock_path):
            yield

    def read(
        self,
        fresh: Callable[[], dict],
        *,
        migrate: Migration | None = None,
    ) -> dict:
        with _exclusive_lock(self.lock_path):
            return self._read_unlocked(fresh, migrate=migrate)

    def read_or_create(
        self,
        fresh: Callable[[], dict],
        *,
        migrate: Migration | None = None,
    ) -> dict:
        """Read existing state without rewriting it, creating it only if absent."""

        with _exclusive_lock(self.lock_path):
            return self._read_unlocked(
                fresh,
                migrate=migrate,
                create_if_missing=True,
            )

    def update(
        self,
        fresh: Callable[[], dict],
        operation: Callable[[dict], T],
        *,
        migrate: Migration | None = None,
    ) -> T:
        with _exclusive_lock(self.lock_path):
            payload = self._read_unlocked(fresh, migrate=migrate)
            result = operation(payload)
            self._write_unlocked(payload)
            return result

    def update_if_changed(
        self,
        fresh: Callable[[], dict],
        operation: ConditionalOperation[T],
        *,
        migrate: Migration | None = None,
    ) -> T:
        """Atomically persist only when ``operation`` reports a real mutation."""

        with _exclusive_lock(self.lock_path):
            payload = self._read_unlocked(fresh, migrate=migrate)
            result, changed = operation(payload)
            if type(changed) is not bool:
                raise StateStoreError("conditional state update must return a boolean")
            if changed:
                self._write_unlocked(payload)
            return result

    def replace(self, fresh: Callable[[], dict]) -> bool:
        """Atomically replace state without parsing the previous document."""
        with _exclusive_lock(self.lock_path):
            existed = self.path.exists()
            payload = self._validated_payload(fresh())
            self._write_unlocked(payload)
            return existed
