"""Isolated memory sectors and bounded cross-sector sensory recall."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
from collections import OrderedDict
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterable, Iterator, Mapping

from .api import Organism
from .authority import sign_feedback
from .body import SensorFrame
from .config import OrganismConfig
from .models import (
    Capability,
    Cues,
    Feedback,
    FeedbackKind,
    FeedbackSource,
    FeelingPacket,
    LearningReceipt,
    Observation,
    ReasonCode,
    bounded_text,
    clamp,
    ensure_utc,
    parse_timestamp,
    timestamp_text,
    unit,
    utc_now,
)
from .storage import JsonStateStore, StateStoreError

ASSOCIATION_TAXONOMY: Mapping[str, str] = MappingProxyType(
    {
        "sensory:rapid-boundary-pressure": (
            "Current interaction pressure is rising quickly around a boundary."
        ),
        "sensory:sustained-demand": (
            "Current demand remains elevated across the interaction."
        ),
        "sensory:high-uncertainty": (
            "Current sensory inputs remain materially uncertain."
        ),
        "sensory:repair-cue": ("Current interaction contains an explicit repair cue."),
        "sensory:stable-safe-contact": (
            "Current contact remains consistently safe and controllable."
        ),
        "pattern:verified-repetition": (
            "Independent reviewed episodes confirm the same non-personal pattern."
        ),
        "pattern:repeated-boundary-pressure": (
            "Reviewed episodes repeat the same boundary-pressure pattern."
        ),
        "pattern:consistent-safe-followthrough": (
            "Reviewed episodes show consistent safe follow-through."
        ),
        "system:resource-warning": (
            "Trusted runtime telemetry reports constrained resources."
        ),
        "system:queue-saturation": (
            "Trusted runtime telemetry reports queue saturation."
        ),
        "system:rate-limit-warning": (
            "Trusted runtime telemetry reports rate-limit pressure."
        ),
        "system:error-burst": (
            "Trusted runtime telemetry reports a bounded error burst."
        ),
        "system:recovery-window": (
            "Trusted runtime telemetry reports a recovery opportunity."
        ),
    }
)
_ASSOCIATION_SCOPE = "shared-sensory-v1"
_ASSOCIATION_REVIEW_AUTHORIZATION = "organism.association-review.authorization.v1"
_ASSOCIATION_REVIEW_TTL = timedelta(minutes=5)
_PRIVACY_LIFECYCLE_SCHEMA = "organism.privacy-lifecycle.v1"
_MAX_PRIVACY_REVISION = (1 << 63) - 1


class PrivacyDeletionSupersededError(RuntimeError):
    """Raised when a durable deletion barrier supersedes queued work."""


def _validate_privacy_lifecycle(payload: dict) -> None:
    if set(payload) != {
        "schema_version",
        "revision",
        "record_authentication",
    }:
        raise StateStoreError("organism privacy lifecycle structure is invalid")
    revision = payload.get("revision")
    authentication = payload.get("record_authentication")
    if (
        payload.get("schema_version") != _PRIVACY_LIFECYCLE_SCHEMA
        or type(revision) is not int
        or not 0 <= revision <= _MAX_PRIVACY_REVISION
        or not isinstance(authentication, str)
        or re.fullmatch(r"[0-9a-f]{64}", authentication) is None
    ):
        raise StateStoreError("organism privacy lifecycle document is invalid")


def _master_key(value: bytes | str, *, name: str) -> bytes:
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    if not isinstance(encoded, bytes) or len(encoded) < 32:
        raise ValueError(f"{name} must contain at least 32 bytes")
    return encoded


def _derive_key(master: bytes, purpose: str, sector_id: str = "") -> bytes:
    material = (
        b"organism.memory-sector-key.v1\0"
        + purpose.encode("ascii")
        + b"\0"
        + sector_id.encode("ascii")
    )
    return hmac.new(master, material, hashlib.sha256).digest()


def _privacy_lifecycle_authentication(key: bytes, revision: int) -> str:
    material = (
        _PRIVACY_LIFECYCLE_SCHEMA.encode("ascii")
        + b"\0"
        + str(revision).encode("ascii")
    )
    return hmac.new(key, material, hashlib.sha256).hexdigest()


def _opaque_reference(master: bytes, domain: str, value: str) -> str:
    material = domain.encode("ascii") + b"\0" + value.encode("utf-8")
    return hmac.new(master, material, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class SensoryReference:
    """A trusted, non-personal association lookup capability."""

    association_id: str
    access_token: str = field(repr=False)
    intensity: float = 1.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "association_id",
            _association_identifier(self.association_id),
        )
        if not isinstance(self.access_token, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.access_token
        ):
            raise ValueError("access_token must be a 64-character lowercase digest")
        object.__setattr__(
            self,
            "intensity",
            unit(self.intensity, name="sensory_reference.intensity"),
        )


def _association_identifier(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("association_id must be a string")
    normalized = value.strip().casefold()
    if normalized not in ASSOCIATION_TAXONOMY:
        raise ValueError(
            "association_id must be an audited non-personal association taxonomy value"
        )
    return normalized


def sign_sensory_reference(
    association_id: str,
    association_key: bytes | str,
) -> str:
    """Mint a lookup token at a trusted application boundary."""

    normalized = _association_identifier(association_id)
    master = _master_key(association_key, name="association_key")
    return hmac.new(
        _derive_key(master, "sensory-reference-access"),
        normalized.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


@dataclass(frozen=True)
class AssociationReview:
    """Reviewed evidence used to build a shared sensory association."""

    association_id: str
    source_sector_ref: str
    event_id: str
    episode_id: str
    kind: FeedbackKind
    reason: ReasonCode
    cues: Cues
    severity: float
    confidence: float
    occurred_at: datetime
    capability: Capability = Capability.CONVERSATION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "association_id",
            _association_identifier(self.association_id),
        )
        object.__setattr__(
            self,
            "source_sector_ref",
            bounded_text(
                self.source_sector_ref,
                name="source_sector_ref",
                maximum=256,
            ),
        )
        object.__setattr__(
            self,
            "event_id",
            bounded_text(self.event_id, name="event_id"),
        )
        object.__setattr__(
            self,
            "episode_id",
            bounded_text(self.episode_id, name="episode_id"),
        )
        if not isinstance(self.kind, FeedbackKind):
            try:
                object.__setattr__(self, "kind", FeedbackKind(self.kind))
            except (TypeError, ValueError) as error:
                raise ValueError("association review kind is unsupported") from error
        if self.kind not in (
            FeedbackKind.ADVERSE,
            FeedbackKind.SAFE,
            FeedbackKind.REPAIR,
        ):
            raise ValueError("association reviews support adverse, safe, or repair")
        if not isinstance(self.reason, ReasonCode):
            try:
                object.__setattr__(self, "reason", ReasonCode(self.reason))
            except (TypeError, ValueError) as error:
                raise ValueError("association review reason is unsupported") from error
        if not isinstance(self.cues, Cues):
            raise ValueError("association review cues must be a Cues instance")
        if not isinstance(self.capability, Capability):
            try:
                object.__setattr__(self, "capability", Capability(self.capability))
            except (TypeError, ValueError) as error:
                raise ValueError("association capability is unsupported") from error
        object.__setattr__(
            self, "severity", unit(self.severity, name="association.severity")
        )
        object.__setattr__(
            self,
            "confidence",
            unit(self.confidence, name="association.confidence"),
        )
        object.__setattr__(
            self,
            "occurred_at",
            ensure_utc(self.occurred_at, name="association.occurred_at"),
        )


def _association_review_payload(
    review: AssociationReview,
    *,
    audience: str,
    issued_at: str,
    nonce: str,
) -> bytes:
    payload = {
        "schema_version": _ASSOCIATION_REVIEW_AUTHORIZATION,
        "audience": bounded_text(audience, name="audience", maximum=128),
        "issued_at": issued_at,
        "nonce": nonce,
        "association_id": review.association_id,
        "source_sector_ref": review.source_sector_ref,
        "event_id": review.event_id,
        "episode_id": review.episode_id,
        "kind": review.kind.value,
        "reason": review.reason.value,
        "cues": review.cues.to_dict(),
        "severity": review.severity,
        "confidence": review.confidence,
        "occurred_at": timestamp_text(review.occurred_at),
        "capability": review.capability.value,
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sign_association_review(
    review: AssociationReview,
    owner_key: bytes | str,
    *,
    audience: str,
    issued_at: datetime | None = None,
    nonce: str | None = None,
) -> str:
    """Authorize one human-reviewed shared association at the application edge."""

    if not isinstance(review, AssociationReview):
        raise ValueError("review must be an AssociationReview")
    master = _master_key(owner_key, name="owner_key")
    issued_text = timestamp_text(issued_at or utc_now())
    selected_nonce = bounded_text(
        nonce or secrets.token_urlsafe(18),
        name="nonce",
        maximum=128,
    )
    if "|" in selected_nonce:
        raise ValueError("nonce cannot contain '|'")
    signature = hmac.new(
        _derive_key(master, "association-review-authorization"),
        _association_review_payload(
            review,
            audience=audience,
            issued_at=issued_text,
            nonce=selected_nonce,
        ),
        hashlib.sha256,
    ).hexdigest()
    return "|".join(
        (_ASSOCIATION_REVIEW_AUTHORIZATION, issued_text, selected_nonce, signature)
    )


@dataclass(frozen=True)
class FeelingWithCues:
    """One feeling packet bound to the exact effective cues that produced it."""

    cues: Cues
    packet: FeelingPacket

    def __post_init__(self) -> None:
        if not isinstance(self.cues, Cues):
            raise ValueError("cues must be a Cues instance")
        if not isinstance(self.packet, FeelingPacket):
            raise ValueError("packet must be a FeelingPacket")


def blend_associative_cues(
    base: Cues,
    recalls: Iterable[tuple[FeelingPacket, float, float]],
) -> Cues:
    """Blend recalled regulatory traces into current cues with a hard cap."""

    values = base.to_dict()
    total_influence = 0.0
    for packet, intensity, learned_activation in recalls:
        influence = min(
            0.25,
            unit(intensity, name="recall intensity")
            * unit(learned_activation, name="recall activation")
            * 0.35,
        )
        influence = min(influence, 0.45 - total_influence)
        if influence <= 0.0:
            continue
        total_influence += influence
        state = packet.state
        recalled_threat = state.activation * (1.0 - state.interaction_safety)
        values["threat"] = clamp(
            1.0 - (1.0 - values["threat"]) * (1.0 - recalled_threat * influence)
        )
        values["safety"] = clamp(
            values["safety"] * (1.0 - influence) + state.interaction_safety * influence
        )
        values["connection"] = clamp(
            values["connection"] * (1.0 - influence)
            + ((state.pleasantness + 1.0) / 2.0) * influence
        )
        values["boundary_pressure"] = clamp(
            1.0
            - (1.0 - values["boundary_pressure"])
            * (1.0 - packet.protection.caution * influence)
        )
        values["uncertainty"] = clamp(
            1.0 - (1.0 - values["uncertainty"]) * (1.0 - state.uncertainty * influence)
        )
        values["demand"] = clamp(
            1.0 - (1.0 - values["demand"]) * (1.0 - state.load * influence)
        )
        values["controllability"] = clamp(
            values["controllability"] * (1.0 - influence) + state.agency * influence
        )
        values["energy_cost"] = clamp(
            1.0 - (1.0 - values["energy_cost"]) * (1.0 - state.load * influence)
        )
    return Cues(**values)


class MemorySectorRegistry:
    """Own per-scope engines and an optional shared sensory memory engine."""

    @staticmethod
    def purge_all_state_after_key_loss(
        state_directory: str | Path,
        *,
        owner_key: bytes | str,
    ) -> dict[str, int | bool]:
        """Explicitly erase every known store and reseal the deletion marker.

        This bootstrap recovery exists only for deliberate key-loss handling,
        when authenticated documents cannot be read well enough to call their
        ordinary purge APIs. Every service instance using the directory must be
        stopped first. The method never runs automatically and preserves no
        learned, discovery, association, or transient state.
        """

        source = Path(state_directory)
        if source.is_symlink():
            raise ValueError("state_directory cannot be a symbolic link")
        root = source.resolve()
        if root == Path(root.anchor) or not root.exists() or not root.is_dir():
            raise ValueError(
                "state_directory must be an existing dedicated non-root directory"
            )
        master = _master_key(owner_key, name="owner_key")
        privacy_key = _derive_key(master, "privacy-lifecycle-authentication")

        def fresh() -> dict:
            revision = 0
            return {
                "schema_version": _PRIVACY_LIFECYCLE_SCHEMA,
                "revision": revision,
                "record_authentication": _privacy_lifecycle_authentication(
                    privacy_key,
                    revision,
                ),
            }

        # Validate every recovery input before deleting the first byte.
        _validate_privacy_lifecycle(fresh())
        marker = JsonStateStore(
            root / ".privacy-lifecycle.json",
            validator=_validate_privacy_lifecycle,
        )

        def dedicated_directory(name: str) -> Path | None:
            candidate = root / name
            if not candidate.exists():
                return None
            if candidate.is_symlink() or not candidate.is_dir():
                raise ValueError(f"state_directory/{name} must be a real directory")
            resolved = candidate.resolve()
            try:
                resolved.relative_to(root)
            except ValueError as error:
                raise ValueError(
                    f"state_directory/{name} must remain inside state_directory"
                ) from error
            return resolved

        targets: list[tuple[str, Path]] = []
        sectors = dedicated_directory("sectors")
        if sectors is not None:
            targets.extend(
                ("sector_states_removed", path)
                for path in sectors.glob("*.json")
            )
            targets.extend(
                ("temporary_files_removed", path)
                for path in sectors.glob(".*.json.*.tmp")
            )
        shared = dedicated_directory("shared")
        if shared is not None:
            targets.extend(
                ("shared_states_removed", path) for path in shared.glob("*.json")
            )
            targets.extend(
                ("temporary_files_removed", path)
                for path in shared.glob(".*.json.*.tmp")
            )
        discovery = root / "discovery.json"
        if discovery.exists() or discovery.is_symlink():
            targets.append(("discovery_states_removed", discovery))
        targets.extend(
            ("temporary_files_removed", path)
            for pattern in (
                ".discovery.json.*.tmp",
                ".privacy-lifecycle.json.*.tmp",
            )
            for path in root.glob(pattern)
        )

        counts = {
            "sector_states_removed": 0,
            "shared_states_removed": 0,
            "discovery_states_removed": 0,
            "temporary_files_removed": 0,
        }
        try:
            for category, target in targets:
                target.unlink()
                counts[category] += 1
        except OSError as error:
            raise StateStoreError(
                "unable to erase organism state during explicit key recovery"
            ) from error

        marker_replaced = marker.replace(fresh)
        return {**counts, "privacy_marker_replaced": marker_replaced}

    def __init__(
        self,
        state_directory: str | Path,
        *,
        identity_key: bytes | str,
        learning_key: bytes | str,
        owner_key: bytes | str,
        association_key: bytes | str | None = None,
        config_path: str | Path | None = None,
        config: OrganismConfig | None = None,
        max_cached_sectors: int = 128,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._identity_master = _master_key(identity_key, name="identity_key")
        self._learning_master = _master_key(learning_key, name="learning_key")
        self._owner_master = _master_key(owner_key, name="owner_key")
        if (
            len(
                {
                    self._identity_master,
                    self._learning_master,
                    self._owner_master,
                }
            )
            != 3
        ):
            raise ValueError("identity, learning, and owner keys must be distinct")
        self._association_master = (
            _master_key(association_key, name="association_key")
            if association_key is not None
            else None
        )
        if self._association_master in {
            self._identity_master,
            self._learning_master,
            self._owner_master,
        }:
            raise ValueError("association_key must be distinct from other keys")
        if isinstance(max_cached_sectors, bool) or not isinstance(
            max_cached_sectors, int
        ):
            raise ValueError("max_cached_sectors must be an integer")
        if max_cached_sectors < 1 or max_cached_sectors > 4096:
            raise ValueError("max_cached_sectors must be between 1 and 4096")
        if config is not None and config_path is not None:
            raise ValueError("config and config_path are mutually exclusive")
        if config is not None and not isinstance(config, OrganismConfig):
            raise TypeError("config must be an OrganismConfig or None")
        self.state_directory = Path(state_directory).resolve()
        self.config_path = Path(config_path).resolve() if config_path else None
        # Pin one immutable calibration for every current and future sector.
        self.config = (
            config if config is not None else OrganismConfig.load(self.config_path)
        )
        self.max_cached_sectors = max_cached_sectors
        self.clock = clock
        self._privacy_key = _derive_key(
            self._owner_master,
            "privacy-lifecycle-authentication",
        )
        self._privacy_store = JsonStateStore(
            self.state_directory / ".privacy-lifecycle.json",
            validator=_validate_privacy_lifecycle,
        )
        self._privacy_cache_lock = threading.RLock()
        self._privacy_revision = self._read_privacy_revision()
        self._sectors: OrderedDict[str, Organism] = OrderedDict()
        self._association_engine: Organism | None = None
        self._cache_lock = threading.RLock()

    @property
    def associations_enabled(self) -> bool:
        return self._association_master is not None

    def _fresh_privacy_lifecycle(self) -> dict:
        revision = 0
        return {
            "schema_version": _PRIVACY_LIFECYCLE_SCHEMA,
            "revision": revision,
            "record_authentication": _privacy_lifecycle_authentication(
                self._privacy_key,
                revision,
            ),
        }

    def _authenticated_privacy_revision(self, payload: Mapping[str, object]) -> int:
        revision = payload.get("revision")
        authentication = payload.get("record_authentication")
        if type(revision) is not int or not isinstance(authentication, str):
            raise StateStoreError("organism privacy lifecycle document is invalid")
        expected = _privacy_lifecycle_authentication(self._privacy_key, revision)
        if not hmac.compare_digest(expected, authentication):
            raise StateStoreError(
                "organism privacy lifecycle authentication failed; refusing state access"
            )
        return revision

    def _read_privacy_revision(self) -> int:
        payload = self._privacy_store.read(self._fresh_privacy_lifecycle)
        return self._authenticated_privacy_revision(payload)

    @property
    def privacy_deletion_revision(self) -> int:
        """Return the local admission snapshot without event-loop disk I/O."""

        with self._privacy_cache_lock:
            return self._privacy_revision

    def _privacy_domains(
        self,
        *,
        sector_refs: Iterable[str | int],
        discovery: bool,
        associations: bool,
    ) -> tuple[str, ...]:
        domains = {
            f"sector:{self.sector_id(sector_ref)}" for sector_ref in sector_refs
        }
        if discovery:
            domains.add("shared:discovery")
        if associations:
            domains.add("shared:associations")
        if not domains:
            raise ValueError("a privacy lifecycle operation requires a state domain")
        return tuple(sorted(domains))

    def _privacy_domain_store(self, domain: str) -> JsonStateStore:
        token = _opaque_reference(
            self._owner_master,
            "organism.privacy-domain.v1",
            domain,
        )[:40]
        return JsonStateStore(
            self.state_directory / ".privacy-locks" / f"{token}.json",
            validator=_validate_privacy_lifecycle,
        )

    @contextmanager
    def _privacy_domain_locks(self, domains: Iterable[str]) -> Iterator[None]:
        with ExitStack() as stack:
            for domain in domains:
                stack.enter_context(self._privacy_domain_store(domain).locked())
            yield

    @contextmanager
    def privacy_operation(
        self,
        expected_revision: int,
        *,
        sector_refs: Iterable[str | int] = (),
        discovery: bool = False,
        associations: bool = False,
    ) -> Iterator[None]:
        """Serialize a mutation with deletions across service instances.

        Existing service instances keep an in-memory admission revision. A
        deletion elsewhere advances the authenticated durable marker, causing
        already-admitted work (and one appropriate retry boundary) to fail
        closed instead of recreating erased data.
        """

        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected privacy revision must be a non-negative integer")
        domains = self._privacy_domains(
            sector_refs=sector_refs,
            discovery=discovery,
            associations=associations,
        )
        with self._privacy_domain_locks(domains):
            current = self._read_privacy_revision()
            with self._privacy_cache_lock:
                self._privacy_revision = max(self._privacy_revision, current)
            if current != expected_revision:
                raise PrivacyDeletionSupersededError(
                    "durable privacy deletion revision changed"
                )
            yield

    @contextmanager
    def privacy_deletion(
        self,
        *,
        sector_refs: Iterable[str | int] = (),
        discovery: bool = False,
        associations: bool = False,
    ) -> Iterator[int]:
        """Advance the durable barrier before executing one erasure operation."""

        domains = self._privacy_domains(
            sector_refs=sector_refs,
            discovery=discovery,
            associations=associations,
        )
        with self._privacy_domain_locks(domains):
            def advance(payload: dict) -> int:
                observed = self._authenticated_privacy_revision(payload)
                if observed >= _MAX_PRIVACY_REVISION:
                    raise StateStoreError(
                        "organism privacy lifecycle revision is exhausted"
                    )
                next_revision = observed + 1
                payload["revision"] = next_revision
                payload["record_authentication"] = (
                    _privacy_lifecycle_authentication(
                        self._privacy_key,
                        next_revision,
                    )
                )
                return next_revision

            next_revision = self._privacy_store.update(
                self._fresh_privacy_lifecycle,
                advance,
            )
            with self._privacy_cache_lock:
                self._privacy_revision = max(
                    self._privacy_revision,
                    next_revision,
                )
            yield next_revision

    def association_review_audience(self) -> str:
        associations, _ = self._associations()
        return associations.learning_audience()

    def _verify_association_review(
        self,
        review: AssociationReview,
        authorization: str,
    ) -> datetime:
        if not isinstance(authorization, str) or len(authorization) > 512:
            raise PermissionError("valid association review authorization is required")
        try:
            version, issued_text, nonce, supplied = authorization.split("|", 3)
            issued_at = parse_timestamp(issued_text)
        except (TypeError, ValueError) as error:
            raise PermissionError(
                "valid association review authorization is required"
            ) from error
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        if (
            version != _ASSOCIATION_REVIEW_AUTHORIZATION
            or not nonce
            or len(nonce) > 128
            or len(supplied) != 64
            or issued_at > now + timedelta(seconds=60)
            or issued_at <= now - _ASSOCIATION_REVIEW_TTL
        ):
            raise PermissionError("valid association review authorization is required")
        expected = hmac.new(
            _derive_key(self._owner_master, "association-review-authorization"),
            _association_review_payload(
                review,
                audience=self.association_review_audience(),
                issued_at=issued_text,
                nonce=nonce,
            ),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, supplied):
            raise PermissionError("valid association review authorization is required")
        return issued_at

    def sector_id(self, sector_ref: str | int) -> str:
        normalized = str(sector_ref).strip()
        if not normalized or len(normalized) > 256:
            raise ValueError("sector_ref must identify one bounded scope")
        return _opaque_reference(
            self._identity_master,
            "organism.guild-sector.v1",
            normalized,
        )[:40]

    def _sector_scope(self, sector_id: str) -> str:
        return f"sector:{sector_id}"

    def experience_slot_id(
        self,
        sector_ref: str | int,
        actor_ref: str | None,
        capability: Capability,
    ) -> str:
        """Return an opaque isolation slot without retaining caller identifiers."""

        try:
            selected_capability = Capability(capability)
        except (TypeError, ValueError) as error:
            raise ValueError("capability is unsupported") from error
        if actor_ref is None:
            actor_component = "actorless"
        else:
            normalized_actor = bounded_text(
                actor_ref,
                name="actor_ref",
                maximum=512,
            )
            actor_component = "actor:" + _opaque_reference(
                self._identity_master,
                "organism.experience-actor.v1",
                normalized_actor,
            )
        material = "\0".join(
            (
                self.sector_id(sector_ref),
                actor_component,
                selected_capability.value,
            )
        )
        return _opaque_reference(
            self._identity_master,
            "organism.experience-slot.v1",
            material,
        )[:40]

    def experience_event_id(
        self,
        sector_ref: str | int,
        event_ref: str,
    ) -> str:
        """Return an opaque event token for ephemeral replay detection."""

        normalized_event = bounded_text(
            event_ref,
            name="event_ref",
            maximum=512,
        )
        material = "\0".join((self.sector_id(sector_ref), normalized_event))
        return _opaque_reference(
            self._identity_master,
            "organism.experience-event.v1",
            material,
        )[:40]

    def _new_sector(self, sector_id: str) -> Organism:
        path = self.state_directory / "sectors" / f"{sector_id}.json"
        return Organism(
            path,
            config=self.config,
            identity_key=_derive_key(self._identity_master, "identity", sector_id),
            learning_key=_derive_key(self._learning_master, "learning", sector_id),
            owner_key=_derive_key(self._owner_master, "owner", sector_id),
            clock=self.clock,
        )

    def sector(self, sector_ref: str | int) -> Organism:
        sector_id = self.sector_id(sector_ref)
        with self._cache_lock:
            engine = self._sectors.pop(sector_id, None)
            if engine is None:
                engine = self._new_sector(sector_id)
            self._sectors[sector_id] = engine
            while len(self._sectors) > self.max_cached_sectors:
                self._sectors.popitem(last=False)
            return engine

    def _associations(self) -> tuple[Organism, bytes]:
        master = self._association_master
        if master is None:
            raise RuntimeError("cross-sector sensory associations are disabled")
        with self._cache_lock:
            if self._association_engine is None:
                self._association_engine = Organism(
                    self.state_directory / "shared" / "sensory.json",
                    config=self.config,
                    identity_key=_derive_key(master, "association-identity"),
                    learning_key=_derive_key(master, "association-learning"),
                    owner_key=_derive_key(master, "association-owner"),
                    clock=self.clock,
                )
            return self._association_engine, _derive_key(master, "association-learning")

    def _references(
        self,
        references: Iterable[SensoryReference],
    ) -> tuple[SensoryReference, ...]:
        unique: dict[str, SensoryReference] = {}
        for index, reference in enumerate(references):
            if index >= 32:
                raise ValueError("a sensor frame may supply at most 32 references")
            if not isinstance(reference, SensoryReference):
                raise ValueError("sensory references must be SensoryReference values")
            master = self._association_master
            if master is None:
                raise RuntimeError("cross-sector sensory associations are disabled")
            expected = sign_sensory_reference(reference.association_id, master)
            if not hmac.compare_digest(expected, reference.access_token):
                raise PermissionError("valid sensory reference access is required")
            previous = unique.get(reference.association_id)
            if previous is None or reference.intensity > previous.intensity:
                unique[reference.association_id] = reference
        if len(unique) > 8:
            raise ValueError("a sensor frame may recall at most eight associations")
        return tuple(unique.values())

    def validated_sensory_references(
        self,
        references: Iterable[SensoryReference],
    ) -> tuple[SensoryReference, ...]:
        """Validate, deduplicate, and bound references before sensing or replay."""

        return self._references(references)

    def _recalls(
        self,
        frame: SensorFrame,
        references: tuple[SensoryReference, ...],
    ) -> list[tuple[FeelingPacket, float, float]]:
        if not references:
            return []
        associations, _ = self._associations()
        recalls: list[tuple[FeelingPacket, float, float]] = []
        profiles = associations.inspect_profiles(
            scope=_ASSOCIATION_SCOPE,
            actor_refs=(reference.association_id for reference in references),
            capability=frame.capability,
        )
        for reference, profile in zip(references, profiles, strict=True):
            learned_activation = float(profile["activation"])
            if learned_activation <= 0.0:
                continue
            master = self._association_master
            assert master is not None
            recall_event = _opaque_reference(
                master,
                "organism.association-recall.v1",
                f"{frame.event_id}\0{reference.association_id}",
            )
            packet = associations.feel(
                Observation(
                    event_id=recall_event,
                    cues=Cues(),
                    scope=_ASSOCIATION_SCOPE,
                    capability=frame.capability,
                    actor_ref=reference.association_id,
                    occurred_at=frame.occurred_at,
                )
            )
            recalls.append((packet, reference.intensity, learned_activation))
        return recalls

    def feel_with_cues(
        self,
        sector_ref: str | int,
        frame: SensorFrame,
        *,
        sensory_references: Iterable[SensoryReference] = (),
    ) -> FeelingWithCues:
        if not isinstance(frame, SensorFrame):
            raise ValueError("frame must be a SensorFrame")
        sector_id = self.sector_id(sector_ref)
        references = self.validated_sensory_references(sensory_references)
        cues = blend_associative_cues(frame.cues(), self._recalls(frame, references))
        observation = replace(
            frame,
            scope=self._sector_scope(sector_id),
        ).observation()
        observation = Observation(
            event_id=observation.event_id,
            cues=cues,
            scope=observation.scope,
            capability=observation.capability,
            actor_ref=observation.actor_ref,
            occurred_at=observation.occurred_at,
        )
        packet = self.sector(sector_ref).feel(observation)
        return FeelingWithCues(cues=cues, packet=packet)

    def feel(
        self,
        sector_ref: str | int,
        frame: SensorFrame,
        *,
        sensory_references: Iterable[SensoryReference] = (),
    ) -> FeelingPacket:
        return self.feel_with_cues(
            sector_ref,
            frame,
            sensory_references=sensory_references,
        ).packet

    def remember_association(
        self,
        review: AssociationReview,
        *,
        authorization: str,
    ) -> LearningReceipt:
        """Persist a reviewed trace; never call this from model-generated text."""

        if not isinstance(review, AssociationReview):
            raise ValueError("review must be an AssociationReview")
        review_issued_at = self._verify_association_review(review, authorization)
        associations, learning_key = self._associations()
        source_sector_id = self.sector_id(review.source_sector_ref)
        provenance_episode = _opaque_reference(
            self._identity_master,
            "organism.association-provenance.v1",
            f"{source_sector_id}\0{review.episode_id}",
        )
        feedback = Feedback(
            event_id=review.event_id,
            episode_id=provenance_episode,
            scope=_ASSOCIATION_SCOPE,
            actor_ref=review.association_id,
            capability=review.capability,
            kind=review.kind,
            reason=review.reason,
            cues=review.cues,
            severity=review.severity,
            confidence=review.confidence,
            source=FeedbackSource.HUMAN_FEEDBACK,
            verified=True,
            occurred_at=review.occurred_at,
        )
        # The outer review token is itself replay protected by its short TTL and
        # complete-payload signature. Deriving a stable inner nonce and issue time
        # makes an exact captured replay map to the same consumed learning token,
        # so duplicate submissions cannot spend the shared store's quota.
        inner_nonce = hmac.new(
            learning_key,
            b"organism.association-inner-authorization.v1\0"
            + authorization.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        learning_authorization = sign_feedback(
            feedback,
            learning_key,
            audience=associations.learning_audience(),
            issued_at=review_issued_at,
            nonce=inner_nonce,
        )
        return associations.learn(feedback, authorization=learning_authorization)

    def forget_member(self, sector_ref: str | int, actor_ref: str) -> int:
        sector_id = self.sector_id(sector_ref)
        return self.sector(sector_ref).forget(
            scope=self._sector_scope(sector_id),
            actor_ref=actor_ref,
        )

    def forget_association(self, association_id: str) -> None:
        """Forget without exposing whether a shared association existed."""

        normalized = _association_identifier(association_id)
        associations, _ = self._associations()
        associations.forget(
            scope=_ASSOCIATION_SCOPE,
            actor_ref=normalized,
        )
        return None

    def purge_sector(self, sector_ref: str | int) -> dict[str, int]:
        removed = self.sector(sector_ref).purge_all()
        if self.associations_enabled:
            associations, _ = self._associations()
            shared = associations.purge_all()
            removed.update(
                {f"shared_association_{name}": count for name, count in shared.items()}
            )
        return removed


__all__ = [
    "ASSOCIATION_TAXONOMY",
    "AssociationReview",
    "FeelingWithCues",
    "MemorySectorRegistry",
    "PrivacyDeletionSupersededError",
    "SensoryReference",
    "blend_associative_cues",
    "sign_association_review",
    "sign_sensory_reference",
]
