"""Authenticated, deterministic discovery queue and reviewed aggregate models.

Considering an observation may queue a neutral question, but it never updates a
model.  Only a delivered question reviewed by its specifically selected,
explicitly configured advisor can update the bounded aggregates.  Those
aggregates are advisory functional models: they cannot affect risk, autonomic
modes, permissions, moderation, or tool authority.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .models import (
    QUESTION_TEMPLATES,
    SELECTION_ENUM_BY_TOPIC,
    CuriosityStimulus,
    DiscoverySettings,
    DiscoveryTopic,
    Dispatch,
    InquirySignal,
    InquiryStatus,
    Question,
    Receipt,
    ReceiptStatus,
    ReviewDisposition,
    ReviewImpact,
    StructuredReview,
)
from ..authority import LearningAuthority
from ..identity import IdentityPseudonymizer
from ..models import (
    AutonomicMode,
    Cues,
    FeelingPacket,
    clamp,
    parse_timestamp,
    timestamp_text,
    utc_now,
)
from ..storage import JsonStateStore, StateStoreError

DISCOVERY_SCHEMA = "organism.discovery.state.v1"
DISCOVERY_INTEGRITY_SCHEMA = "organism.discovery.integrity.v1"
PERSON_TOPICS = frozenset(
    {
        DiscoveryTopic.BEHAVIOR_IMPACT,
        DiscoveryTopic.PERSPECTIVE_DISCOVERY,
    }
)
PERSON_SCORE_THRESHOLD = 0.78
MODEL_BUCKET_BY_TOPIC: Mapping[DiscoveryTopic, str] = {
    DiscoveryTopic.FUNCTIONAL_SELF: "functional_commitments",
    DiscoveryTopic.MORAL_TRADEOFF: "moral_principles",
    DiscoveryTopic.CONCEPT_RIDDLE: "riddle_kinds",
    DiscoveryTopic.BEHAVIOR_IMPACT: "behavior_exemplars",
    DiscoveryTopic.PERSPECTIVE_DISCOVERY: "perspective_kinds",
}
_MODEL_BUCKETS = frozenset(MODEL_BUCKET_BY_TOPIC.values())
_DAY = timedelta(hours=24)
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}")
_MAX_PENDING = 64
_MAX_HISTORY = 4096
_MAX_MODEL_SCOPES = 4096
_MAX_MODEL_REVIEWS = 1_000_000
_MAX_CLOCK_ROLLBACK = timedelta(minutes=5)


def _key_bytes(value: bytes | str, *, name: str) -> bytes:
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    if not isinstance(encoded, bytes) or len(encoded) < 32:
        raise ValueError(f"{name} must contain at least 32 bytes")
    return encoded


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and _HEX_DIGEST.fullmatch(value) is not None


def _is_timestamp(value: object) -> bool:
    try:
        parse_timestamp(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return True


def _is_unit(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and 0.0 <= float(value) <= 1.0
    )


_QUESTION_FIELDS = {
    "question_id",
    "scope_key",
    "event_key",
    "subject_key",
    "topic",
    "advisor_key",
    "score",
    "created_at",
    "expires_at",
    "delivered_at",
}
_HISTORY_FIELDS = _QUESTION_FIELDS | {"closed_at", "status"}
_AGGREGATE_FIELDS = {
    "review_count",
    "support",
    "challenge",
    "uncertainty",
    "positive_impact",
    "negative_impact",
    "updated_at",
}


def _valid_question_record(record: object, *, history: bool) -> bool:
    fields = _HISTORY_FIELDS if history else _QUESTION_FIELDS
    if not isinstance(record, dict) or set(record) != fields:
        return False
    try:
        DiscoveryTopic(record["topic"])
    except (TypeError, ValueError):
        return False
    if not all(
        _is_digest(record.get(name))
        for name in ("question_id", "scope_key", "event_key", "advisor_key")
    ):
        return False
    if record.get("subject_key") is not None and not _is_digest(
        record.get("subject_key")
    ):
        return False
    if not _is_unit(record.get("score")):
        return False
    if not all(
        _is_timestamp(record.get(name)) for name in ("created_at", "expires_at")
    ):
        return False
    created_at = parse_timestamp(record["created_at"])
    expires_at = parse_timestamp(record["expires_at"])
    if expires_at <= created_at:
        return False
    delivered = record.get("delivered_at")
    if delivered is not None and not _is_timestamp(delivered):
        return False
    delivered_at = parse_timestamp(delivered) if delivered is not None else None
    if delivered_at is not None and not created_at <= delivered_at < expires_at:
        return False
    if history:
        if record.get("status") not in {"expired", "reviewed", "abstained"}:
            return False
        if not _is_timestamp(record.get("closed_at")):
            return False
        closed_at = parse_timestamp(record["closed_at"])
        if closed_at < created_at or (
            delivered_at is not None and closed_at < delivered_at
        ):
            return False
        if record["status"] in {"reviewed", "abstained"} and delivered_at is None:
            return False
    return True


def _valid_aggregate(record: object) -> bool:
    return (
        isinstance(record, dict)
        and set(record) == _AGGREGATE_FIELDS
        and type(record.get("review_count")) is int
        and 1 <= int(record["review_count"]) <= _MAX_MODEL_REVIEWS
        and all(
            _is_unit(record.get(name))
            for name in (
                "support",
                "challenge",
                "uncertainty",
                "positive_impact",
                "negative_impact",
            )
        )
        and _is_timestamp(record.get("updated_at"))
    )


def _validate_discovery_state(payload: dict) -> None:
    expected = {
        "schema_version",
        "identity_key_id",
        "integrity_key_id",
        "state_authentication",
        "pending",
        "history",
        "models",
        "revision",
        "updated_at",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise StateStoreError("discovery state has an invalid top-level structure")
    if payload.get("schema_version") != DISCOVERY_SCHEMA:
        raise StateStoreError("discovery state uses an unsupported schema")
    if not isinstance(payload.get("identity_key_id"), str) or not isinstance(
        payload.get("integrity_key_id"), str
    ):
        raise StateStoreError("discovery key identifiers are invalid")
    authentication = payload.get("state_authentication")
    if not isinstance(authentication, str) or not _is_digest(authentication):
        raise StateStoreError("discovery state authentication is invalid")
    if type(payload.get("revision")) is not int or payload["revision"] < 0:
        raise StateStoreError("discovery revision is invalid")
    if not _is_timestamp(payload.get("updated_at")):
        raise StateStoreError("discovery updated_at is invalid")

    pending = payload.get("pending")
    if not isinstance(pending, dict) or len(pending) > _MAX_PENDING:
        raise StateStoreError("discovery pending queue is invalid")
    for question_id, record in pending.items():
        if (
            not _is_digest(question_id)
            or not _valid_question_record(record, history=False)
            or record["question_id"] != question_id
        ):
            raise StateStoreError("discovery pending question is invalid")

    history = payload.get("history")
    if not isinstance(history, list) or len(history) > _MAX_HISTORY:
        raise StateStoreError("discovery history is invalid")
    history_ids: set[str] = set()
    for record in history:
        if not _valid_question_record(record, history=True):
            raise StateStoreError("discovery history entry is invalid")
        question_id = str(record["question_id"])
        if question_id in history_ids or question_id in pending:
            raise StateStoreError("discovery question identifiers must be unique")
        history_ids.add(question_id)

    models = payload.get("models")
    if not isinstance(models, dict) or len(models) > _MAX_MODEL_SCOPES:
        raise StateStoreError("discovery models are invalid")
    for scope_key, scoped in models.items():
        if not _is_digest(scope_key) or not isinstance(scoped, dict):
            raise StateStoreError("discovery scoped model is invalid")
        if not set(scoped) <= _MODEL_BUCKETS:
            raise StateStoreError("discovery model bucket is invalid")
        for bucket_name, bucket in scoped.items():
            if not isinstance(bucket, dict):
                raise StateStoreError("discovery model selection map is invalid")
            topic = next(
                topic
                for topic, selected_bucket in MODEL_BUCKET_BY_TOPIC.items()
                if selected_bucket == bucket_name
            )
            enum_type = SELECTION_ENUM_BY_TOPIC[topic]
            for selection, aggregate in bucket.items():
                try:
                    enum_type(selection)
                except (TypeError, ValueError) as error:
                    raise StateStoreError(
                        "discovery model selection is invalid"
                    ) from error
                if not _valid_aggregate(aggregate):
                    raise StateStoreError("discovery model aggregate is invalid")


class DiscoveryEngine:
    """Maintain a bounded question queue and advisor-reviewed abstract models."""

    def __init__(
        self,
        state_path: str | Path,
        *,
        settings: DiscoverySettings | Mapping[str, object] | None = None,
        identity_key: bytes | str,
        integrity_key: bytes | str,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        identity = _key_bytes(identity_key, name="identity_key")
        integrity = _key_bytes(integrity_key, name="integrity_key")
        if identity == integrity:
            raise ValueError("identity_key and integrity_key must be distinct")
        self.settings = (
            settings
            if isinstance(settings, DiscoverySettings)
            else DiscoverySettings.from_mapping(settings)
        )
        self.identities = IdentityPseudonymizer(identity)
        self.integrity = LearningAuthority(integrity)
        self.store = JsonStateStore(state_path, validator=_validate_discovery_state)
        self.clock = clock

    def _now(self) -> datetime:
        now = self.clock()
        if not isinstance(now, datetime):
            raise ValueError("clock must return a datetime")
        return (
            now
            if now.tzinfo is not None and now.utcoffset() is not None
            else self._naive_clock()
        )

    @staticmethod
    def _naive_clock() -> datetime:
        raise ValueError("clock must return a timezone-aware datetime")

    def _fresh(self, now: datetime) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": DISCOVERY_SCHEMA,
            "identity_key_id": self.identities.key_id,
            "integrity_key_id": self.integrity.key_id,
            "state_authentication": "0" * 64,
            "pending": {},
            "history": [],
            "models": {},
            "revision": 0,
            "updated_at": timestamp_text(now),
        }
        self._seal(payload)
        return payload

    @staticmethod
    def _integrity_record(payload: Mapping[str, object]) -> dict[str, object]:
        return {
            "schema_version": DISCOVERY_INTEGRITY_SCHEMA,
            "document": {
                key: value
                for key, value in payload.items()
                if key != "state_authentication"
            },
            "record_authentication": payload.get("state_authentication"),
        }

    def _seal(self, payload: dict[str, object]) -> None:
        payload["state_authentication"] = self.integrity.authenticate_record(
            self._integrity_record(payload)
        )

    def _verify(self, payload: dict[str, object]) -> None:
        if (
            payload.get("identity_key_id") != self.identities.key_id
            or payload.get("integrity_key_id") != self.integrity.key_id
        ):
            raise StateStoreError(
                "configured discovery keys do not match this state file"
            )
        if not self.integrity.verifies_record(self._integrity_record(payload)):
            raise StateStoreError(
                "discovery state authentication failed; refusing to use or modify it"
            )

    def _update(
        self,
        now: datetime,
        operation: Callable[[dict[str, object], datetime], object],
    ) -> object:
        def secured(payload: dict) -> tuple[object, bool]:
            self._verify(payload)
            previous = self._assert_clock(payload, now)
            operation_now = max(previous, now)
            previous_revision = int(payload["revision"])
            result = operation(payload, operation_now)
            changed = int(payload["revision"]) != previous_revision
            if not changed:
                return result, False
            payload["updated_at"] = timestamp_text(operation_now)
            self._seal(payload)
            return result, True

        return self.store.update_if_changed(lambda: self._fresh(now), secured)

    def _read(
        self,
        now: datetime,
        operation: Callable[[dict[str, object]], object],
    ) -> object:
        """Authenticate and inspect state without turning a read into a write."""

        payload = self.store.read(lambda: self._fresh(now))
        self._verify(payload)
        self._assert_clock(payload, now)
        return operation(payload)

    @staticmethod
    def _assert_clock(payload: Mapping[str, object], now: datetime) -> datetime:
        previous = parse_timestamp(payload["updated_at"])
        if previous > now + _MAX_CLOCK_ROLLBACK:
            raise StateStoreError(
                "system clock moved backwards beyond the allowed skew; refusing "
                "to reactivate discovery state"
            )
        return previous

    def _advisor_key(self, advisor_id: int) -> str:
        return self.identities.content_key(
            "organism.discovery.advisor.v1", str(advisor_id)
        )

    def _question_from_record(self, record: Mapping[str, object]) -> Question:
        return Question(
            question_id=str(record["question_id"]),
            topic=DiscoveryTopic(record["topic"]),
            score=float(record["score"]),
            created_at=parse_timestamp(record["created_at"]),
            expires_at=parse_timestamp(record["expires_at"]),
            subject_present=record.get("subject_key") is not None,
        )

    def _activity(self, payload: Mapping[str, object]) -> list[Mapping[str, object]]:
        pending = payload.get("pending", {})
        history = payload.get("history", [])
        return [
            *pending.values(),  # type: ignore[union-attr]
            *history,  # type: ignore[misc]
        ]

    def _history_retention(self) -> timedelta:
        return (
            max(
                _DAY,
                timedelta(hours=self.settings.reviewer_cooldown_hours),
                timedelta(days=self.settings.same_subject_topic_days),
                timedelta(hours=self.settings.question_ttl_hours),
            )
            + _DAY
        )

    def _prune(self, payload: dict[str, object], now: datetime) -> None:
        pending: dict[str, dict[str, object]] = payload["pending"]  # type: ignore[assignment]
        history: list[dict[str, object]] = payload["history"]  # type: ignore[assignment]
        changed = False
        for question_id, record in tuple(pending.items()):
            if parse_timestamp(record["expires_at"]) > now:
                continue
            closed = dict(record)
            closed.update({"closed_at": timestamp_text(now), "status": "expired"})
            history.append(closed)
            pending.pop(question_id)
            changed = True
        cutoff = now - self._history_retention()
        retained_count = len(history)
        history[:] = [
            record
            for record in history
            if parse_timestamp(record["created_at"]) >= cutoff
        ]
        changed = changed or len(history) != retained_count
        retained_count = len(history)
        self._trim_history(history)
        changed = changed or len(history) != retained_count
        if changed:
            payload["revision"] = int(payload["revision"]) + 1

    @staticmethod
    def _trim_history(history: list[dict[str, object]]) -> None:
        if len(history) > _MAX_HISTORY:
            history.sort(key=lambda item: (item["created_at"], item["question_id"]))
            del history[: len(history) - _MAX_HISTORY]

    @staticmethod
    def _score(cues: Cues, feeling: FeelingPacket) -> float:
        mobilization = feeling.modes[AutonomicMode.MOBILIZATION]
        base = clamp(
            0.50 * feeling.motifs.riddle + 0.30 * cues.uncertainty + 0.20 * mobilization
        )
        return clamp(
            base
            * (1.0 - 0.60 * feeling.motifs.panic)
            * (1.0 - 0.35 * feeling.motifs.pain)
        )

    @staticmethod
    def _passes_safe_gates(cues: Cues, feeling: FeelingPacket) -> bool:
        return (
            feeling.motifs.riddle >= 0.60
            and cues.uncertainty >= 0.50
            and cues.safety >= 0.60
            and feeling.state.interaction_safety >= 0.60
            and cues.controllability >= 0.50
            and feeling.state.agency >= 0.50
            and cues.threat <= 0.35
            and feeling.motifs.panic <= 0.45
            and cues.social_exposure <= 0.50
        )

    @staticmethod
    def _has_topic_evidence(stimulus: CuriosityStimulus) -> bool:
        """Require bounded topic evidence; profile novelty is never evidence."""

        if stimulus.topic is DiscoveryTopic.FUNCTIONAL_SELF:
            return stimulus.functional_tension > 0.0
        if stimulus.topic is DiscoveryTopic.MORAL_TRADEOFF:
            return stimulus.authenticated_episode and stimulus.moral_tension > 0.0
        if stimulus.topic is DiscoveryTopic.CONCEPT_RIDDLE:
            return stimulus.concept_novelty > 0.0
        if stimulus.actor_ref is None or not stimulus.authenticated_episode:
            return False
        if stimulus.topic is DiscoveryTopic.BEHAVIOR_IMPACT:
            return max(stimulus.behavior_signal, stimulus.impact_signal) > 0.0
        return (
            max(
                stimulus.perspective_signal,
                stimulus.behavior_signal,
                stimulus.impact_signal,
            )
            > 0.0
        )

    def _eligible_advisor(
        self,
        activity: Iterable[Mapping[str, object]],
        now: datetime,
        *,
        question_id: str,
    ) -> int | None:
        recent_day = now - _DAY
        cooldown = now - timedelta(hours=self.settings.reviewer_cooldown_hours)
        ranked: list[tuple[int, datetime, str, int]] = []
        floor = datetime.min.replace(tzinfo=now.tzinfo)
        advisor_keys = {
            advisor_id: self._advisor_key(advisor_id)
            for advisor_id in self.settings.advisor_ids
        }
        summary: dict[str, tuple[int, datetime]] = {
            key: (0, floor) for key in advisor_keys.values()
        }
        for record in activity:
            key = str(record["advisor_key"])
            current = summary.get(key)
            if current is None:
                continue
            created_at = parse_timestamp(record["created_at"])
            summary[key] = (
                current[0] + int(created_at >= recent_day),
                max(current[1], created_at),
            )
        for advisor_id, key in advisor_keys.items():
            daily, latest = summary[key]
            if daily >= self.settings.max_per_reviewer_24h:
                continue
            if latest > cooldown:
                continue
            tie = self.identities.content_key(
                "organism.discovery.advisor-choice.v1",
                question_id,
                str(advisor_id),
            )
            ranked.append((daily, latest, tie, advisor_id))
        if not ranked:
            return None
        ranked.sort(key=lambda value: (value[0], value[1], value[2]))
        return ranked[0][3]

    def consider(
        self,
        cues: Cues,
        feeling: FeelingPacket,
        stimulus: CuriosityStimulus,
    ) -> InquirySignal:
        """Atomically evaluate and, when eligible, queue one neutral inquiry."""

        if not isinstance(cues, Cues):
            raise ValueError("cues must be a Cues instance")
        if not isinstance(feeling, FeelingPacket):
            raise ValueError("feeling must be a FeelingPacket")
        if not isinstance(stimulus, CuriosityStimulus):
            raise ValueError("stimulus must be a CuriosityStimulus")
        score = self._score(cues, feeling)
        if not self.settings.enabled:
            return InquirySignal(stimulus.topic, score, InquiryStatus.DISABLED)
        if stimulus.topic not in self.settings.allowed_topics:
            return InquirySignal(stimulus.topic, score, InquiryStatus.TOPIC_DISABLED)
        if not self._passes_safe_gates(cues, feeling):
            return InquirySignal(stimulus.topic, score, InquiryStatus.GATE_REJECTED)
        threshold = (
            max(self.settings.score_threshold, PERSON_SCORE_THRESHOLD)
            if stimulus.topic in PERSON_TOPICS
            else self.settings.score_threshold
        )
        if score < threshold:
            return InquirySignal(stimulus.topic, score, InquiryStatus.BELOW_THRESHOLD)
        if not self._has_topic_evidence(stimulus):
            return InquirySignal(
                stimulus.topic,
                score,
                (
                    InquiryStatus.PERSON_EVIDENCE_REQUIRED
                    if stimulus.topic in PERSON_TOPICS
                    else InquiryStatus.TOPIC_EVIDENCE_REQUIRED
                ),
            )

        now = self._now()
        scope_key = self.identities.scope_key(stimulus.scope_ref)
        event_key = self.identities.event_key(
            stimulus.scope_ref,
            stimulus.event_ref,
        )
        subject_key = (
            self.identities.subject_key(stimulus.scope_ref, stimulus.actor_ref)
            if stimulus.actor_ref is not None
            else None
        )
        question_id = self.identities.content_key(
            "organism.discovery.question.v1",
            stimulus.scope_ref,
            stimulus.event_ref,
            stimulus.topic.value,
        )

        def operation(
            payload: dict[str, object], operation_now: datetime
        ) -> InquirySignal:
            self._prune(payload, operation_now)
            pending: dict[str, dict[str, object]] = payload["pending"]  # type: ignore[assignment]
            activity = self._activity(payload)
            if any(record["question_id"] == question_id for record in activity):
                return InquirySignal(stimulus.topic, score, InquiryStatus.DEDUPLICATED)
            if len(pending) >= self.settings.max_pending:
                return InquirySignal(
                    stimulus.topic, score, InquiryStatus.PENDING_CAPACITY
                )
            recent_day = operation_now - _DAY
            if (
                sum(
                    record["scope_key"] == scope_key
                    and parse_timestamp(record["created_at"]) >= recent_day
                    for record in activity
                )
                >= self.settings.max_per_scope_24h
            ):
                return InquirySignal(stimulus.topic, score, InquiryStatus.RATE_LIMITED)
            if subject_key is not None:
                subject_cutoff = operation_now - timedelta(
                    days=self.settings.same_subject_topic_days
                )
                if any(
                    record.get("subject_key") == subject_key
                    and record["topic"] == stimulus.topic.value
                    and parse_timestamp(record["created_at"]) >= subject_cutoff
                    for record in activity
                ):
                    return InquirySignal(
                        stimulus.topic, score, InquiryStatus.RATE_LIMITED
                    )
            advisor_id = self._eligible_advisor(
                activity,
                operation_now,
                question_id=question_id,
            )
            if advisor_id is None:
                return InquirySignal(stimulus.topic, score, InquiryStatus.RATE_LIMITED)
            expires_at = operation_now + timedelta(
                hours=self.settings.question_ttl_hours
            )
            record = {
                "question_id": question_id,
                "scope_key": scope_key,
                "event_key": event_key,
                "subject_key": subject_key,
                "topic": stimulus.topic.value,
                "advisor_key": self._advisor_key(advisor_id),
                "score": score,
                "created_at": timestamp_text(operation_now),
                "expires_at": timestamp_text(expires_at),
                "delivered_at": None,
            }
            pending[question_id] = record
            payload["revision"] = int(payload["revision"]) + 1
            question = self._question_from_record(record)
            return InquirySignal(
                stimulus.topic,
                score,
                InquiryStatus.QUEUED,
                question,
            )

        return self._update(now, operation)  # type: ignore[return-value]

    def pending(self) -> tuple[Dispatch, ...]:
        """Return undelivered questions with transient configured advisor IDs."""

        if not self.settings.enabled:
            return ()
        now = self._now()
        advisors = {
            self._advisor_key(advisor_id): advisor_id
            for advisor_id in self.settings.advisor_ids
        }

        def operation(payload: dict[str, object]) -> tuple[Dispatch, ...]:
            pending: dict[str, dict[str, object]] = payload["pending"]  # type: ignore[assignment]
            dispatches = []
            for record in sorted(
                pending.values(),
                key=lambda item: (item["created_at"], item["question_id"]),
            ):
                if (
                    record["delivered_at"] is not None
                    or parse_timestamp(record["expires_at"]) <= now
                ):
                    continue
                advisor_id = advisors.get(str(record["advisor_key"]))
                if advisor_id is None:
                    continue
                question = self._question_from_record(record)
                dispatches.append(
                    Dispatch(
                        question=question,
                        advisor_id=advisor_id,
                        prompt=QUESTION_TEMPLATES[question.topic],
                    )
                )
            return tuple(dispatches)

        return self._read(now, operation)  # type: ignore[return-value]

    def mark_delivery(self, question_id: str, advisor_id: int) -> Receipt:
        """Mark a successful application-level delivery to the selected advisor."""

        question = self._normalized_question_id(question_id)
        selected_advisor = self._normalized_advisor_id(advisor_id)
        if not self.settings.enabled:
            return Receipt(
                ReceiptStatus.DISABLED,
                question,
                self._current_revision(),
            )
        selected_advisor = self._configured_advisor(selected_advisor)
        now = self._now()

        def operation(
            payload: dict[str, object], operation_now: datetime
        ) -> Receipt:
            self._prune(payload, operation_now)
            revision = int(payload["revision"])
            pending: dict[str, dict[str, object]] = payload["pending"]  # type: ignore[assignment]
            record = pending.get(question)
            if record is None:
                status = self._closed_status(payload, question)
                return Receipt(status, question, revision)
            if record["advisor_key"] != self._advisor_key(selected_advisor):
                return Receipt(ReceiptStatus.WRONG_ADVISOR, question, revision)
            if parse_timestamp(record["expires_at"]) <= operation_now:
                return Receipt(ReceiptStatus.EXPIRED, question, revision)
            if record["delivered_at"] is not None:
                return Receipt(
                    ReceiptStatus.ALREADY_DELIVERED,
                    question,
                    revision,
                )
            record["delivered_at"] = timestamp_text(operation_now)
            payload["revision"] = revision + 1
            return Receipt(ReceiptStatus.DELIVERED, question, revision + 1)

        return self._update(now, operation)  # type: ignore[return-value]

    def review(self, review: StructuredReview) -> Receipt:
        """Apply one fixed, structured review after verified delivery."""

        if not isinstance(review, StructuredReview):
            raise ValueError("review must be a StructuredReview")
        if not self.settings.enabled:
            return Receipt(
                ReceiptStatus.DISABLED,
                review.question_id,
                self._current_revision(),
            )
        advisor_id = self._configured_advisor(review.advisor_id)
        now = self._now()

        def operation(
            payload: dict[str, object], operation_now: datetime
        ) -> Receipt:
            self._prune(payload, operation_now)
            revision = int(payload["revision"])
            pending: dict[str, dict[str, object]] = payload["pending"]  # type: ignore[assignment]
            record = pending.get(review.question_id)
            if record is None:
                status = self._closed_status(payload, review.question_id)
                return Receipt(status, review.question_id, revision)
            if record["advisor_key"] != self._advisor_key(advisor_id):
                return Receipt(
                    ReceiptStatus.WRONG_ADVISOR,
                    review.question_id,
                    revision,
                )
            if record["delivered_at"] is None:
                return Receipt(
                    ReceiptStatus.NOT_DELIVERED,
                    review.question_id,
                    revision,
                )
            if parse_timestamp(record["expires_at"]) <= operation_now:
                return Receipt(
                    ReceiptStatus.EXPIRED,
                    review.question_id,
                    revision,
                )
            topic = DiscoveryTopic(record["topic"])
            expected_selection = SELECTION_ENUM_BY_TOPIC[topic]
            if not isinstance(review.selection, expected_selection):
                raise ValueError(
                    f"review selection does not match topic {topic.value!r}"
                )
            model_updated = review.disposition is not ReviewDisposition.ABSTAIN
            if model_updated:
                self._update_model(payload, record, review, operation_now)
            closed = dict(record)
            closed.update(
                {
                    "closed_at": timestamp_text(operation_now),
                    "status": "reviewed" if model_updated else "abstained",
                }
            )
            history: list[dict[str, object]] = payload["history"]  # type: ignore[assignment]
            history.append(closed)
            self._trim_history(history)
            pending.pop(review.question_id)
            payload["revision"] = revision + 1
            return Receipt(
                ReceiptStatus.ACCEPTED if model_updated else ReceiptStatus.ABSTAINED,
                review.question_id,
                revision + 1,
                model_updated=model_updated,
            )

        return self._update(now, operation)  # type: ignore[return-value]

    def _update_model(
        self,
        payload: dict[str, object],
        question: Mapping[str, object],
        review: StructuredReview,
        now: datetime,
    ) -> None:
        topic = DiscoveryTopic(question["topic"])
        bucket_name = MODEL_BUCKET_BY_TOPIC[topic]
        models: dict[str, dict[str, dict[str, dict[str, object]]]] = payload[  # type: ignore[assignment]
            "models"
        ]
        scope_key = str(question["scope_key"])
        if scope_key not in models and len(models) >= _MAX_MODEL_SCOPES:
            raise StateStoreError("discovery model scope capacity is exhausted")
        scoped = models.setdefault(scope_key, {})
        bucket = scoped.setdefault(bucket_name, {})
        selection = review.selection.value
        aggregate = bucket.get(selection)
        if aggregate is None:
            aggregate = {
                "review_count": 0,
                "support": 0.0,
                "challenge": 0.0,
                "uncertainty": 0.0,
                "positive_impact": 0.0,
                "negative_impact": 0.0,
                "updated_at": timestamp_text(now),
            }
            bucket[selection] = aggregate
        disposition = {
            ReviewDisposition.SUPPORT: (1.0, 0.0),
            ReviewDisposition.CHALLENGE: (0.0, 1.0),
            ReviewDisposition.MIXED: (0.65, 0.65),
            ReviewDisposition.ABSTAIN: (0.0, 0.0),
        }[review.disposition]
        impact = {
            ReviewImpact.POSITIVE: (1.0, 0.0),
            ReviewImpact.NEGATIVE: (0.0, 1.0),
            ReviewImpact.MIXED: (0.65, 0.65),
            ReviewImpact.NEUTRAL: (0.0, 0.0),
        }[review.impact]
        incorporation = 0.25
        aggregate["support"] = self._incorporate(
            float(aggregate["support"]), disposition[0], incorporation
        )
        aggregate["challenge"] = self._incorporate(
            float(aggregate["challenge"]), disposition[1], incorporation
        )
        aggregate["positive_impact"] = self._incorporate(
            float(aggregate["positive_impact"]), impact[0], incorporation
        )
        aggregate["negative_impact"] = self._incorporate(
            float(aggregate["negative_impact"]), impact[1], incorporation
        )
        # Independent support and challenge are deliberately retained.  A later
        # disagreement raises rather than erases model uncertainty.
        aggregate["uncertainty"] = clamp(
            math.sqrt(float(aggregate["support"]) * float(aggregate["challenge"]))
        )
        aggregate["review_count"] = min(
            _MAX_MODEL_REVIEWS, int(aggregate["review_count"]) + 1
        )
        aggregate["updated_at"] = timestamp_text(now)
        # Person-derived aggregate buckets intentionally contain no subject or
        # event key.  Only their fixed, de-identified exemplar kind remains.

    @staticmethod
    def _incorporate(current: float, signal: float, gain: float) -> float:
        return clamp(current + gain * signal * (1.0 - current))

    def snapshot(self, *, scope_ref: str | None = None) -> dict[str, object]:
        """Return aggregate models and queue counts without pseudonymous keys."""

        now = self._now()
        selected_scope = (
            self.identities.scope_key(scope_ref) if scope_ref is not None else None
        )

        def operation(payload: dict[str, object]) -> dict[str, object]:
            pending: dict[str, dict[str, object]] = payload["pending"]  # type: ignore[assignment]
            history: list[dict[str, object]] = payload["history"]  # type: ignore[assignment]
            models: dict[str, dict[str, dict[str, dict[str, object]]]] = payload[  # type: ignore[assignment]
                "models"
            ]
            scopes = [selected_scope] if selected_scope is not None else sorted(models)
            combined = self._combined_models(
                models[scope]
                for scope in scopes
                if scope is not None and scope in models
            )
            active_pending = [
                record
                for record in pending.values()
                if parse_timestamp(record["expires_at"]) > now
                and (
                    selected_scope is None
                    or record["scope_key"] == selected_scope
                )
            ]
            history_cutoff = now - self._history_retention()
            history_records = [
                record
                for record in history
                if parse_timestamp(record["created_at"]) >= history_cutoff
                and (
                    selected_scope is None
                    or record["scope_key"] == selected_scope
                )
            ]
            expired_pending_count = sum(
                parse_timestamp(record["expires_at"]) <= now
                and parse_timestamp(record["created_at"]) >= history_cutoff
                and (
                    selected_scope is None
                    or record["scope_key"] == selected_scope
                )
                for record in pending.values()
            )
            return {
                "schema_version": "organism.discovery.snapshot.v1",
                "enabled": self.settings.enabled,
                "revision": int(payload["revision"]),
                "pending_delivery_count": (
                    sum(record["delivered_at"] is None for record in active_pending)
                    if self.settings.enabled
                    else 0
                ),
                "awaiting_review_count": (
                    sum(
                        record["delivered_at"] is not None
                        for record in active_pending
                    )
                    if self.settings.enabled
                    else 0
                ),
                "recent_closed_count": len(history_records)
                + expired_pending_count,
                "models": combined,
            }

        return self._read(now, operation)  # type: ignore[return-value]

    def _combined_models(
        self,
        scoped_models: Iterable[Mapping[str, Mapping[str, Mapping[str, object]]]],
    ) -> dict[str, dict[str, dict[str, object]]]:
        accumulators: dict[str, dict[str, dict[str, float | int | str]]] = {}
        for scoped in scoped_models:
            for bucket_name, bucket in scoped.items():
                target_bucket = accumulators.setdefault(bucket_name, {})
                for selection, aggregate in bucket.items():
                    target = target_bucket.setdefault(
                        selection,
                        {
                            "review_count": 0,
                            "support_total": 0.0,
                            "challenge_total": 0.0,
                            "positive_total": 0.0,
                            "negative_total": 0.0,
                            "updated_at": str(aggregate["updated_at"]),
                        },
                    )
                    count = int(aggregate["review_count"])
                    target["review_count"] = int(target["review_count"]) + count
                    for source, destination in (
                        ("support", "support_total"),
                        ("challenge", "challenge_total"),
                        ("positive_impact", "positive_total"),
                        ("negative_impact", "negative_total"),
                    ):
                        target[destination] = (
                            float(target[destination])
                            + float(aggregate[source]) * count
                        )
                    if str(aggregate["updated_at"]) > str(target["updated_at"]):
                        target["updated_at"] = str(aggregate["updated_at"])
        result: dict[str, dict[str, dict[str, object]]] = {}
        for bucket_name, bucket in accumulators.items():
            result_bucket = result.setdefault(bucket_name, {})
            for selection, aggregate in bucket.items():
                count = max(1, int(aggregate["review_count"]))
                support = float(aggregate["support_total"]) / count
                challenge = float(aggregate["challenge_total"]) / count
                result_bucket[selection] = {
                    "review_count": int(aggregate["review_count"]),
                    "support": round(support, 6),
                    "challenge": round(challenge, 6),
                    "uncertainty": round(math.sqrt(support * challenge), 6),
                    "positive_impact": round(
                        float(aggregate["positive_total"]) / count, 6
                    ),
                    "negative_impact": round(
                        float(aggregate["negative_total"]) / count, 6
                    ),
                    "updated_at": aggregate["updated_at"],
                }
        return result

    def forget_subject(self, *, scope_ref: str, actor_ref: str) -> dict[str, int]:
        """Remove retained question activity for one scoped pseudonymous subject."""

        subject_key = self.identities.subject_key(scope_ref, actor_ref)
        now = self._now()

        def operation(
            payload: dict[str, object], operation_now: datetime
        ) -> dict[str, int]:
            self._prune(payload, operation_now)
            pending: dict[str, dict[str, object]] = payload["pending"]  # type: ignore[assignment]
            history: list[dict[str, object]] = payload["history"]  # type: ignore[assignment]
            pending_ids = [
                question_id
                for question_id, record in pending.items()
                if record.get("subject_key") == subject_key
            ]
            for question_id in pending_ids:
                pending.pop(question_id)
            history_before = len(history)
            history[:] = [
                record for record in history if record.get("subject_key") != subject_key
            ]
            if pending_ids or history_before != len(history):
                payload["revision"] = int(payload["revision"]) + 1
            return {
                "pending_removed": len(pending_ids),
                "history_removed": history_before - len(history),
            }

        return self._update(now, operation)  # type: ignore[return-value]

    def purge_scope(self, *, scope_ref: str) -> dict[str, int]:
        """Erase one scope's queue, rate history, and de-identified models."""

        scope_key = self.identities.scope_key(scope_ref)
        now = self._now()

        def operation(
            payload: dict[str, object], operation_now: datetime
        ) -> dict[str, int]:
            self._prune(payload, operation_now)
            pending: dict[str, dict[str, object]] = payload["pending"]  # type: ignore[assignment]
            history: list[dict[str, object]] = payload["history"]  # type: ignore[assignment]
            models: dict[str, object] = payload["models"]  # type: ignore[assignment]
            pending_ids = [
                question_id
                for question_id, record in pending.items()
                if record["scope_key"] == scope_key
            ]
            for question_id in pending_ids:
                pending.pop(question_id)
            history_before = len(history)
            history[:] = [
                record for record in history if record["scope_key"] != scope_key
            ]
            model_removed = int(models.pop(scope_key, None) is not None)
            if pending_ids or history_before != len(history) or model_removed:
                payload["revision"] = int(payload["revision"]) + 1
            return {
                "pending_removed": len(pending_ids),
                "history_removed": history_before - len(history),
                "scoped_models_removed": model_removed,
            }

        return self._update(now, operation)  # type: ignore[return-value]

    def purge_all(self) -> dict[str, int]:
        """Erase every question, rate record, and reviewed aggregate model."""

        now = self._now()

        def operation(
            payload: dict[str, object], operation_now: datetime
        ) -> dict[str, int]:
            removed = {
                "pending_removed": len(payload["pending"]),  # type: ignore[arg-type]
                "history_removed": len(payload["history"]),  # type: ignore[arg-type]
                "scoped_models_removed": len(payload["models"]),  # type: ignore[arg-type]
            }
            fresh = self._fresh(operation_now)
            payload.clear()
            payload.update(fresh)
            return removed

        try:
            return self._update(now, operation)  # type: ignore[return-value]
        except StateStoreError:
            existed = self.store.replace(lambda: self._fresh(now))
            return {
                "pending_removed": 0,
                "history_removed": 0,
                "scoped_models_removed": 0,
                "unreadable_state_replaced": int(existed),
            }

    @staticmethod
    def _normalized_question_id(question_id: str) -> str:
        if not _is_digest(question_id):
            raise ValueError("question_id must be a 64-character lowercase digest")
        return question_id

    @staticmethod
    def _normalized_advisor_id(advisor_id: int) -> int:
        if (
            type(advisor_id) is not int
            or not 1 <= advisor_id <= (1 << 64) - 1
        ):
            raise PermissionError("advisor is not explicitly configured for discovery")
        return advisor_id

    def _configured_advisor(self, advisor_id: int) -> int:
        selected = self._normalized_advisor_id(advisor_id)
        if selected not in self.settings.advisor_ids:
            raise PermissionError("advisor is not explicitly configured for discovery")
        return selected

    def _current_revision(self) -> int:
        now = self._now()
        return self._read(
            now,
            lambda payload: int(payload["revision"]),
        )  # type: ignore[return-value]

    @staticmethod
    def _closed_status(
        payload: Mapping[str, object], question_id: str
    ) -> ReceiptStatus:
        history = payload.get("history", [])
        record = next(
            (
                item
                for item in history  # type: ignore[union-attr]
                if item["question_id"] == question_id
            ),
            None,
        )
        if record is None:
            return ReceiptStatus.UNKNOWN_QUESTION
        if record["status"] in {"reviewed", "abstained"}:
            return ReceiptStatus.ALREADY_REVIEWED
        return ReceiptStatus.EXPIRED


__all__ = [
    "DISCOVERY_SCHEMA",
    "DiscoveryEngine",
    "MODEL_BUCKET_BY_TOPIC",
    "PERSON_SCORE_THRESHOLD",
    "PERSON_TOPICS",
]
