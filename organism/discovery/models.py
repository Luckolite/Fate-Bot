"""Strict contracts for bounded, nonliteral discovery and advisory review.

The types in this module describe software inquiry, not subjective curiosity,
personality, moral worth, or consciousness.  Raw references are permitted only
on :class:`CuriosityStimulus`; the discovery engine pseudonymizes them before
anything is written to disk.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Mapping, TypeAlias, cast

from ..models import bounded_text, ensure_utc, unit


class DiscoveryTopic(str, Enum):
    FUNCTIONAL_SELF = "functional_self"
    MORAL_TRADEOFF = "moral_tradeoff"
    CONCEPT_RIDDLE = "concept_riddle"
    BEHAVIOR_IMPACT = "behavior_impact"
    PERSPECTIVE_DISCOVERY = "perspective_discovery"


class FunctionalCommitment(str, Enum):
    CLARIFY_UNCERTAINTY = "clarify_uncertainty"
    PRESERVE_AGENCY = "preserve_agency"
    RESPECT_BOUNDARIES = "respect_boundaries"
    PREFER_REVERSIBLE_ACTIONS = "prefer_reversible_actions"
    REPAIR_HARM = "repair_harm"
    SEEK_UNDERSTANDING = "seek_understanding"


class MoralPrinciple(str, Enum):
    CARE = "care"
    FAIRNESS = "fairness"
    AUTONOMY = "autonomy"
    HONESTY = "honesty"
    NON_HARM = "non_harm"
    ACCOUNTABILITY = "accountability"
    REPAIR = "repair"


class RiddleKind(str, Enum):
    AMBIGUITY = "ambiguity"
    CONTRADICTION = "contradiction"
    CAUSAL_GAP = "causal_gap"
    VALUE_TENSION = "value_tension"
    PERSPECTIVE_GAP = "perspective_gap"
    PATTERN = "pattern"
    EXPRESSION = "expression"


class BehaviorExemplar(str, Enum):
    """Reviewed episode-level patterns, never labels for a person."""

    BOUNDARY_RESPECT = "boundary_respect"
    SAFE_FOLLOWTHROUGH = "safe_followthrough"
    REPAIR = "repair"
    COOPERATION = "cooperation"
    ACCOUNTABILITY = "accountability"
    CONSTRUCTIVE_CHALLENGE = "constructive_challenge"
    CAREFUL_VERIFICATION = "careful_verification"
    BOUNDARY_PRESSURE = "boundary_pressure"
    ADVERSE_FOLLOWTHROUGH = "adverse_followthrough"
    REPAIR_NOT_COMPLETED = "repair_not_completed"
    INTERACTION_ESCALATION = "interaction_escalation"
    ACCOUNTABILITY_NOT_DEMONSTRATED = "accountability_not_demonstrated"


class PerspectiveKind(str, Enum):
    DIFFERENT_ASSUMPTION = "different_assumption"
    ALTERNATIVE_GOAL = "alternative_goal"
    UNFAMILIAR_EXPERIENCE = "unfamiliar_experience"
    CONTRASTING_INTERPRETATION = "contrasting_interpretation"
    NEW_CONSTRAINT = "new_constraint"
    CREATIVE_FRAME = "creative_frame"


ReviewedSelection: TypeAlias = (
    FunctionalCommitment
    | MoralPrinciple
    | RiddleKind
    | BehaviorExemplar
    | PerspectiveKind
)

MAX_ADVISOR_IDS = 64


SELECTION_ENUM_BY_TOPIC: Mapping[DiscoveryTopic, type[Enum]] = {
    DiscoveryTopic.FUNCTIONAL_SELF: FunctionalCommitment,
    DiscoveryTopic.MORAL_TRADEOFF: MoralPrinciple,
    DiscoveryTopic.CONCEPT_RIDDLE: RiddleKind,
    DiscoveryTopic.BEHAVIOR_IMPACT: BehaviorExemplar,
    DiscoveryTopic.PERSPECTIVE_DISCOVERY: PerspectiveKind,
}


class ReviewDisposition(str, Enum):
    SUPPORT = "support"
    CHALLENGE = "challenge"
    MIXED = "mixed"
    ABSTAIN = "abstain"


class ReviewImpact(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    MIXED = "mixed"
    NEUTRAL = "neutral"


class InquiryStatus(str, Enum):
    DISABLED = "disabled"
    TOPIC_DISABLED = "topic_disabled"
    GATE_REJECTED = "gate_rejected"
    BELOW_THRESHOLD = "below_threshold"
    TOPIC_EVIDENCE_REQUIRED = "topic_evidence_required"
    PERSON_EVIDENCE_REQUIRED = "person_evidence_required"
    DEDUPLICATED = "deduplicated"
    RATE_LIMITED = "rate_limited"
    PENDING_CAPACITY = "pending_capacity"
    QUEUED = "queued"


class ReceiptStatus(str, Enum):
    DISABLED = "disabled"
    DELIVERED = "delivered"
    ALREADY_DELIVERED = "already_delivered"
    ACCEPTED = "accepted"
    ABSTAINED = "abstained"
    ALREADY_REVIEWED = "already_reviewed"
    NOT_DELIVERED = "not_delivered"
    UNKNOWN_QUESTION = "unknown_question"
    WRONG_ADVISOR = "wrong_advisor"
    EXPIRED = "expired"


QUESTION_TEMPLATES: Mapping[DiscoveryTopic, str] = {
    DiscoveryTopic.FUNCTIONAL_SELF: (
        "Which functional commitment should guide Fate when its current signals "
        "leave its response direction uncertain?"
    ),
    DiscoveryTopic.MORAL_TRADEOFF: (
        "Which moral principle should Fate emphasize when this kind of tradeoff "
        "appears again?"
    ),
    DiscoveryTopic.CONCEPT_RIDDLE: (
        "Which kind of riddle best describes this recurring information gap?"
    ),
    DiscoveryTopic.BEHAVIOR_IMPACT: (
        "Which de-identified behavior pattern, if any, is worth retaining from "
        "this reviewed interaction?"
    ),
    DiscoveryTopic.PERSPECTIVE_DISCOVERY: (
        "Which kind of perspective difference, if any, is worth retaining from "
        "this reviewed interaction?"
    ),
}


def _enum(value: object, enum_type: type[Enum], *, name: str) -> Enum:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} is not supported") from error


def _finite_number(
    value: object,
    *,
    name: str,
    minimum: float,
    maximum: float,
) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return result


def _integer(
    value: object,
    *,
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _advisor_id(value: object) -> int:
    return _integer(
        value,
        name="discovery.advisor_ids entry",
        minimum=1,
        maximum=(1 << 64) - 1,
    )


def _digest(value: str, *, name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{name} must be a 64-character lowercase digest")
    return value


@dataclass(frozen=True)
class DiscoverySettings:
    """Explicitly configured inquiry policy; disabled settings contact nobody."""

    enabled: bool = False
    advisor_ids: tuple[int, ...] = ()
    allowed_topics: tuple[DiscoveryTopic, ...] = tuple(DiscoveryTopic)
    score_threshold: float = 0.65
    reviewer_cooldown_hours: float = 8.0
    max_per_reviewer_24h: int = 3
    max_per_scope_24h: int = 12
    same_subject_topic_days: float = 7.0
    question_ttl_hours: float = 24.0
    max_pending: int = 64

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("discovery.enabled must be true or false")
        advisors = tuple(_advisor_id(value) for value in self.advisor_ids)
        if len(advisors) > MAX_ADVISOR_IDS:
            raise ValueError(
                f"discovery.advisor_ids cannot contain more than {MAX_ADVISOR_IDS} users"
            )
        if len(advisors) != len(set(advisors)):
            raise ValueError("discovery.advisor_ids cannot contain duplicates")
        topics = tuple(
            _enum(value, DiscoveryTopic, name="discovery.allowed_topics")
            for value in self.allowed_topics
        )
        if len(topics) != len(set(topics)):
            raise ValueError("discovery.allowed_topics cannot contain duplicates")
        object.__setattr__(self, "advisor_ids", advisors)
        object.__setattr__(self, "allowed_topics", topics)
        object.__setattr__(
            self,
            "score_threshold",
            _finite_number(
                self.score_threshold,
                name="discovery.score_threshold",
                minimum=0.65,
                maximum=0.95,
            ),
        )
        for name, minimum, maximum in (
            ("reviewer_cooldown_hours", 8.0, 168.0),
            ("same_subject_topic_days", 7.0, 90.0),
            ("question_ttl_hours", 1.0, 24.0),
        ):
            object.__setattr__(
                self,
                name,
                _finite_number(
                    getattr(self, name),
                    name=f"discovery.{name}",
                    minimum=minimum,
                    maximum=maximum,
                ),
            )
        for name, minimum, maximum in (
            ("max_per_reviewer_24h", 1, 3),
            ("max_per_scope_24h", 1, 12),
            ("max_pending", 1, 64),
        ):
            object.__setattr__(
                self,
                name,
                _integer(
                    getattr(self, name),
                    name=f"discovery.{name}",
                    minimum=minimum,
                    maximum=maximum,
                ),
            )
        if self.max_per_reviewer_24h > self.max_per_scope_24h:
            raise ValueError(
                "discovery.max_per_reviewer_24h cannot exceed max_per_scope_24h"
            )
        if self.enabled and not advisors:
            raise ValueError(
                "enabled discovery requires explicit advisor_ids; bot owners are "
                "never an implicit fallback"
            )
        if self.enabled and not topics:
            raise ValueError("enabled discovery requires at least one allowed topic")

    @classmethod
    def from_mapping(
        cls, values: Mapping[str, object] | None = None
    ) -> "DiscoverySettings":
        if values is None:
            return cls()
        if not isinstance(values, Mapping):
            raise ValueError("discovery settings must be an object")
        allowed = {
            "enabled",
            "advisor_ids",
            "allowed_topics",
            "score_threshold",
            "reviewer_cooldown_hours",
            "max_per_reviewer_24h",
            "max_per_scope_24h",
            "same_subject_topic_days",
            "question_ttl_hours",
            "max_pending",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(
                "unknown discovery settings: " + ", ".join(sorted(unknown))
            )
        data = dict(values)
        for name in ("advisor_ids", "allowed_topics"):
            if name in data:
                value = data[name]
                if not isinstance(value, (list, tuple)):
                    raise ValueError(f"discovery.{name} must be an array")
                data[name] = tuple(value)
        return cls(**data)


@dataclass(frozen=True)
class CuriosityStimulus:
    """One trusted, transient discovery observation.

    References may identify application objects while this value is in memory.
    They are never persisted directly.  The remaining fields are closed enums,
    booleans, or bounded numeric signals; no message, name, answer, or profile
    text can be carried by this contract.
    """

    topic: DiscoveryTopic
    scope_ref: str
    event_ref: str
    actor_ref: str | None = None
    authenticated_episode: bool = False
    functional_tension: float = 0.0
    moral_tension: float = 0.0
    concept_novelty: float = 0.0
    behavior_signal: float = 0.0
    impact_signal: float = 0.0
    perspective_signal: float = 0.0
    profile_novelty: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "topic", _enum(self.topic, DiscoveryTopic, name="stimulus.topic")
        )
        object.__setattr__(
            self,
            "scope_ref",
            bounded_text(self.scope_ref, name="stimulus.scope_ref", maximum=256),
        )
        object.__setattr__(
            self,
            "event_ref",
            bounded_text(self.event_ref, name="stimulus.event_ref", maximum=256),
        )
        if self.actor_ref is not None:
            object.__setattr__(
                self,
                "actor_ref",
                bounded_text(
                    self.actor_ref,
                    name="stimulus.actor_ref",
                    maximum=512,
                ),
            )
        if type(self.authenticated_episode) is not bool:
            raise ValueError("stimulus.authenticated_episode must be a boolean")
        for name in (
            "functional_tension",
            "moral_tension",
            "concept_novelty",
            "behavior_signal",
            "impact_signal",
            "perspective_signal",
            "profile_novelty",
        ):
            object.__setattr__(
                self,
                name,
                unit(getattr(self, name), name=f"stimulus.{name}"),
            )


@dataclass(frozen=True)
class Question:
    question_id: str
    topic: DiscoveryTopic
    score: float
    created_at: datetime
    expires_at: datetime
    subject_present: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "question_id", _digest(self.question_id, name="question_id")
        )
        object.__setattr__(
            self, "topic", _enum(self.topic, DiscoveryTopic, name="question.topic")
        )
        object.__setattr__(self, "score", unit(self.score, name="question.score"))
        created = ensure_utc(self.created_at, name="question.created_at")
        expires = ensure_utc(self.expires_at, name="question.expires_at")
        if expires <= created:
            raise ValueError("question.expires_at must be after created_at")
        if type(self.subject_present) is not bool:
            raise ValueError("question.subject_present must be a boolean")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "expires_at", expires)


@dataclass(frozen=True)
class InquirySignal:
    topic: DiscoveryTopic
    score: float
    status: InquiryStatus
    question: Question | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "topic", _enum(self.topic, DiscoveryTopic, name="inquiry.topic")
        )
        object.__setattr__(self, "score", unit(self.score, name="inquiry.score"))
        object.__setattr__(
            self,
            "status",
            _enum(self.status, InquiryStatus, name="inquiry.status"),
        )
        if self.question is not None and not isinstance(self.question, Question):
            raise ValueError("inquiry.question must be Question or None")
        if self.status is InquiryStatus.QUEUED and self.question is None:
            raise ValueError("queued inquiry requires a question")
        if self.status is not InquiryStatus.QUEUED and self.question is not None:
            raise ValueError("rejected inquiry cannot contain a question")


@dataclass(frozen=True)
class Dispatch:
    question: Question
    advisor_id: int
    prompt: str

    def __post_init__(self) -> None:
        if not isinstance(self.question, Question):
            raise ValueError("dispatch.question must be a Question")
        object.__setattr__(self, "advisor_id", _advisor_id(self.advisor_id))
        expected = QUESTION_TEMPLATES[self.question.topic]
        if self.prompt != expected:
            raise ValueError("dispatch.prompt must match the fixed topic template")

    @property
    def selection_options(self) -> tuple[str, ...]:
        """Return the exact closed selections accepted for this question."""

        enum_type = SELECTION_ENUM_BY_TOPIC[self.question.topic]
        return tuple(option.value for option in enum_type)

    @property
    def disposition_options(self) -> tuple[str, ...]:
        """Return the exact closed review dispositions."""

        return tuple(option.value for option in ReviewDisposition)

    @property
    def impact_options(self) -> tuple[str, ...]:
        """Return the exact closed impact labels."""

        return tuple(option.value for option in ReviewImpact)

    def make_review(
        self,
        *,
        selection: ReviewedSelection | str,
        disposition: ReviewDisposition | str,
        impact: ReviewImpact | str = ReviewImpact.NEUTRAL,
    ) -> StructuredReview:
        """Build a review bound to this question, advisor, and topic options."""

        enum_type = SELECTION_ENUM_BY_TOPIC[self.question.topic]
        error_message = (
            "selection must be one of dispatch.selection_options for "
            f"topic {self.question.topic.value!r}"
        )
        if isinstance(selection, enum_type):
            selected = selection
        elif type(selection) is str:
            try:
                selected = enum_type(selection)
            except (TypeError, ValueError) as error:
                raise ValueError(error_message) from error
        else:
            raise ValueError(error_message)
        return StructuredReview(
            question_id=self.question.question_id,
            advisor_id=self.advisor_id,
            selection=cast(ReviewedSelection, selected),
            disposition=cast(ReviewDisposition, disposition),
            impact=cast(ReviewImpact, impact),
        )


@dataclass(frozen=True)
class StructuredReview:
    question_id: str
    advisor_id: int
    selection: ReviewedSelection
    disposition: ReviewDisposition
    impact: ReviewImpact = ReviewImpact.NEUTRAL

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "question_id", _digest(self.question_id, name="question_id")
        )
        object.__setattr__(self, "advisor_id", _advisor_id(self.advisor_id))
        if not isinstance(
            self.selection,
            (
                FunctionalCommitment,
                MoralPrinciple,
                RiddleKind,
                BehaviorExemplar,
                PerspectiveKind,
            ),
        ):
            raise ValueError("review.selection must be a supported selection enum")
        object.__setattr__(
            self,
            "disposition",
            _enum(
                self.disposition,
                ReviewDisposition,
                name="review.disposition",
            ),
        )
        object.__setattr__(
            self,
            "impact",
            _enum(self.impact, ReviewImpact, name="review.impact"),
        )


@dataclass(frozen=True)
class Receipt:
    status: ReceiptStatus
    question_id: str
    revision: int
    model_updated: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            _enum(self.status, ReceiptStatus, name="receipt.status"),
        )
        object.__setattr__(
            self, "question_id", _digest(self.question_id, name="question_id")
        )
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("receipt.revision must be a non-negative integer")
        if type(self.model_updated) is not bool:
            raise ValueError("receipt.model_updated must be a boolean")


__all__ = [
    "BehaviorExemplar",
    "CuriosityStimulus",
    "DiscoverySettings",
    "DiscoveryTopic",
    "Dispatch",
    "FunctionalCommitment",
    "InquirySignal",
    "InquiryStatus",
    "MoralPrinciple",
    "MAX_ADVISOR_IDS",
    "PerspectiveKind",
    "QUESTION_TEMPLATES",
    "Question",
    "Receipt",
    "ReceiptStatus",
    "ReviewDisposition",
    "ReviewImpact",
    "ReviewedSelection",
    "RiddleKind",
    "SELECTION_ENUM_BY_TOPIC",
    "StructuredReview",
]
