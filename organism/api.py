"""Public facade for feeling, learning, growth, healing, and forgetting."""

from __future__ import annotations

import json
import math
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, TypeVar

from .authority import LearningAuthority
from .autonomic.coordinator import (
    dominant_mode,
    mode_weights,
    synthesize_autonomic_response,
)
from .config import OrganismConfig
from .growth import rebuild_growth
from .healing import recover_state
from .identity import IdentityPseudonymizer
from .learning import (
    active_records,
    adverse_admission_rejection,
    evidence_record,
    evidence_semantic_rejection,
    feedback_rejection,
    forget_subject,
    healing_admission_rejection,
    profile_metrics,
    prune_ledger,
)
from .models import (
    AutonomicMode,
    Capability,
    Feedback,
    FeedbackKind,
    FeedbackSource,
    FeelingPacket,
    LearningReceipt,
    Observation,
    OrganismicState,
    bounded_text,
    parse_timestamp,
    timestamp_text,
    unit,
    utc_now,
)
from .motifs import derive_regulatory_motifs
from .nervous_system import transition_state
from .protection import historical_state_bias, protective_summary
from .storage import (
    LEGACY_STATE_SCHEMA,
    JsonStateStore,
    STATE_SCHEMA,
    StateStoreError,
    migrate_v4_to_v5,
)
from .weights import baseline_cue_weights, predict_risk

T = TypeVar("T")
_MAX_TOTAL_RECENT_OBSERVATIONS = 4096


class Organism:
    """A deterministic, inspectable response-simulation and relational learning engine.

    Person-specific memory is disabled unless ``identity_key`` is supplied.
    The key must live in an environment variable or secret store, not in the
    organism state file.
    """

    def __init__(
        self,
        state_path: str | Path,
        *,
        config_path: str | Path | None = None,
        config: OrganismConfig | None = None,
        identity_key: bytes | str | None = None,
        learning_key: bytes | str | None = None,
        owner_key: bytes | str | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        configured_secrets: list[tuple[str, bytes]] = []
        for name, key in (
            ("identity_key", identity_key),
            ("learning_key", learning_key),
            ("owner_key", owner_key),
        ):
            encoded = key.encode("utf-8") if isinstance(key, str) else key
            if isinstance(encoded, bytes):
                if any(encoded == previous for _, previous in configured_secrets):
                    raise ValueError(
                        "identity_key, learning_key, and owner_key must be "
                        "pairwise distinct"
                    )
                configured_secrets.append((name, encoded))
        if config is not None and config_path is not None:
            raise ValueError("config and config_path are mutually exclusive")
        if config is not None and not isinstance(config, OrganismConfig):
            raise TypeError("config must be an OrganismConfig or None")
        self.store = JsonStateStore(state_path)
        self.config = config if config is not None else OrganismConfig.load(config_path)
        self.identities = IdentityPseudonymizer(identity_key)
        evidence_key = (
            identity_key
            if identity_key is not None
            else (learning_key if learning_key is not None else owner_key)
        )
        state_integrity_key = (
            owner_key
            if owner_key is not None
            else (identity_key if identity_key is not None else learning_key)
        )
        self.evidence_identities = IdentityPseudonymizer(evidence_key)
        self.transient_identities = IdentityPseudonymizer(
            identity_key if identity_key is not None else secrets.token_bytes(32)
        )
        self.authority = LearningAuthority(learning_key)
        self.owner_authority = LearningAuthority(owner_key)
        self.state_authority = LearningAuthority(state_integrity_key)
        self.clock = clock

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now

    def _baseline_state(self, now: datetime, *, revision: int = 0) -> OrganismicState:
        return OrganismicState(
            **self.config.baseline_state,
            updated_at=now,
            revision=revision,
        )

    def _fresh(self, now: datetime) -> dict:
        payload = {
            "schema_version": STATE_SCHEMA,
            "states": {},
            "dominant_modes": {},
            "state_access": {},
            "last_state_key": None,
            "identity_key_id": self.identities.key_id,
            "learning_key_id": self.authority.key_id,
            "owner_key_id": self.owner_authority.key_id,
            "evidence_key_id": self.evidence_identities.key_id,
            "state_integrity_key_id": self.state_authority.key_id,
            "state_authentication": None,
            "learning_audience": secrets.token_urlsafe(24),
            "evidence": [],
            "used_authorizations": [],
            "recent_observations": {},
            "attached_subjects": {},
            "learned": {
                "cue_weights": baseline_cue_weights(),
                "strategy_weights": {
                    strategy.value: value
                    for strategy, value in self.config.strategy_baselines.items()
                },
                "capacities": dict(self.config.capacity_baselines),
                "weight_revision": 0,
            },
            "updated_at": timestamp_text(now),
        }
        self._seal_security(payload)
        return payload

    def _fresh_factory(self, now: datetime) -> Callable[[], dict]:
        return lambda: self._fresh(now)

    @staticmethod
    def _security_payload(payload: dict) -> dict[str, object]:
        document = {
            key: value
            for key, value in payload.items()
            if key != "state_authentication"
        }
        return {
            "schema_version": "organism.security-root.v2",
            "document": document,
        }

    @staticmethod
    def _legacy_security_payload(payload: dict) -> dict[str, object]:
        """Return the v1 subset used only to authenticate a v4 migration."""

        return {
            "schema_version": "organism.security-root.v1",
            "state_schema": payload.get("schema_version"),
            "identity_key_id": payload.get("identity_key_id"),
            "learning_key_id": payload.get("learning_key_id"),
            "owner_key_id": payload.get("owner_key_id"),
            "evidence_key_id": payload.get("evidence_key_id"),
            "state_integrity_key_id": payload.get("state_integrity_key_id"),
            "learning_audience": payload.get("learning_audience"),
            "evidence": payload.get("evidence"),
            "used_authorizations": payload.get("used_authorizations"),
        }

    def _seal_security(self, payload: dict) -> None:
        if not self.state_authority.enabled:
            payload["state_authentication"] = None
            return
        payload["state_authentication"] = self.state_authority.authenticate_record(
            self._security_payload(payload)
        )

    def _verify_security(self, payload: dict) -> None:
        authentication = payload.get("state_authentication")
        if not self.state_authority.enabled:
            if (
                authentication is not None
                or payload.get("evidence")
                or payload.get("used_authorizations")
            ):
                raise StateStoreError(
                    "organism security root cannot be verified without its key"
                )
            return
        signed = self._security_payload(payload)
        signed["record_authentication"] = authentication
        if not self.state_authority.verifies_record(signed):
            raise StateStoreError(
                "organism security-root authentication failed; refuse to use "
                "or modify persisted state"
            )

    def _verify_legacy_security(self, payload: dict) -> None:
        authentication = payload.get("state_authentication")
        if not self.state_authority.enabled:
            if (
                authentication is not None
                or payload.get("evidence")
                or payload.get("used_authorizations")
            ):
                raise StateStoreError(
                    "legacy organism state cannot be authenticated without its key"
                )
            return
        signed = self._legacy_security_payload(payload)
        signed["record_authentication"] = authentication
        if not self.state_authority.verifies_record(signed):
            raise StateStoreError(
                "legacy organism security-root authentication failed; refusing "
                "migration"
            )

    def _migrate_state(self, payload: dict) -> dict:
        if payload.get("schema_version") != LEGACY_STATE_SCHEMA:
            raise StateStoreError("organism migration requires a v4 document")
        expected = {
            "identity_key_id": self.identities.key_id,
            "learning_key_id": self.authority.key_id,
            "owner_key_id": self.owner_authority.key_id,
            "evidence_key_id": self.evidence_identities.key_id,
            "state_integrity_key_id": self.state_authority.key_id,
        }
        mismatched = [
            name for name, current in expected.items() if payload.get(name) != current
        ]
        if mismatched:
            raise StateStoreError(
                "configured keys do not match the legacy organism state"
            )
        self._verify_legacy_security(payload)
        migrated = migrate_v4_to_v5(payload)
        self._seal_security(migrated)
        return migrated

    def _update(self, now: datetime, operation: Callable[[dict], T]) -> T:
        def secured_operation(payload: dict) -> T:
            result = operation(payload)
            self._seal_security(payload)
            return result

        return self.store.update(
            self._fresh_factory(now),
            secured_operation,
            migrate=self._migrate_state,
        )

    def _read(self, now: datetime, operation: Callable[[dict], T]) -> T:
        """Read a validated snapshot, creating only a missing initial state."""

        payload = self.store.read_or_create(
            self._fresh_factory(now),
            migrate=self._migrate_state,
        )
        return operation(payload)

    def _subject_key(self, scope: str, actor_ref: str | None) -> str | None:
        if actor_ref is None:
            return None
        return self.identities.subject_key(scope, actor_ref)

    @staticmethod
    def _attached_subjects(payload: dict) -> dict[str, str]:
        raw = payload.get("attached_subjects")
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise StateStoreError("organism attached_subjects is invalid")
        normalized: dict[str, str] = {}
        for observer_subject, target_subject in raw.items():
            if not isinstance(observer_subject, str) or not isinstance(
                target_subject, str
            ):
                raise StateStoreError("organism attached_subjects is invalid")
            normalized[observer_subject] = target_subject
        return normalized

    def _resolved_subject_key(
        self, payload: dict, scope: str, actor_ref: str | None
    ) -> str | None:
        subject_key = self._subject_key(scope, actor_ref)
        if subject_key is None:
            return None
        return self._attached_subjects(payload).get(subject_key, subject_key)

    def _assert_key_compatibility(self, payload: dict) -> None:
        expected = {
            "identity_key_id": self.identities.key_id,
            "learning_key_id": self.authority.key_id,
            "owner_key_id": self.owner_authority.key_id,
            "evidence_key_id": self.evidence_identities.key_id,
            "state_integrity_key_id": self.state_authority.key_id,
        }
        mismatched = [
            name for name, current in expected.items() if payload.get(name) != current
        ]
        if mismatched:
            labels = ", ".join(mismatched)
            raise StateStoreError(
                f"configured keys do not match this state file ({labels}); "
                "restore every stable key or call purge_all() to erase the state"
            )
        self._verify_security(payload)

    def _verify_evidence_integrity(self, ledger: list[dict]) -> None:
        def verified(record: dict) -> bool:
            authority_name = record.get("authority")
            authority = {
                "learning": self.authority,
                "owner": self.owner_authority,
            }.get(authority_name)
            return (
                authority is not None
                and authority.verifies_record(record)
                and evidence_semantic_rejection(record) is None
            )

        if any(not verified(record) for record in ledger):
            raise StateStoreError(
                "organism evidence authentication or semantics failed; refuse "
                "to use or modify the ledger"
            )

    @staticmethod
    def _evidence_partition(record: dict) -> str:
        return str(record.get("subject_key") or record.get("scope_key") or "global")

    @staticmethod
    def _is_retraction_tombstone(record: dict) -> bool:
        return record.get("kind") == FeedbackKind.RETRACTION.value

    def _assert_ledger_capacity(self, ledger: list[dict]) -> None:
        evidence = [
            record for record in ledger if not self._is_retraction_tombstone(record)
        ]
        tombstones = [
            record for record in ledger if self._is_retraction_tombstone(record)
        ]
        if len(evidence) > self.config.max_evidence_events:
            raise StateStoreError("organism evidence ledger exceeds its global cap")
        counts: dict[str, int] = {}
        for record in evidence:
            partition = self._evidence_partition(record)
            counts[partition] = counts.get(partition, 0) + 1
        if any(
            count > self.config.max_evidence_per_partition for count in counts.values()
        ):
            raise StateStoreError("organism evidence partition exceeds its cap")
        if len(tombstones) > self.config.max_retraction_tombstones:
            raise StateStoreError(
                "organism retraction tombstones exceed their global cap"
            )
        tombstone_counts: dict[str, int] = {}
        for record in tombstones:
            partition = self._evidence_partition(record)
            tombstone_counts[partition] = tombstone_counts.get(partition, 0) + 1
        if any(
            count > self.config.max_retraction_tombstones_per_partition
            for count in tombstone_counts.values()
        ):
            raise StateStoreError(
                "organism retraction tombstone partition exceeds its cap"
            )

    def _evidence_capacity_rejection(
        self, ledger: list[dict], record: dict
    ) -> str | None:
        evidence = [item for item in ledger if not self._is_retraction_tombstone(item)]
        owner_or_global = (
            record.get("authority") == "owner"
            or record.get("global_calibration_eligible") is True
        )
        if len(evidence) >= self.config.max_evidence_events:
            return "evidence_capacity"
        if not owner_or_global:
            ordinary_count = sum(
                1
                for item in evidence
                if item.get("authority") != "owner"
                and item.get("global_calibration_eligible") is not True
            )
            ordinary_limit = (
                self.config.max_evidence_events
                - self.config.owner_reserved_evidence_events
            )
            if ordinary_count >= ordinary_limit:
                return "evidence_owner_reserve"
        partition = self._evidence_partition(record)
        partition_count = sum(
            1 for item in evidence if self._evidence_partition(item) == partition
        )
        if partition_count >= self.config.max_evidence_per_partition:
            return "evidence_partition_capacity"
        return None

    def _tombstone_capacity_rejection(
        self, ledger: list[dict], record: dict
    ) -> str | None:
        tombstones = [item for item in ledger if self._is_retraction_tombstone(item)]
        if len(tombstones) >= self.config.max_retraction_tombstones:
            return "retraction_tombstone_capacity"
        partition = self._evidence_partition(record)
        partition_count = sum(
            1 for item in tombstones if self._evidence_partition(item) == partition
        )
        if partition_count >= self.config.max_retraction_tombstones_per_partition:
            return "retraction_tombstone_partition_capacity"
        return None

    def learning_audience(self) -> str:
        """Return this datastore's public signing audience identifier."""
        now = self._now()

        def operation(payload: dict) -> str:
            self._assert_key_compatibility(payload)
            self._assert_monotonic_clock(payload, now)
            audience = payload.get("learning_audience")
            if not isinstance(audience, str):
                raise StateStoreError("organism learning audience is invalid")
            return audience

        return self._read(now, operation)

    def _prune_authorizations(
        self, payload: dict, now: datetime
    ) -> dict[str, set[str]]:
        entries = payload.get("used_authorizations")
        if not isinstance(entries, list):
            raise StateStoreError("organism authorization history is invalid")
        retained = [
            dict(entry)
            for entry in entries
            if parse_timestamp(entry["expires_at"]) > now
        ]
        by_authority: dict[str, set[str]] = {"learning": set(), "owner": set()}
        for entry in retained:
            authority = str(entry["authority"])
            if authority not in by_authority:
                raise StateStoreError("organism authorization role is invalid")
            by_authority[authority].add(str(entry["token"]))
        if any(
            len(tokens) > self.config.max_authorization_tokens
            for tokens in by_authority.values()
        ):
            raise StateStoreError(
                "organism per-authority authorization history exceeds its cap"
            )
        payload["used_authorizations"] = retained
        return by_authority

    def _assert_monotonic_clock(self, payload: dict, now: datetime) -> None:
        try:
            previous = parse_timestamp(payload["updated_at"])
        except (KeyError, TypeError, ValueError) as error:
            raise StateStoreError("organism updated_at is invalid") from error
        if previous > now + timedelta(seconds=self.config.max_future_skew_seconds):
            raise StateStoreError(
                "system clock moved backwards beyond the allowed skew; refusing "
                "to reactivate expired state"
            )

    @staticmethod
    def _mark_updated(payload: dict, now: datetime) -> None:
        previous = parse_timestamp(payload["updated_at"])
        payload["updated_at"] = timestamp_text(max(previous, now))

    def _state_slot(
        self,
        *,
        scope: str,
        actor_ref: str | None,
        capability: Capability,
    ) -> str:
        if actor_ref is not None:
            transient_subject = self.transient_identities.subject_key(scope, actor_ref)
            base = f"subject:{transient_subject}"
        else:
            base = f"scope:{self.transient_identities.scope_key(scope)}"
        return f"{base}:{capability.value}"

    def _prune_transient_states(
        self, payload: dict, now: datetime, *, keep: str | None = None
    ) -> None:
        states = payload.setdefault("states", {})
        modes = payload.setdefault("dominant_modes", {})
        access = payload.setdefault("state_access", {})
        observations = payload.setdefault("recent_observations", {})
        if not all(
            isinstance(value, dict) for value in (states, modes, access, observations)
        ):
            raise StateStoreError("organism transient-state structure is invalid")
        cutoff = now - timedelta(hours=self.config.transient_state_retention_hours)
        stale = set()
        for state_key in states:
            accessed_at = access.get(state_key)
            try:
                expired = (
                    not isinstance(accessed_at, str)
                    or parse_timestamp(accessed_at) < cutoff
                )
            except ValueError:
                expired = True
            if expired:
                stale.add(state_key)
        for state_key in stale:
            states.pop(state_key, None)
            modes.pop(state_key, None)
            access.pop(state_key, None)
            observations.pop(state_key, None)

        if len(states) > self.config.max_transient_states:
            candidates = sorted(
                (key for key in states if key != keep),
                key=lambda key: str(access.get(key, "")),
            )
            excess = len(states) - self.config.max_transient_states
            for state_key in candidates[:excess]:
                states.pop(state_key, None)
                modes.pop(state_key, None)
                access.pop(state_key, None)
                observations.pop(state_key, None)
        for state_key in tuple(observations):
            if state_key not in states:
                observations.pop(state_key, None)

        total_observations = sum(
            len(history)
            for history in observations.values()
            if isinstance(history, list)
        )
        excess_observations = max(
            0,
            total_observations - _MAX_TOTAL_RECENT_OBSERVATIONS,
        )
        if excess_observations:
            # Discard the oldest replay history from least-recently used slots
            # first. The currently active slot is trimmed last.
            candidates = sorted(
                observations,
                key=lambda key: (key == keep, str(access.get(key, ""))),
            )
            for state_key in candidates:
                history = observations.get(state_key)
                if not isinstance(history, list) or not history:
                    continue
                remove = min(excess_observations, len(history))
                del history[:remove]
                excess_observations -= remove
                if not history:
                    observations.pop(state_key, None)
                if not excess_observations:
                    break
        if payload.get("last_state_key") not in states:
            payload["last_state_key"] = keep if keep in states else None

    def _observation_fingerprint(
        self, observation: Observation, transient_subject_key: str | None
    ) -> str:
        # Scope and actor input affect the digest but are never persisted in clear
        # text.  A fingerprint prevents one idempotency key being reused with a
        # different observation payload.
        canonical = json.dumps(
            {
                "scope": observation.scope,
                "subject_key": transient_subject_key,
                "actor_present": observation.actor_ref is not None,
                "capability": observation.capability.value,
                "cues": observation.cues.to_dict(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return self.transient_identities.content_key(
            "organism.observation-content.v2", canonical
        )

    def _rebuild_learning(self, payload: dict, now: datetime) -> list[dict]:
        self._assert_key_compatibility(payload)
        self._assert_monotonic_clock(payload, now)
        self._prune_authorizations(payload, now)
        raw_ledger = [dict(record) for record in payload.get("evidence", ())]
        self._verify_evidence_integrity(raw_ledger)
        ledger = prune_ledger(raw_ledger, now)
        self._assert_ledger_capacity(ledger)
        active = active_records(ledger, now)
        cue_weights, strategy_weights, capacities = rebuild_growth(
            active, self.config, now
        )
        previous_revision = int(payload.get("learned", {}).get("weight_revision", 0))
        payload["evidence"] = ledger
        payload["learned"] = {
            "cue_weights": cue_weights,
            "strategy_weights": strategy_weights,
            "capacities": capacities,
            "weight_revision": previous_revision,
        }
        return active

    def feel(self, observation: Observation) -> FeelingPacket:
        """Update isolated transient state and return response-style weights."""
        if not isinstance(observation, Observation):
            raise ValueError("observation must be an Observation instance")
        now = self._now()
        occurred_at = observation.occurred_at or now
        if occurred_at > now + timedelta(seconds=self.config.max_future_skew_seconds):
            raise ValueError("observation occurred_at is too far in the future")
        if occurred_at < now - timedelta(
            seconds=self.config.max_observation_age_seconds
        ):
            raise ValueError("observation occurred_at is too old for transient state")
        state_slot = self._state_slot(
            scope=observation.scope,
            actor_ref=observation.actor_ref,
            capability=observation.capability,
        )
        transient_subject_key = (
            self.transient_identities.subject_key(
                observation.scope, observation.actor_ref
            )
            if observation.actor_ref is not None
            else None
        )
        observation_key = self.transient_identities.event_key(
            observation.scope, observation.event_id
        )
        observation_fingerprint = self._observation_fingerprint(
            observation, transient_subject_key
        )

        def operation(payload: dict) -> FeelingPacket:
            active = self._rebuild_learning(payload, now)
            learned = payload["learned"]
            subject_key = self._resolved_subject_key(
                payload,
                observation.scope,
                observation.actor_ref,
            )
            self._prune_transient_states(payload, now, keep=state_slot)
            states = payload["states"]
            dominant_modes = payload["dominant_modes"]
            if state_slot in states:
                previous_state = OrganismicState.from_dict(states[state_slot])
            else:
                previous_state = self._baseline_state(now)
            try:
                previous_mode = AutonomicMode(dominant_modes.get(state_slot))
            except (TypeError, ValueError):
                previous_mode = None
            profile = profile_metrics(
                active,
                subject_key,
                observation.capability,
                now,
                self.config,
            )
            predicted = predict_risk(
                observation.cues, learned["cue_weights"], self.config
            )
            protection = protective_summary(
                observation.cues, predicted, profile, self.config
            )
            historical_bias = historical_state_bias(profile, self.config)
            recent_by_state = payload["recent_observations"]
            recent = []
            for value in recent_by_state.get(state_slot, ()):
                if isinstance(value, str):
                    recent.append({"event_key": value, "fingerprint": None})
                elif isinstance(value, dict) and isinstance(
                    value.get("event_key"), str
                ):
                    recent.append(
                        {
                            "event_key": value["event_key"],
                            "fingerprint": value.get("fingerprint"),
                        }
                    )
            existing = next(
                (item for item in recent if item["event_key"] == observation_key),
                None,
            )
            if existing is not None and existing.get("fingerprint") not in (
                None,
                observation_fingerprint,
            ):
                raise ValueError(
                    "observation event_id was reused with a different payload"
                )
            if existing is not None:
                existing["fingerprint"] = observation_fingerprint
                state = recover_state(previous_state, self.config, now)
            else:
                state, predicted = transition_state(
                    previous_state,
                    observation.cues,
                    learned["cue_weights"],
                    learned["capacities"],
                    historical_bias,
                    self.config,
                    now,
                )
                recent.append(
                    {
                        "event_key": observation_key,
                        "fingerprint": observation_fingerprint,
                    }
                )
                recent = recent[-self.config.recent_observation_limit :]
            motifs = derive_regulatory_motifs(
                observation.cues,
                state,
                predicted,
            )
            modes, dominant, guidance, recruitments = synthesize_autonomic_response(
                state=state,
                cues=observation.cues,
                protection=protection,
                predicted_risk=predicted,
                strategy_affinities=learned["strategy_weights"],
                previous_mode=previous_mode,
                config=self.config,
                motifs=motifs,
            )
            states[state_slot] = state.to_dict()
            dominant_modes[state_slot] = dominant.value
            payload["state_access"][state_slot] = timestamp_text(now)
            payload["last_state_key"] = state_slot
            recent_by_state[state_slot] = recent
            self._prune_transient_states(payload, now, keep=state_slot)
            self._mark_updated(payload, now)
            return FeelingPacket(
                generated_at=now,
                expires_at=now + timedelta(seconds=self.config.prompt_ttl_seconds),
                state=state,
                modes=modes,
                dominant_mode=dominant,
                guidance=guidance,
                protection=protection,
                recruitments=recruitments,
                motifs=motifs,
            )

        return self._update(now, operation)

    def learn(
        self, feedback: Feedback, *, authorization: str | None = None
    ) -> LearningReceipt:
        """Persist only explicit, verified, structured outcome evidence."""
        if not isinstance(feedback, Feedback):
            raise ValueError("feedback must be a Feedback instance")
        now = self._now()
        authority = (
            self.owner_authority
            if feedback.source is FeedbackSource.OWNER_OVERRIDE
            else self.authority
        )
        authority_role = (
            "owner" if feedback.source is FeedbackSource.OWNER_OVERRIDE else "learning"
        )
        audience = self.learning_audience()
        issued_at = authority.authorization_time(
            feedback, authorization, audience=audience
        )
        if not authority.enabled or issued_at is None:
            payload = self.store.read(self._fresh_factory(now))
            self._assert_key_compatibility(payload)
            revision = int(payload.get("learned", {}).get("weight_revision", 0))
            status = (
                "ignored_learning_disabled"
                if not authority.enabled
                else "ignored_unauthorized"
            )
            return LearningReceipt(
                status=status,
                event_key="",
                durable_learning_applied=False,
                actor_memory_applied=False,
                weight_revision=revision,
            )
        if issued_at > now + timedelta(seconds=self.config.max_future_skew_seconds) or (
            issued_at <= now - timedelta(seconds=self.config.authorization_ttl_seconds)
        ):
            payload = self.store.read(self._fresh_factory(now))
            self._assert_key_compatibility(payload)
            revision = int(payload.get("learned", {}).get("weight_revision", 0))
            return LearningReceipt(
                status="ignored_stale_authorization",
                event_key="",
                durable_learning_applied=False,
                actor_memory_applied=False,
                weight_revision=revision,
            )
        if not isinstance(authorization, str):  # narrowed by authorization_time()
            raise ValueError("authorization must be a string")
        authorization_token = authority.authorization_token(authorization)
        authorization_expires_at = issued_at + timedelta(
            seconds=self.config.authorization_ttl_seconds
        )
        event_key = self.evidence_identities.event_key(
            feedback.scope, feedback.event_id
        )
        rejection = feedback_rejection(feedback)
        if rejection is not None:
            payload = self.store.read(self._fresh_factory(now))
            self._assert_key_compatibility(payload)
            self._assert_monotonic_clock(payload, now)
            revision = int(payload.get("learned", {}).get("weight_revision", 0))
            return LearningReceipt(
                status=f"ignored_{rejection}",
                event_key=event_key,
                durable_learning_applied=False,
                actor_memory_applied=False,
                weight_revision=revision,
            )
        if feedback.actor_ref is not None and not self.identities.enabled:
            payload = self.store.read(self._fresh_factory(now))
            self._assert_key_compatibility(payload)
            revision = int(payload.get("learned", {}).get("weight_revision", 0))
            return LearningReceipt(
                status="ignored_identity_memory_disabled",
                event_key=event_key,
                durable_learning_applied=False,
                actor_memory_applied=False,
                weight_revision=revision,
            )
        record = evidence_record(feedback, self.evidence_identities, self.config, now)
        record["authority"] = authority_role

        def operation(payload: dict) -> LearningReceipt:
            self._assert_key_compatibility(payload)
            self._assert_monotonic_clock(payload, now)
            raw_ledger = [dict(item) for item in payload.get("evidence", ())]
            self._verify_evidence_integrity(raw_ledger)
            ledger = prune_ledger(raw_ledger, now)
            self._assert_ledger_capacity(ledger)
            current_issued_at = authority.authorization_time(
                feedback,
                authorization,
                audience=str(payload.get("learning_audience")),
            )
            previous_revision = int(
                payload.get("learned", {}).get("weight_revision", 0)
            )
            if current_issued_at is None:
                return LearningReceipt(
                    status="ignored_wrong_audience",
                    event_key="",
                    durable_learning_applied=False,
                    actor_memory_applied=False,
                    weight_revision=previous_revision,
                )
            used_tokens = self._prune_authorizations(payload, now)[authority_role]
            token_already_used = authorization_token in used_tokens

            def consume_authorization() -> None:
                if token_already_used:
                    return
                payload["used_authorizations"].append(
                    {
                        "authority": authority_role,
                        "token": authorization_token,
                        "expires_at": timestamp_text(authorization_expires_at),
                    }
                )

            def ignored(status: str, *, consume: bool = True) -> LearningReceipt:
                payload["evidence"] = ledger
                if consume:
                    consume_authorization()
                self._mark_updated(payload, now)
                return LearningReceipt(
                    status=status,
                    event_key=event_key,
                    durable_learning_applied=False,
                    actor_memory_applied=False,
                    weight_revision=previous_revision,
                )

            duplicate = next(
                (item for item in ledger if item.get("event_key") == event_key),
                None,
            )
            if duplicate is not None:
                if duplicate.get("event_fingerprint") not in (
                    None,
                    record.get("event_fingerprint"),
                ):
                    raise ValueError(
                        "feedback event_id was reused with a different payload"
                    )
                if (
                    not token_already_used
                    and len(used_tokens) >= self.config.max_authorization_tokens
                ):
                    return ignored("ignored_authorization_capacity", consume=False)
                return ignored("duplicate")
            if token_already_used:
                return ignored("ignored_replayed_authorization", consume=False)
            if len(used_tokens) >= self.config.max_authorization_tokens:
                return ignored("ignored_authorization_capacity", consume=False)
            if feedback.kind is not FeedbackKind.RETRACTION and any(
                item.get("kind") == FeedbackKind.RETRACTION.value
                and item.get("retracts_event_key") == event_key
                for item in ledger
            ):
                return ignored("ignored_retracted_event_id")
            if (
                feedback.kind is not FeedbackKind.RETRACTION
                and float(record["learning_strength"]) <= 1e-9
            ):
                return ignored("ignored_zero_learning_strength")
            adverse_rejection = adverse_admission_rejection(
                record, active_records(ledger, now)
            )
            if adverse_rejection is not None:
                return ignored(f"ignored_{adverse_rejection}")
            healing_rejection = healing_admission_rejection(
                record, active_records(ledger, now)
            )
            if healing_rejection is not None:
                return ignored(f"ignored_{healing_rejection}")
            if feedback.kind is FeedbackKind.RETRACTION:
                target_key = record.get("retracts_event_key")
                if any(
                    item.get("kind") == FeedbackKind.RETRACTION.value
                    and item.get("retracts_event_key") == target_key
                    for item in ledger
                ):
                    return ignored("ignored_already_retracted")
                target = next(
                    (
                        item
                        for item in ledger
                        if item.get("event_key") == target_key
                        and item.get("kind") != FeedbackKind.RETRACTION.value
                    ),
                    None,
                )
                if target is None:
                    return ignored("ignored_unknown_retraction_target")
                # Bind the tombstone to its actual target so subject deletion
                # removes both and can never reactivate another profile.
                for field_name in (
                    "subject_key",
                    "scope_key",
                    "actor_scoped",
                    "capability",
                ):
                    record[field_name] = target.get(field_name)
                record["observed_at"] = timestamp_text(now)
                record["ingested_at"] = timestamp_text(now)
                record["expires_at"] = target["expires_at"]
                tombstone_rejection = self._tombstone_capacity_rejection(ledger, record)
                if tombstone_rejection is not None:
                    return ignored(f"ignored_{tombstone_rejection}")
                ledger = [
                    item for item in ledger if item.get("event_key") != target_key
                ]
            else:
                capacity_rejection = self._evidence_capacity_rejection(ledger, record)
                if capacity_rejection is not None:
                    return ignored(f"ignored_{capacity_rejection}")
            record["record_authentication"] = authority.authenticate_record(record)
            consume_authorization()
            ledger.append(record)
            payload["evidence"] = prune_ledger(
                ledger,
                now,
            )
            self._seal_security(payload)
            self._rebuild_learning(payload, now)
            revision = previous_revision + 1
            payload["learned"]["weight_revision"] = revision
            self._mark_updated(payload, now)
            affects_actor = record.get("subject_key") is not None and feedback.kind in (
                FeedbackKind.ADVERSE,
                FeedbackKind.SAFE,
                FeedbackKind.REPAIR,
            )
            return LearningReceipt(
                status="accepted",
                event_key=event_key,
                durable_learning_applied=True,
                actor_memory_applied=affects_actor,
                weight_revision=revision,
            )

        return self._update(now, operation)

    def heal(
        self,
        *,
        hours: float = 1.0,
        support: float = 0.5,
        scope: str | None = None,
        actor_ref: str | None = None,
        capability: Capability = Capability.CONVERSATION,
    ) -> OrganismicState:
        """Advance one selected transient state toward baseline.

        With no scope, the most recently used state is selected. Supplying a
        scope selects that scope/actor/capability namespace. Use ``heal_all``
        only for an explicit operator-wide recovery action.
        """
        if (
            isinstance(hours, bool)
            or not isinstance(hours, (int, float))
            or not math.isfinite(float(hours))
            or hours < 0
        ):
            raise ValueError("hours must be a finite non-negative number")
        support = unit(support, name="support")
        selected_capability = Capability(capability)
        if scope is None and actor_ref is not None:
            raise ValueError("actor_ref requires scope")
        now = self._now()

        def operation(payload: dict) -> OrganismicState:
            self._rebuild_learning(payload, now)
            self._prune_transient_states(payload, now)
            states = payload["states"]
            dominant_modes = payload["dominant_modes"]
            if scope is None:
                selected_key = payload.get("last_state_key")
            else:
                selected_key = self._state_slot(
                    scope=scope,
                    actor_ref=actor_ref,
                    capability=selected_capability,
                )
            if not isinstance(selected_key, str) or selected_key not in states:
                state = self._baseline_state(now)
            else:
                previous = OrganismicState.from_dict(states[selected_key])
                state = recover_state(
                    previous,
                    self.config,
                    now,
                    additional_hours=float(hours),
                    support=support,
                )
                modes = mode_weights(state, self.config)
                try:
                    prior_mode = AutonomicMode(dominant_modes.get(selected_key))
                except (TypeError, ValueError):
                    prior_mode = None
                states[selected_key] = state.to_dict()
                dominant_modes[selected_key] = dominant_mode(
                    modes, prior_mode, self.config
                ).value
                payload["state_access"][selected_key] = timestamp_text(now)
                payload["last_state_key"] = selected_key
            self._mark_updated(payload, now)
            return state

        return self._update(now, operation)

    def heal_all(self, *, hours: float = 1.0, support: float = 0.5) -> int:
        """Explicitly recover every transient namespace and return its count."""

        if (
            isinstance(hours, bool)
            or not isinstance(hours, (int, float))
            or not math.isfinite(float(hours))
            or hours < 0
        ):
            raise ValueError("hours must be a finite non-negative number")
        support = unit(support, name="support")
        now = self._now()

        def operation(payload: dict) -> int:
            self._rebuild_learning(payload, now)
            self._prune_transient_states(payload, now)
            states = payload["states"]
            dominant_modes = payload["dominant_modes"]
            for state_key, state_payload in list(states.items()):
                previous = OrganismicState.from_dict(state_payload)
                healed = recover_state(
                    previous,
                    self.config,
                    now,
                    additional_hours=float(hours),
                    support=support,
                )
                modes = mode_weights(healed, self.config)
                try:
                    prior_mode = AutonomicMode(dominant_modes.get(state_key))
                except (TypeError, ValueError):
                    prior_mode = None
                states[state_key] = healed.to_dict()
                dominant_modes[state_key] = dominant_mode(
                    modes, prior_mode, self.config
                ).value
                payload["state_access"][state_key] = timestamp_text(now)
            self._mark_updated(payload, now)
            return len(states)

        return self._update(now, operation)

    def reset_state(self) -> OrganismicState:
        """Reset transient state while retaining reviewed learning evidence."""
        now = self._now()

        def operation(payload: dict) -> OrganismicState:
            self._assert_key_compatibility(payload)
            self._assert_monotonic_clock(payload, now)
            state = self._baseline_state(now)
            payload["states"] = {}
            payload["dominant_modes"] = {}
            payload["state_access"] = {}
            payload["last_state_key"] = None
            payload["recent_observations"] = {}
            self._mark_updated(payload, now)
            return state

        return self._update(now, operation)

    def forget(self, *, scope: str, actor_ref: str) -> int:
        """Hard-delete all retained evidence for one scoped pseudonymous actor."""
        subject_key = self.identities.subject_key(scope, actor_ref)
        transient_subject_key = self.transient_identities.subject_key(scope, actor_ref)
        now = self._now()

        def operation(payload: dict) -> int:
            self._assert_key_compatibility(payload)
            raw_ledger = [dict(record) for record in payload.get("evidence", ())]
            self._verify_evidence_integrity(raw_ledger)
            ledger = prune_ledger(raw_ledger, now)
            self._assert_ledger_capacity(ledger)
            if subject_key is None:
                retained, removed = ledger, 0
            else:
                retained, removed = forget_subject(ledger, subject_key)
            payload["evidence"] = retained
            state_prefix = (
                f"subject:{transient_subject_key}:"
                if self.identities.enabled
                else "subject:"
            )
            removed_states = 0
            for state_key in list(payload.get("states", {})):
                if state_key.startswith(state_prefix):
                    removed_states += 1
                    payload["states"].pop(state_key, None)
                    payload.get("dominant_modes", {}).pop(state_key, None)
                    payload.get("state_access", {}).pop(state_key, None)
                    payload.get("recent_observations", {}).pop(state_key, None)
            last_state_key = payload.get("last_state_key")
            if isinstance(last_state_key, str) and last_state_key.startswith(
                state_prefix
            ):
                payload["last_state_key"] = None
            previous_revision = int(
                payload.get("learned", {}).get("weight_revision", 0)
            )
            self._seal_security(payload)
            self._rebuild_learning(payload, now)
            if removed:
                payload["learned"]["weight_revision"] = previous_revision + 1
            self._mark_updated(payload, now)
            return removed + removed_states

        return self._update(now, operation)

    def inspect_profile(
        self,
        *,
        scope: str,
        actor_ref: str,
        capability: Capability = Capability.CONVERSATION,
    ) -> dict[str, object]:
        """Return aggregates only; never return the pseudonymous key or ledger."""

        return self.inspect_profiles(
            scope=scope,
            actor_refs=(actor_ref,),
            capability=capability,
        )[0]

    def inspect_profiles(
        self,
        *,
        scope: str,
        actor_refs: Iterable[str],
        capability: Capability = Capability.CONVERSATION,
    ) -> tuple[dict[str, object], ...]:
        """Read ordered aggregate profiles in one authenticated store snapshot."""

        selected_scope = bounded_text(scope, name="scope")
        if isinstance(actor_refs, (str, bytes)):
            raise TypeError("actor_refs must be an iterable of actor strings")
        selected_actors: list[str] = []
        for index, actor_ref in enumerate(actor_refs):
            if index >= 64:
                raise ValueError("actor_refs may contain at most 64 entries")
            selected_actors.append(
                bounded_text(actor_ref, name="actor_ref", maximum=512)
            )
        try:
            selected_capability = Capability(capability)
        except (TypeError, ValueError) as error:
            raise ValueError("capability is not supported") from error
        if not selected_actors:
            return ()
        now = self._now()

        def operation(payload: dict) -> tuple[dict[str, object], ...]:
            active = self._rebuild_learning(payload, now)
            self._prune_transient_states(payload, now)
            attached = self._attached_subjects(payload)

            def subject_key(actor_ref: str) -> str | None:
                direct = self._subject_key(selected_scope, actor_ref)
                return attached.get(direct, direct) if direct is not None else None

            def profile_view(actor_ref: str) -> dict[str, object]:
                metrics = profile_metrics(
                    active,
                    subject_key(actor_ref),
                    selected_capability,
                    now,
                    self.config,
                )
                return {
                    "view_schema_version": "organism.profile-inspect.v1",
                    "identity_memory_enabled": self.identities.enabled,
                    "capability": selected_capability.value,
                    "guard_activation": metrics["activation"],
                    **metrics,
                }

            return tuple(profile_view(actor_ref) for actor_ref in selected_actors)

        return self._read(now, operation)

    def inspect(self) -> dict[str, object]:
        """Return a sanitized operator view without identities or evidence rows."""
        now = self._now()

        def operation(payload: dict) -> dict[str, object]:
            active = self._rebuild_learning(payload, now)
            self._prune_transient_states(payload, now)
            ledger = payload["evidence"]
            learned = payload["learned"]
            last_state_key = payload.get("last_state_key")
            states = payload["states"]
            if last_state_key in states:
                state = OrganismicState.from_dict(states[last_state_key])
                dominant = payload["dominant_modes"].get(last_state_key)
            else:
                state = self._baseline_state(now)
                baseline_modes = mode_weights(state, self.config)
                dominant = dominant_mode(
                    baseline_modes,
                    None,
                    self.config,
                ).value
            state_payload = state.to_dict()
            return {
                "view_schema_version": "organism.inspect.v1",
                "state_schema_version": payload["schema_version"],
                "schema_version": payload["schema_version"],
                "last_transient_state": state_payload,
                "state": dict(state_payload),
                "dominant_mode": dominant,
                "transient_state_count": len(states),
                "identity_memory_enabled": self.identities.enabled,
                "durable_learning_enabled": (
                    self.authority.enabled or self.owner_authority.enabled
                ),
                "ordinary_learning_enabled": self.authority.enabled,
                "owner_learning_enabled": self.owner_authority.enabled,
                "retained_evidence_count": len(ledger),
                "active_evidence_count": len(active),
                "learned": {
                    "cue_weights": dict(learned["cue_weights"]),
                    "strategy_weights": dict(learned["strategy_weights"]),
                    "capacities": dict(learned["capacities"]),
                    "weight_revision": int(learned["weight_revision"]),
                },
            }

        return self._read(now, operation)

    def purge_expired(self) -> dict[str, int]:
        """Physically remove expired evidence and transient namespaces now."""
        now = self._now()

        def operation(payload: dict) -> dict[str, int]:
            evidence_before = len(payload.get("evidence", ()))
            states_before = len(payload.get("states", {}))
            self._rebuild_learning(payload, now)
            self._prune_transient_states(payload, now)
            self._mark_updated(payload, now)
            return {
                "evidence_removed": evidence_before - len(payload["evidence"]),
                "transient_states_removed": states_before - len(payload["states"]),
            }

        return self._update(now, operation)

    def purge_all(self) -> dict[str, int]:
        """Erase all state and evidence, including data tied to a lost key."""
        now = self._now()

        def operation(payload: dict) -> dict[str, int]:
            removed = {
                "evidence_removed": len(payload.get("evidence", ())),
                "transient_states_removed": len(payload.get("states", {})),
            }
            payload.clear()
            payload.update(self._fresh(now))
            return removed

        try:
            return self._update(now, operation)
        except StateStoreError:
            existed = self.store.replace(self._fresh_factory(now))
            return {
                "evidence_removed": 0,
                "transient_states_removed": 0,
                "unreadable_state_replaced": int(existed),
            }


__all__ = ["Organism", "StateStoreError"]
