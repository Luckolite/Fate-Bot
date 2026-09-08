"""Async Fate boundary for per-guild organism memory sectors."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
import secrets
import stat
import tempfile
import threading
from collections import OrderedDict
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping, TypeVar

import psutil

from .body import (
    OperationalInteroception,
    SensorFrame,
    memory_constraint_signal,
)
from .config import OrganismConfig
from .discovery import (
    CuriosityStimulus,
    DiscoveryEngine,
    DiscoverySettings,
    DiscoveryTopic,
    Dispatch,
    InquirySignal,
    Receipt,
    ReceiptStatus,
    StructuredReview,
    discovery_cortex_developer_message,
)
from .experience import (
    ExperienceMoment,
    cortex_developer_message,
    integrate_experience,
    person_developer_message,
)
from .models import Capability, FeelingPacket, LearningReceipt, ensure_utc
from .prompt import validate_user_text
from .sectors import (
    AssociationReview,
    FeelingWithCues,
    MemorySectorRegistry,
    PrivacyDeletionSupersededError,
    SensoryReference,
)
from .storage import StateStoreError


class OrganismServiceConfigurationError(RuntimeError):
    pass


class OrganismServiceBusyError(RuntimeError):
    """Raised before queuing work when the bounded worker admission is full."""


class OrganismServiceStaleOperationError(RuntimeError):
    """Raised when a privacy deletion supersedes already-queued sensing work."""


class OrganismDiscoveryDisabledError(RuntimeError):
    """Raised before admission when explicit discovery sensing is disabled."""


_WorkerResult = TypeVar("_WorkerResult")
_MAX_REPLAY_EVENTS_PER_SLOT = 8


def sample_operational_interoception() -> OperationalInteroception:
    """Read bounded host-memory pressure for the software body's current frame."""
    try:
        memory = psutil.virtual_memory()
        pressure = max(0.0, min(1.0, float(memory.percent) / 100.0))
    except (psutil.Error, OSError, TypeError, ValueError):
        # Missing telemetry is unknown, not an invented emergency.
        return OperationalInteroception()
    return OperationalInteroception(
        memory_pressure=pressure,
        energy_reserve=1.0 - memory_constraint_signal(pressure),
    )


def merge_operational_interoception(
    supplied: OperationalInteroception,
    sampled: OperationalInteroception,
) -> OperationalInteroception:
    """Preserve explicit pressures while ensuring live resource limits are sensed."""
    if not isinstance(supplied, OperationalInteroception) or not isinstance(
        sampled, OperationalInteroception
    ):
        raise TypeError("operational inputs must be OperationalInteroception values")
    pressure_fields = (
        "latency_pressure",
        "queue_pressure",
        "rate_limit_pressure",
        "compute_pressure",
        "memory_pressure",
        "error_pressure",
    )
    values = {
        name: max(getattr(supplied, name), getattr(sampled, name))
        for name in pressure_fields
    }
    values["energy_reserve"] = min(
        supplied.energy_reserve,
        sampled.energy_reserve,
    )
    return OperationalInteroception(**values)


def _boolean(value: object, *, name: str) -> bool:
    if type(value) is not bool:
        raise OrganismServiceConfigurationError(f"{name} must be true or false")
    return value


def _resolved_path(value: str, *, root: Path, name: str) -> Path:
    expanded = Path(os.path.expandvars(os.path.expanduser(value)))
    if not expanded.is_absolute():
        expanded = root / expanded
    resolved = _checked_path(expanded, name=name)
    return resolved


def _checked_path(path: Path, *, name: str) -> Path:
    absolute = Path(os.path.abspath(path))
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    for component in (absolute, *absolute.parents):
        try:
            metadata = component.lstat()
        except OSError:
            continue
        if component.is_symlink() or (
            reparse_flag and getattr(metadata, "st_file_attributes", 0) & reparse_flag
        ):
            raise OrganismServiceConfigurationError(
                f"{name} cannot contain a symbolic link or filesystem junction"
            )
    return absolute.resolve()


def _secret(
    name: str,
    environment: Mapping[str, str],
) -> bytes:
    inline = environment.get(name, "").strip()
    path_value = environment.get(f"{name}_PATH", "").strip()
    if inline and path_value:
        raise OrganismServiceConfigurationError(
            f"configure only one of {name} or {name}_PATH"
        )
    if path_value:
        path = _checked_path(
            Path(os.path.expandvars(os.path.expanduser(path_value))),
            name=f"{name}_PATH",
        )
        try:
            inline = path.read_text(encoding="utf-8-sig").strip()
        except OSError as error:
            raise OrganismServiceConfigurationError(
                f"unable to read the secret configured by {name}_PATH"
            ) from error
    encoded = inline.encode("utf-8")
    if len(encoded) < 32:
        raise OrganismServiceConfigurationError(
            f"{name} must provide a stable secret of at least 32 bytes"
        )
    return encoded


@dataclass(frozen=True)
class OrganismServiceSettings:
    enabled: bool
    state_directory: Path
    config_path: Path | None
    cross_guild_associations: bool
    max_cached_sectors: int
    max_pending_operations: int
    max_pending_per_guild: int
    discovery: DiscoverySettings = field(default_factory=DiscoverySettings)

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, object] | None,
        *,
        repository_root: str | Path,
    ) -> "OrganismServiceSettings":
        root = Path(repository_root).resolve()
        if values is not None and not isinstance(values, Mapping):
            raise OrganismServiceConfigurationError(
                "organism configuration must be an object"
            )
        data = dict(values or {})
        allowed = {
            "enabled",
            "state_directory",
            "config_path",
            "cross_guild_associations",
            "max_cached_sectors",
            "max_pending_operations",
            "max_pending_per_guild",
            "discovery",
        }
        unknown = set(data) - allowed
        if unknown:
            raise OrganismServiceConfigurationError(
                "unknown organism configuration fields: " + ", ".join(sorted(unknown))
            )
        enabled = _boolean(data.get("enabled", False), name="organism.enabled")
        associations = _boolean(
            data.get("cross_guild_associations", False),
            name="organism.cross_guild_associations",
        )
        directory_value = data.get("state_directory", "./data/organism")
        if not isinstance(directory_value, str) or not directory_value.strip():
            raise OrganismServiceConfigurationError(
                "organism.state_directory must be a non-empty path"
            )
        state_directory = _resolved_path(
            directory_value,
            root=root,
            name="organism.state_directory",
        )
        config_value = data.get("config_path")
        if config_value is None:
            config_path = None
        elif isinstance(config_value, str) and config_value.strip():
            config_path = _resolved_path(
                config_value,
                root=root,
                name="organism.config_path",
            )
        else:
            raise OrganismServiceConfigurationError(
                "organism.config_path must be null or a non-empty path"
            )
        max_cached = data.get("max_cached_sectors", 128)
        if isinstance(max_cached, bool) or not isinstance(max_cached, int):
            raise OrganismServiceConfigurationError(
                "organism.max_cached_sectors must be an integer"
            )
        if max_cached < 1 or max_cached > 4096:
            raise OrganismServiceConfigurationError(
                "organism.max_cached_sectors must be between 1 and 4096"
            )
        max_pending = data.get("max_pending_operations", 64)
        if isinstance(max_pending, bool) or not isinstance(max_pending, int):
            raise OrganismServiceConfigurationError(
                "organism.max_pending_operations must be an integer"
            )
        if max_pending < 1 or max_pending > 4096:
            raise OrganismServiceConfigurationError(
                "organism.max_pending_operations must be between 1 and 4096"
            )
        max_pending_per_guild = data.get("max_pending_per_guild", 8)
        if isinstance(max_pending_per_guild, bool) or not isinstance(
            max_pending_per_guild, int
        ):
            raise OrganismServiceConfigurationError(
                "organism.max_pending_per_guild must be an integer"
            )
        if not 1 <= max_pending_per_guild <= max_pending:
            raise OrganismServiceConfigurationError(
                "organism.max_pending_per_guild must be between 1 and "
                "max_pending_operations"
            )
        try:
            discovery = DiscoverySettings.from_mapping(data.get("discovery"))
        except ValueError as error:
            raise OrganismServiceConfigurationError(str(error)) from error
        if discovery.enabled and not enabled:
            raise OrganismServiceConfigurationError(
                "organism.discovery.enabled requires organism.enabled"
            )
        return cls(
            enabled=enabled,
            state_directory=state_directory,
            config_path=config_path,
            cross_guild_associations=associations,
            max_cached_sectors=max_cached,
            max_pending_operations=max_pending,
            max_pending_per_guild=max_pending_per_guild,
            discovery=discovery,
        )


@dataclass(frozen=True)
class _ExperienceReplayRecord:
    """One opaque, short-lived result used to make event retries idempotent."""

    event_token: str
    input_fingerprint: str
    packet: FeelingPacket
    moment: ExperienceMoment
    generated_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class _ExperienceCacheEntry:
    sector_id: str
    records: tuple[_ExperienceReplayRecord, ...]

    @property
    def latest(self) -> _ExperienceReplayRecord:
        return self.records[-1]

    # These compatibility projections keep cache inspection small and intuitive.
    @property
    def event_token(self) -> str:
        return self.latest.event_token

    @property
    def packet(self) -> FeelingPacket:
        return self.latest.packet

    @property
    def moment(self) -> ExperienceMoment:
        return self.latest.moment

    @property
    def generated_at(self) -> datetime:
        return self.latest.generated_at

    @property
    def expires_at(self) -> datetime:
        return self.latest.expires_at

    def replay(self, event_token: str) -> _ExperienceReplayRecord | None:
        return next(
            (
                record
                for record in reversed(self.records)
                if hmac.compare_digest(record.event_token, event_token)
            ),
            None,
        )


class OrganismService:
    """Run synchronous organism stores outside Discord's event loop."""

    def __init__(
        self,
        registry: MemorySectorRegistry,
        *,
        discovery: DiscoveryEngine | None = None,
        operational_sensor: Callable[[], OperationalInteroception] = (
            sample_operational_interoception
        ),
        experience_cache_ttl_seconds: float = 120.0,
        max_experience_cache_entries: int = 512,
        max_pending_operations: int = 64,
        max_pending_per_guild: int = 8,
    ) -> None:
        if not isinstance(registry, MemorySectorRegistry):
            raise TypeError("registry must be a MemorySectorRegistry")
        if discovery is not None and not isinstance(discovery, DiscoveryEngine):
            raise TypeError("discovery must be a DiscoveryEngine or None")
        if not callable(operational_sensor):
            raise TypeError("operational_sensor must be callable")
        if (
            isinstance(experience_cache_ttl_seconds, bool)
            or not isinstance(experience_cache_ttl_seconds, (int, float))
            or not math.isfinite(float(experience_cache_ttl_seconds))
            or not 0.0 < float(experience_cache_ttl_seconds) <= 3600.0
        ):
            raise ValueError(
                "experience_cache_ttl_seconds must be finite and between 0 and 3600"
            )
        if (
            isinstance(max_experience_cache_entries, bool)
            or not isinstance(max_experience_cache_entries, int)
            or not 1 <= max_experience_cache_entries <= 4096
        ):
            raise ValueError("max_experience_cache_entries must be between 1 and 4096")
        if (
            isinstance(max_pending_operations, bool)
            or not isinstance(max_pending_operations, int)
            or not 1 <= max_pending_operations <= 4096
        ):
            raise ValueError("max_pending_operations must be between 1 and 4096")
        if (
            isinstance(max_pending_per_guild, bool)
            or not isinstance(max_pending_per_guild, int)
            or not 1 <= max_pending_per_guild <= max_pending_operations
        ):
            raise ValueError(
                "max_pending_per_guild must be between 1 and max_pending_operations"
            )
        self.registry = registry
        self.discovery = discovery
        self._operational_sensor = operational_sensor
        self._experience_cache_ttl = timedelta(
            seconds=float(experience_cache_ttl_seconds)
        )
        self._max_experience_cache_entries = max_experience_cache_entries
        self._experience_cache: OrderedDict[str, _ExperienceCacheEntry] = OrderedDict()
        self._experience_cache_lock = threading.RLock()
        # Request fingerprints are only for this process-local, short-lived cache.
        # A fresh key prevents the cache from retaining reversible caller payloads.
        self._experience_request_key = secrets.token_bytes(32)
        self._experience_slot_locks = tuple(threading.RLock() for _ in range(64))
        self._association_lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._core_lifecycle_revision = 0
        self._discovery_lifecycle_revision = 0
        self._max_pending_operations = max_pending_operations
        self._max_pending_per_guild = max_pending_per_guild
        self._admission_lock = threading.Lock()
        self._pending_operations = 0
        self._pending_by_sector: dict[str, int] = {}

    @property
    def associations_enabled(self) -> bool:
        return self.registry.associations_enabled

    @property
    def discovery_enabled(self) -> bool:
        return self.discovery is not None and self.discovery.settings.enabled

    @property
    def discovery_control_available(self) -> bool:
        """Whether trusted inspection and erasure have a backing store."""

        return self.discovery is not None

    @property
    def experience_cache_size(self) -> int:
        with self._experience_cache_lock:
            return len(self._experience_cache)

    @property
    def pending_operation_count(self) -> int:
        with self._admission_lock:
            return self._pending_operations

    def _acquire_admission(self, sector_id: str | None) -> None:
        with self._admission_lock:
            sector_pending = (
                self._pending_by_sector.get(sector_id, 0)
                if sector_id is not None
                else 0
            )
            if self._pending_operations >= self._max_pending_operations or (
                sector_id is not None and sector_pending >= self._max_pending_per_guild
            ):
                raise OrganismServiceBusyError(
                    "organism service is at its bounded pending-work capacity"
                )
            self._pending_operations += 1
            if sector_id is not None:
                self._pending_by_sector[sector_id] = sector_pending + 1

    def _release_admission(self, sector_id: str | None) -> None:
        with self._admission_lock:
            if self._pending_operations <= 0:
                raise RuntimeError("organism service admission count underflow")
            self._pending_operations -= 1
            if sector_id is None:
                return
            sector_pending = self._pending_by_sector.get(sector_id, 0)
            if sector_pending <= 0:
                raise RuntimeError("organism service sector admission count underflow")
            if sector_pending == 1:
                self._pending_by_sector.pop(sector_id, None)
            else:
                self._pending_by_sector[sector_id] = sector_pending - 1

    @staticmethod
    def _observe_worker(task: asyncio.Task[object]) -> None:
        if not task.cancelled():
            task.exception()

    async def _run_in_worker(
        self,
        operation: Callable[..., _WorkerResult],
        *args: object,
        sector_id: str | None = None,
        **kwargs: object,
    ) -> _WorkerResult:
        """Fail fast before queueing, and hold admission until the thread exits."""

        self._acquire_admission(sector_id)

        def admitted_operation() -> _WorkerResult:
            try:
                return operation(*args, **kwargs)
            finally:
                # Cancellation of the awaiting coroutine does not stop a running
                # thread. Releasing here prevents cancelled callers from opening
                # unbounded replacement capacity while their work is still active.
                self._release_admission(sector_id)

        try:
            worker = asyncio.create_task(asyncio.to_thread(admitted_operation))
        except BaseException:
            self._release_admission(sector_id)
            raise
        worker.add_done_callback(self._observe_worker)
        return await asyncio.shield(worker)

    def _experience_slot(
        self,
        guild_ref: str | int,
        frame: SensorFrame,
    ) -> str:
        if not isinstance(frame, SensorFrame):
            raise TypeError("frame must be a SensorFrame")
        return self.registry.experience_slot_id(
            guild_ref,
            frame.actor_ref,
            frame.capability,
        )

    def _slot_locks(self, slots: Iterable[str]) -> tuple[threading.RLock, ...]:
        indexes = sorted(
            {int(slot[:8], 16) % len(self._experience_slot_locks) for slot in slots}
        )
        return tuple(self._experience_slot_locks[index] for index in indexes)

    def _all_slot_locks(self) -> tuple[threading.RLock, ...]:
        return self._experience_slot_locks

    def _lifecycle_snapshot(self) -> tuple[int, int]:
        with self._lifecycle_lock:
            return (
                self._core_lifecycle_revision,
                self._discovery_lifecycle_revision,
            )

    def _assert_lifecycle(
        self,
        expected: tuple[int, int],
        *,
        discovery: bool,
    ) -> None:
        with self._lifecycle_lock:
            current = (
                self._core_lifecycle_revision,
                self._discovery_lifecycle_revision,
            )
        if current[0] != expected[0] or (discovery and current[1] != expected[1]):
            raise OrganismServiceStaleOperationError(
                "organism operation was superseded by a privacy deletion; "
                "retry only if the operation is still appropriate"
            )

    def _advance_lifecycle(self, *, core: bool, discovery: bool) -> None:
        with self._lifecycle_lock:
            if core:
                self._core_lifecycle_revision += 1
            if discovery:
                self._discovery_lifecycle_revision += 1

    @contextmanager
    def _privacy_operation(
        self,
        expected_revision: int,
        *,
        sector_refs: Iterable[str | int] = (),
        discovery: bool = False,
        associations: bool = False,
    ) -> Iterator[None]:
        selected_sector_refs = tuple(sector_refs)
        try:
            with self.registry.privacy_operation(
                expected_revision,
                sector_refs=selected_sector_refs,
                discovery=discovery,
                associations=associations,
            ):
                yield
        except PrivacyDeletionSupersededError as error:
            # The durable marker may have been advanced by another service
            # instance, which cannot directly evict this process's replay RAM.
            # Drop every potentially affected coherent packet before allowing a
            # caller-chosen retry to proceed under the refreshed revision.
            # The durable revision is global while its transaction locks are
            # domain-specific. A stale admission therefore does not reveal
            # which domain was erased; evict every replay record so a retry in
            # another guild cannot reuse pre-deletion RAM.
            self._drop_all_experience()
            raise OrganismServiceStaleOperationError(
                "organism operation was superseded by a privacy deletion; "
                "retry only if the operation is still appropriate"
            ) from error

    def _experience_entry(
        self,
        slot: str,
        checked_at: datetime,
    ) -> _ExperienceCacheEntry | None:
        with self._experience_cache_lock:
            for key, candidate in tuple(self._experience_cache.items()):
                active = tuple(
                    record
                    for record in candidate.records
                    if checked_at < record.expires_at
                )
                if not active:
                    self._experience_cache.pop(key, None)
                elif len(active) != len(candidate.records):
                    self._experience_cache[key] = replace(candidate, records=active)
            entry = self._experience_cache.get(slot)
            if entry is not None:
                self._experience_cache.move_to_end(slot)
            return entry

    @staticmethod
    def _fingerprint_value(value: object) -> object:
        """Normalize dataclass input for a deterministic, content-free digest."""

        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, Mapping):
            return {
                str(key): OrganismService._fingerprint_value(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(value, (list, tuple)):
            return [OrganismService._fingerprint_value(item) for item in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        raise TypeError(f"unsupported replay-fingerprint value: {type(value).__name__}")

    def _experience_input_fingerprint(
        self,
        guild_ref: str | int,
        frame: SensorFrame,
        references: tuple[SensoryReference, ...],
    ) -> str:
        """Bind an event token to its stable caller-supplied organism inputs."""

        frame_payload = asdict(frame)
        # The registry owns routing and intentionally replaces this presentation
        # default with an opaque sector scope.
        frame_payload.pop("scope", None)
        payload = {
            "schema": "organism.experience-replay-input.v1",
            "sector_id": self.registry.sector_id(guild_ref),
            "frame": self._fingerprint_value(frame_payload),
            "references": [
                {
                    "association_id": reference.association_id,
                    "intensity": reference.intensity,
                }
                for reference in sorted(
                    references,
                    key=lambda value: (value.association_id, value.intensity),
                )
            ],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hmac.new(
            self._experience_request_key,
            encoded,
            hashlib.sha256,
        ).hexdigest()

    def _store_experience(
        self,
        slot: str,
        sector_id: str,
        event_token: str,
        input_fingerprint: str,
        moment: ExperienceMoment,
        packet: FeelingPacket,
    ) -> bool:
        expires_at = min(
            packet.expires_at,
            packet.generated_at + self._experience_cache_ttl,
        )
        record = _ExperienceReplayRecord(
            event_token=event_token,
            input_fingerprint=input_fingerprint,
            packet=packet,
            moment=moment,
            generated_at=packet.generated_at,
            expires_at=expires_at,
        )
        with self._experience_cache_lock:
            current = self._experience_cache.get(slot)
            if current is not None and current.generated_at > packet.generated_at:
                return False
            prior_records = current.records if current is not None else ()
            records = tuple(
                prior
                for prior in prior_records
                if not hmac.compare_digest(prior.event_token, event_token)
                and packet.generated_at < prior.expires_at
            )
            records = (records + (record,))[-_MAX_REPLAY_EVENTS_PER_SLOT:]
            entry = _ExperienceCacheEntry(
                sector_id=sector_id,
                records=records,
            )
            self._experience_cache[slot] = entry
            self._experience_cache.move_to_end(slot)
            while len(self._experience_cache) > self._max_experience_cache_entries:
                self._experience_cache.popitem(last=False)
        return True

    def _drop_experience_slots(self, slots: Iterable[str]) -> None:
        with self._experience_cache_lock:
            for slot in slots:
                self._experience_cache.pop(slot, None)

    def _drop_sector_experience(self, sector_id: str) -> None:
        with self._experience_cache_lock:
            for slot, entry in tuple(self._experience_cache.items()):
                if entry.sector_id == sector_id:
                    self._experience_cache.pop(slot, None)

    def _drop_all_experience(self) -> None:
        with self._experience_cache_lock:
            self._experience_cache.clear()

    def _feel_with_cues_operation(
        self,
        guild_ref: str | int,
        frame: SensorFrame,
        references: Iterable[SensoryReference],
    ) -> FeelingWithCues:
        if not isinstance(frame, SensorFrame):
            raise TypeError("frame must be a SensorFrame")
        sampled = self._operational_sensor()
        if not isinstance(sampled, OperationalInteroception):
            raise TypeError("operational_sensor must return OperationalInteroception")
        enriched = replace(
            frame,
            interoception=merge_operational_interoception(
                frame.interoception,
                sampled,
            ),
        )
        return self.registry.feel_with_cues(
            guild_ref,
            enriched,
            sensory_references=references,
        )

    @staticmethod
    def _validate_discovery_binding(
        guild_ref: str | int,
        frame: SensorFrame,
        stimulus: CuriosityStimulus,
    ) -> None:
        """Bind transient discovery references to the authenticated body frame."""

        if stimulus.scope_ref != str(guild_ref).strip():
            raise ValueError("stimulus.scope_ref must match guild_ref")
        if stimulus.event_ref != frame.event_id:
            raise ValueError("stimulus.event_ref must match frame.event_id")
        if stimulus.actor_ref != frame.actor_ref:
            raise ValueError("stimulus.actor_ref must match frame.actor_ref")

    def _consider_discovery_operation(
        self,
        guild_ref: str | int,
        frame: SensorFrame,
        stimulus: CuriosityStimulus,
        references: Iterable[SensoryReference],
    ) -> tuple[FeelingPacket, InquirySignal]:
        discovery = self.discovery
        if discovery is None or not discovery.settings.enabled:
            raise RuntimeError("organism discovery is disabled")
        if not isinstance(frame, SensorFrame):
            raise TypeError("frame must be a SensorFrame")
        if not isinstance(stimulus, CuriosityStimulus):
            raise TypeError("stimulus must be a CuriosityStimulus")
        self._validate_discovery_binding(guild_ref, frame, stimulus)
        result = self._feel_with_cues_operation(guild_ref, frame, references)
        inquiry = discovery.consider(result.cues, result.packet, stimulus)
        return result.packet, inquiry

    def _response_input(
        self,
        *,
        packet: FeelingPacket,
        moment: ExperienceMoment,
        frame: SensorFrame,
        user_text: str,
        discovery_message: dict[str, str] | None,
    ) -> list[dict[str, str]]:
        """Compose one provider handoff from a coherent packet/moment pair."""

        checked_at = ensure_utc(self.registry.clock(), name="clock")
        # A bounded clock correction must not make a cached packet appear to
        # predate itself. Expiry remains enforced in the forward direction.
        if checked_at < packet.generated_at:
            if packet.generated_at > checked_at + timedelta(
                seconds=self.registry.config.max_future_skew_seconds
            ):
                raise StateStoreError(
                    "system clock moved backwards beyond the allowed skew; "
                    "refusing to reuse cached experience"
                )
            checked_at = packet.generated_at
        response_input = packet.responses_input(user_text, now=checked_at)
        response_input.insert(-1, cortex_developer_message(moment))
        if discovery_message is not None:
            response_input.insert(-1, discovery_message)
        profile_message = person_developer_message(frame.profile)
        if profile_message is not None:
            response_input.insert(-1, profile_message)
        return response_input

    async def feel_with_cues(
        self,
        guild_ref: str | int,
        frame: SensorFrame,
        *,
        sensory_references: Iterable[SensoryReference] = (),
    ) -> FeelingWithCues:
        sector_id = self.registry.sector_id(guild_ref)
        slot = self._experience_slot(guild_ref, frame)
        slot_lock = self._slot_locks((slot,))[0]
        lifecycle = self._lifecycle_snapshot()
        privacy_revision = self.registry.privacy_deletion_revision

        def operation() -> FeelingWithCues:
            with slot_lock:
                self._assert_lifecycle(lifecycle, discovery=False)
                references = self.registry.validated_sensory_references(
                    sensory_references
                )
                with self._privacy_operation(
                    privacy_revision,
                    sector_refs=(guild_ref,),
                    associations=bool(references),
                ):
                    return self._feel_with_cues_operation(
                        guild_ref,
                        frame,
                        references,
                    )

        return await self._run_in_worker(
            operation,
            sector_id=sector_id,
        )

    async def feel(
        self,
        guild_ref: str | int,
        frame: SensorFrame,
        *,
        sensory_references: Iterable[SensoryReference] = (),
    ) -> FeelingPacket:
        result = await self.feel_with_cues(
            guild_ref,
            frame,
            sensory_references=sensory_references,
        )
        return result.packet

    async def consider_discovery(
        self,
        guild_ref: str | int,
        frame: SensorFrame,
        stimulus: CuriosityStimulus,
        *,
        sensory_references: Iterable[SensoryReference] = (),
    ) -> tuple[FeelingPacket, InquirySignal]:
        """Observe one trusted content-free episode and maybe queue a question."""

        if not self.discovery_enabled:
            raise OrganismDiscoveryDisabledError(
                "organism discovery is disabled; enable it before considering inquiry"
            )
        sector_id = self.registry.sector_id(guild_ref)
        slot = self._experience_slot(guild_ref, frame)
        slot_lock = self._slot_locks((slot,))[0]
        lifecycle = self._lifecycle_snapshot()
        privacy_revision = self.registry.privacy_deletion_revision

        def operation() -> tuple[FeelingPacket, InquirySignal]:
            with slot_lock:
                self._assert_lifecycle(lifecycle, discovery=True)
                references = self.registry.validated_sensory_references(
                    sensory_references
                )
                with self._privacy_operation(
                    privacy_revision,
                    sector_refs=(guild_ref,),
                    discovery=True,
                    associations=bool(references),
                ):
                    return self._consider_discovery_operation(
                        guild_ref,
                        frame,
                        stimulus,
                        references,
                    )

        return await self._run_in_worker(
            operation,
            sector_id=sector_id,
        )

    async def cortex_input(
        self,
        guild_ref: str | int,
        frame: SensorFrame,
        user_text: str,
        *,
        sensory_references: Iterable[SensoryReference] = (),
    ) -> tuple[FeelingPacket, list[dict[str, str]]]:
        """Prepare a fresh provider handoff without calling a language model."""

        validated_user_text = validate_user_text(user_text)
        slot = self._experience_slot(guild_ref, frame)
        slot_lock = self._slot_locks((slot,))[0]
        lifecycle = self._lifecycle_snapshot()
        privacy_revision = self.registry.privacy_deletion_revision

        def operation() -> tuple[FeelingPacket, list[dict[str, str]]]:
            with slot_lock:
                self._assert_lifecycle(lifecycle, discovery=True)
                references = self.registry.validated_sensory_references(
                    sensory_references
                )
                with self._privacy_operation(
                    privacy_revision,
                    sector_refs=(guild_ref,),
                    discovery=self.discovery_enabled,
                    associations=bool(references),
                ):
                    actor_scoped = frame.actor_ref is not None
                    event_token = self.registry.experience_event_id(
                        guild_ref,
                        frame.event_id,
                    )
                    checked_at = ensure_utc(self.registry.clock(), name="clock")
                    entry = (
                        self._experience_entry(slot, checked_at)
                        if actor_scoped
                        else None
                    )
                    input_fingerprint = (
                        self._experience_input_fingerprint(guild_ref, frame, references)
                        if actor_scoped
                        else None
                    )
                    replay = entry.replay(event_token) if entry is not None else None
                    if replay is not None:
                        assert input_fingerprint is not None
                        if not hmac.compare_digest(
                            replay.input_fingerprint,
                            input_fingerprint,
                        ):
                            raise ValueError(
                                "frame event_id was reused with different organism inputs"
                            )
                        discovery_message = None
                        if self.discovery_enabled:
                            assert self.discovery is not None
                            discovery_message = discovery_cortex_developer_message(
                                self.discovery.snapshot(scope_ref=str(guild_ref).strip())
                            )
                        return replay.packet, self._response_input(
                            packet=replay.packet,
                            moment=replay.moment,
                            frame=frame,
                            user_text=validated_user_text,
                            discovery_message=discovery_message,
                        )

                    result = self._feel_with_cues_operation(
                        guild_ref,
                        frame,
                        references,
                    )
                    out_of_order = (
                        entry is not None
                        and entry.generated_at > result.packet.generated_at
                    )
                    moment = integrate_experience(
                        cues=result.cues,
                        feeling=result.packet,
                        previous=(
                            entry.moment
                            if entry is not None and not out_of_order
                            else None
                        ),
                    )
                    discovery_message = None
                    if self.discovery_enabled:
                        assert self.discovery is not None
                        discovery_scope = str(guild_ref).strip()
                        topic = (
                            DiscoveryTopic.CONCEPT_RIDDLE
                            if result.cues.novelty > moment.prediction.surprise
                            else DiscoveryTopic.FUNCTIONAL_SELF
                        )
                        if not out_of_order:
                            self.discovery.consider(
                                result.cues,
                                result.packet,
                                CuriosityStimulus(
                                    topic=topic,
                                    scope_ref=discovery_scope,
                                    event_ref=frame.event_id,
                                    functional_tension=(
                                        max(
                                            result.cues.uncertainty,
                                            moment.prediction.surprise,
                                        )
                                        if topic is DiscoveryTopic.FUNCTIONAL_SELF
                                        else 0.0
                                    ),
                                    concept_novelty=(
                                        result.cues.novelty
                                        if topic is DiscoveryTopic.CONCEPT_RIDDLE
                                        else 0.0
                                    ),
                                ),
                            )
                        discovery_message = discovery_cortex_developer_message(
                            self.discovery.snapshot(scope_ref=discovery_scope)
                        )
                    response_input = self._response_input(
                        packet=result.packet,
                        moment=moment,
                        frame=frame,
                        user_text=validated_user_text,
                        discovery_message=discovery_message,
                    )
                    if actor_scoped and not out_of_order:
                        assert input_fingerprint is not None
                        self._store_experience(
                            slot,
                            self.registry.sector_id(guild_ref),
                            event_token,
                            input_fingerprint,
                            moment,
                            result.packet,
                        )
                    return result.packet, response_input

        return await self._run_in_worker(
            operation,
            sector_id=self.registry.sector_id(guild_ref),
        )

    async def pending_discovery(self) -> tuple[Dispatch, ...]:
        """Return fixed-template questions awaiting application delivery."""

        if self.discovery is None:
            return ()
        return await self._run_in_worker(self.discovery.pending)

    async def mark_discovery_delivery(
        self,
        question_id: str,
        advisor_id: int,
    ) -> Receipt:
        """Record that the application delivered a question to its advisor."""

        if self.discovery is None:
            if type(advisor_id) is not int or not 1 <= advisor_id < (1 << 64):
                raise ValueError(
                    "discovery.advisor_ids entry must be between 1 and "
                    "18446744073709551615"
                )
            return Receipt(ReceiptStatus.DISABLED, question_id, 0)
        return await self._run_in_worker(
            self.discovery.mark_delivery,
            question_id,
            advisor_id,
        )

    async def review_discovery(self, review: StructuredReview) -> Receipt:
        """Apply one structured response from the selected configured advisor."""

        if self.discovery is None:
            if not isinstance(review, StructuredReview):
                raise ValueError("review must be a StructuredReview")
            return Receipt(ReceiptStatus.DISABLED, review.question_id, 0)
        return await self._run_in_worker(self.discovery.review, review)

    async def inspect_discovery(
        self,
        *,
        guild_ref: str | int | None = None,
    ) -> dict[str, object]:
        """Inspect aggregate discovery models without exposing keyed references."""

        if self.discovery is None:
            return {
                "schema_version": "organism.discovery.snapshot.v1",
                "enabled": False,
                "revision": 0,
                "pending_delivery_count": 0,
                "awaiting_review_count": 0,
                "recent_closed_count": 0,
                "models": {},
            }
        scope_ref = None if guild_ref is None else str(guild_ref).strip()
        return await self._run_in_worker(
            self.discovery.snapshot,
            scope_ref=scope_ref,
        )

    async def purge_discovery(self) -> dict[str, int]:
        """Erase the full discovery queue, rate history, and aggregate models."""

        if self.discovery is None:
            return {
                "pending_removed": 0,
                "history_removed": 0,
                "scoped_models_removed": 0,
            }
        def operation() -> dict[str, int]:
            with ExitStack() as stack:
                for lock in self._all_slot_locks():
                    stack.enter_context(lock)
                with self.registry.privacy_deletion(discovery=True):
                    self._advance_lifecycle(core=False, discovery=True)
                    assert self.discovery is not None
                    return self.discovery.purge_all()

        return await self._run_in_worker(operation)

    async def remember_association(
        self,
        review: AssociationReview,
        *,
        authorization: str,
    ) -> LearningReceipt:
        if not isinstance(review, AssociationReview):
            raise ValueError("review must be an AssociationReview")
        lifecycle = self._lifecycle_snapshot()
        privacy_revision = self.registry.privacy_deletion_revision

        def operation() -> LearningReceipt:
            with self._association_lock:
                self._assert_lifecycle(lifecycle, discovery=False)
                with self._privacy_operation(
                    privacy_revision,
                    associations=True,
                ):
                    return self.registry.remember_association(
                        review,
                        authorization=authorization,
                    )

        return await self._run_in_worker(
            operation,
            sector_id=self.registry.sector_id(review.source_sector_ref),
        )

    async def association_review_audience(self) -> str:
        return await self._run_in_worker(self.registry.association_review_audience)

    async def forget_member(self, guild_ref: str | int, actor_ref: str) -> int:
        slots = tuple(
            self.registry.experience_slot_id(guild_ref, actor_ref, capability)
            for capability in Capability
        )
        locks = self._slot_locks(slots)

        def operation() -> int:
            with ExitStack() as stack:
                for lock in locks:
                    stack.enter_context(lock)
                with self.registry.privacy_deletion(
                    sector_refs=(guild_ref,),
                    discovery=self.discovery is not None,
                ):
                    self._advance_lifecycle(
                        core=True,
                        discovery=self.discovery is not None,
                    )
                    removed = self.registry.forget_member(guild_ref, actor_ref)
                    if self.discovery is not None:
                        self.discovery.forget_subject(
                            scope_ref=str(guild_ref).strip(),
                            actor_ref=actor_ref,
                        )
                    self._drop_experience_slots(slots)
                    return removed

        return await self._run_in_worker(
            operation,
            sector_id=self.registry.sector_id(guild_ref),
        )

    async def forget_association(self, association_id: str) -> None:
        def operation() -> None:
            with ExitStack() as stack:
                for lock in self._all_slot_locks():
                    stack.enter_context(lock)
                stack.enter_context(self._association_lock)
                with self.registry.privacy_deletion(associations=True):
                    self._advance_lifecycle(core=True, discovery=False)
                    removed = self.registry.forget_association(association_id)
                    self._drop_all_experience()
                    return removed

        return await self._run_in_worker(operation)

    async def purge_guild(self, guild_ref: str | int) -> dict[str, int]:
        sector_id = self.registry.sector_id(guild_ref)

        def operation() -> dict[str, int]:
            with ExitStack() as stack:
                for lock in self._all_slot_locks():
                    stack.enter_context(lock)
                if self.associations_enabled:
                    stack.enter_context(self._association_lock)
                with self.registry.privacy_deletion(
                    sector_refs=(guild_ref,),
                    discovery=self.discovery is not None,
                    associations=self.associations_enabled,
                ):
                    self._advance_lifecycle(
                        core=True,
                        discovery=self.discovery is not None,
                    )
                    removed = self.registry.purge_sector(guild_ref)
                    if self.discovery is not None:
                        discovery_removed = self.discovery.purge_scope(
                            scope_ref=str(guild_ref).strip()
                        )
                        removed.update(
                            {
                                f"discovery_{name}": count
                                for name, count in discovery_removed.items()
                            }
                        )
                    if self.associations_enabled:
                        self._drop_all_experience()
                    else:
                        self._drop_sector_experience(sector_id)
                    return removed

        return await self._run_in_worker(operation, sector_id=sector_id)


def _prepare_state_directory(path: Path, *, associations_enabled: bool) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        _checked_path(path, name="organism.state_directory")
        if not path.is_dir():
            raise OSError("configured state path is not a directory")
        write_directories = [path, path / "sectors"]
        if associations_enabled:
            write_directories.append(path / "shared")
        for directory in write_directories:
            directory.mkdir(exist_ok=True)
            _checked_path(directory, name="organism.state_directory")
            with tempfile.NamedTemporaryFile(
                prefix=".organism-readiness-",
                dir=directory,
                delete=True,
            ) as probe:
                probe.write(b"ready")
                probe.flush()
    except (OSError, ValueError) as error:
        raise OrganismServiceConfigurationError(
            "organism.state_directory is not ready for atomic state writes"
        ) from error


def build_organism_service(
    values: Mapping[str, object] | None,
    *,
    repository_root: str | Path,
    environment: Mapping[str, str] | None = None,
) -> OrganismService | None:
    settings = OrganismServiceSettings.from_mapping(
        values,
        repository_root=repository_root,
    )
    if not settings.enabled:
        return None
    try:
        calibration = OrganismConfig.load(settings.config_path)
    except ValueError as error:
        calibration_label = (
            str(settings.config_path)
            if settings.config_path is not None
            else "built-in defaults"
        )
        raise OrganismServiceConfigurationError(
            f"organism calibration {calibration_label} is invalid: {error}"
        ) from error
    env = os.environ if environment is None else environment
    identity_key = _secret("FATE_ORGANISM_IDENTITY_KEY", env)
    learning_key = _secret("FATE_ORGANISM_LEARNING_KEY", env)
    owner_key = _secret("FATE_ORGANISM_OWNER_KEY", env)
    association_key = (
        _secret("FATE_ORGANISM_ASSOCIATION_KEY", env)
        if settings.cross_guild_associations
        else None
    )
    _prepare_state_directory(
        settings.state_directory,
        associations_enabled=settings.cross_guild_associations,
    )
    registry = MemorySectorRegistry(
        settings.state_directory,
        identity_key=identity_key,
        learning_key=learning_key,
        owner_key=owner_key,
        association_key=association_key,
        config=calibration,
        max_cached_sectors=settings.max_cached_sectors,
    )
    # Keep the disabled engine available to trusted inspection and erasure
    # controls so previously retained discovery data never becomes invisible.
    discovery = DiscoveryEngine(
        settings.state_directory / "discovery.json",
        settings=settings.discovery,
        identity_key=identity_key,
        integrity_key=owner_key,
    )
    return OrganismService(
        registry,
        discovery=discovery,
        max_pending_operations=settings.max_pending_operations,
        max_pending_per_guild=settings.max_pending_per_guild,
    )


__all__ = [
    "OrganismDiscoveryDisabledError",
    "OrganismService",
    "OrganismServiceBusyError",
    "OrganismServiceConfigurationError",
    "OrganismServiceStaleOperationError",
    "OrganismServiceSettings",
    "build_organism_service",
    "merge_operational_interoception",
    "sample_operational_interoception",
]
