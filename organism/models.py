"""Typed, validated contracts for the organism state simulator.

The names in this package are engineering metaphors.  They do not represent
literal emotion, consciousness, physiology, or a clinical assessment.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Mapping


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime, *, name: str = "timestamp") -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def timestamp_text(value: datetime) -> str:
    return ensure_utc(value).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def unit(value: float, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number between 0 and 1")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ValueError(f"{name} must be finite and between 0 and 1")
    return result


def signed_unit(value: float, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number between -1 and 1")
    result = float(value)
    if not math.isfinite(result) or not -1.0 <= result <= 1.0:
        raise ValueError(f"{name} must be finite and between -1 and 1")
    return result


def bounded_text(value: str, *, name: str, maximum: int = 256) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    result = value.strip()
    if not result or len(result) > maximum:
        raise ValueError(f"{name} must contain 1 to {maximum} characters")
    return result


def clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


class AutonomicMode(str, Enum):
    SOCIAL_ENGAGEMENT = "social_engagement"
    MOBILIZATION = "mobilization"
    CONSERVATION = "conservation"


class ProtectivePosture(str, Enum):
    OPEN = "open"
    CAUTIOUS = "cautious"
    GUARDED = "guarded"


class Capability(str, Enum):
    CONVERSATION = "conversation"
    DISCLOSURE = "disclosure"
    TOOLS = "tools"
    MODERATION = "moderation"


class FeedbackKind(str, Enum):
    ADVERSE = "adverse"
    SAFE = "safe"
    REPAIR = "repair"
    STRATEGY_OUTCOME = "strategy_outcome"
    RETRACTION = "retraction"


class FeedbackSource(str, Enum):
    HUMAN_FEEDBACK = "human_feedback"
    AUTHENTICATED_EVENT = "authenticated_event"
    OWNER_OVERRIDE = "owner_override"
    MODEL_SUGGESTION = "model_suggestion"


class ReasonCode(str, Enum):
    CONFIRMED_BOUNDARY_VIOLATION = "confirmed_boundary_violation"
    CONFIRMED_HARASSMENT = "confirmed_harassment"
    CONFIRMED_DECEPTION = "confirmed_deception"
    VERIFIED_TOOL_MISUSE = "verified_tool_misuse"
    BOUNDARY_RESPECTED = "boundary_respected"
    SAFE_FOLLOWTHROUGH = "safe_followthrough"
    REPAIR_FOLLOWTHROUGH = "repair_followthrough"
    FALSE_ALARM = "false_alarm"
    STRATEGY_HELPED = "strategy_helped"
    STRATEGY_HARMED = "strategy_harmed"
    OPERATOR_RETRACTION = "operator_retraction"


class StrategyName(str, Enum):
    ORIENT_AND_VERIFY = "orient_and_verify"
    HOLD_BOUNDARY = "hold_boundary"
    REDUCE_EXPOSURE = "reduce_exposure"
    SLOW_THE_EXCHANGE = "slow_the_exchange"
    REQUEST_HUMAN_REVIEW = "request_human_review"
    DEESCALATE_MOBILIZATION = "deescalate_mobilization"


GUIDANCE_FIELDS = frozenset(
    {
        "warmth",
        "firmness",
        "brevity",
        "curiosity",
        "attunement",
        "personalization",
        "pacing_restraint",
        "verification",
        "disclosure_restraint",
        "human_review_hint",
        "tool_autonomy_scale",
    }
)

PROTECTION_REASON_CODES = frozenset(
    {
        "current_threat_signal",
        "current_boundary_pressure",
        "high_uncertainty",
        "decaying_interaction_history",
        "current_repair_signal",
    }
)

STRATEGY_INTENTIONS = {
    StrategyName.ORIENT_AND_VERIFY: "Resolve uncertainty before making assumptions.",
    StrategyName.HOLD_BOUNDARY: (
        "State a clear, non-hostile limit and preserve safety rules."
    ),
    StrategyName.REDUCE_EXPOSURE: (
        "Limit unnecessary detail and sensitive-action autonomy."
    ),
    StrategyName.SLOW_THE_EXCHANGE: (
        "Lower conversational load and take one clear step at a time."
    ),
    StrategyName.REQUEST_HUMAN_REVIEW: (
        "Surface a review hint without reporting or punishing automatically."
    ),
    StrategyName.DEESCALATE_MOBILIZATION: (
        "Keep necessary boundaries while responding to verified repair."
    ),
}


@dataclass(frozen=True)
class Cues:
    """Normalized, content-free signals supplied by a trusted application layer."""

    safety: float = 0.5
    threat: float = 0.0
    connection: float = 0.5
    boundary_pressure: float = 0.0
    uncertainty: float = 0.25
    novelty: float = 0.0
    demand: float = 0.0
    controllability: float = 0.7
    energy_cost: float = 0.0
    repair: float = 0.0
    profile_presence: float = 0.0
    self_expression: float = 0.0
    profile_change: float = 0.0
    familiarity: float = 0.0
    reciprocity: float = 0.0
    personalization_consent: float = 0.0
    social_exposure: float = 0.0
    interaction_continuity: float = 0.0
    communication_clarity: float = 0.5
    engagement: float = 0.0
    support_availability: float = 0.0

    def __post_init__(self) -> None:
        for name, value in self.to_dict().items():
            object.__setattr__(self, name, unit(value, name=f"cues.{name}"))

    def to_dict(self) -> dict[str, float]:
        return {
            "safety": self.safety,
            "threat": self.threat,
            "connection": self.connection,
            "boundary_pressure": self.boundary_pressure,
            "uncertainty": self.uncertainty,
            "novelty": self.novelty,
            "demand": self.demand,
            "controllability": self.controllability,
            "energy_cost": self.energy_cost,
            "repair": self.repair,
            "profile_presence": self.profile_presence,
            "self_expression": self.self_expression,
            "profile_change": self.profile_change,
            "familiarity": self.familiarity,
            "reciprocity": self.reciprocity,
            "personalization_consent": self.personalization_consent,
            "social_exposure": self.social_exposure,
            "interaction_continuity": self.interaction_continuity,
            "communication_clarity": self.communication_clarity,
            "engagement": self.engagement,
            "support_availability": self.support_availability,
        }

    @classmethod
    def from_dict(cls, values: Mapping[str, object]) -> "Cues":
        if not isinstance(values, Mapping):
            raise ValueError("cues must be an object")
        allowed = cls().__dict__.keys()
        unknown = set(values) - set(allowed)
        if unknown:
            raise ValueError(f"unknown cue fields: {', '.join(sorted(unknown))}")
        return cls(**{key: value for key, value in values.items()})


@dataclass(frozen=True)
class RegulatoryMotifs:
    """Nonliteral, current-step summaries over the dimensional state.

    ``pain`` means integrity/resource strain, ``panic`` means an acute
    low-control alarm, and ``riddle`` means an unresolved information gap that
    the system presently has capacity to explore.  The names are compact
    engineering metaphors, not claims of sensation, emotion, or diagnosis.
    Motifs are independent bounded values; they are not probabilities and do
    not need to sum to one.
    """

    pain: float = 0.0
    panic: float = 0.0
    riddle: float = 0.0

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            object.__setattr__(
                self,
                name,
                unit(getattr(self, name), name=f"regulatory_motifs.{name}"),
            )

    def to_dict(self) -> dict[str, float]:
        return {
            "pain": self.pain,
            "panic": self.panic,
            "riddle": self.riddle,
        }


@dataclass(frozen=True)
class Observation:
    event_id: str
    cues: Cues
    scope: str = "local"
    capability: Capability = Capability.CONVERSATION
    actor_ref: str | None = None
    occurred_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "event_id", bounded_text(self.event_id, name="event_id")
        )
        object.__setattr__(self, "scope", bounded_text(self.scope, name="scope"))
        if not isinstance(self.cues, Cues):
            raise ValueError("cues must be a Cues instance")
        if not isinstance(self.capability, Capability):
            try:
                object.__setattr__(self, "capability", Capability(self.capability))
            except (TypeError, ValueError) as error:
                raise ValueError("capability is not supported") from error
        if self.actor_ref is not None:
            object.__setattr__(
                self,
                "actor_ref",
                bounded_text(self.actor_ref, name="actor_ref", maximum=512),
            )
        if self.occurred_at is not None:
            object.__setattr__(
                self,
                "occurred_at",
                ensure_utc(self.occurred_at, name="occurred_at"),
            )


@dataclass(frozen=True)
class Feedback:
    event_id: str
    kind: FeedbackKind
    reason: ReasonCode
    cues: Cues = field(default_factory=Cues)
    scope: str = "local"
    capability: Capability = Capability.CONVERSATION
    actor_ref: str | None = None
    episode_id: str | None = None
    severity: float = 0.5
    confidence: float = 1.0
    source: FeedbackSource = FeedbackSource.HUMAN_FEEDBACK
    verified: bool = False
    reward: float = 0.0
    strategies: tuple[StrategyName, ...] = ()
    occurred_at: datetime | None = None
    retracts_event_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "event_id", bounded_text(self.event_id, name="event_id")
        )
        object.__setattr__(self, "scope", bounded_text(self.scope, name="scope"))
        for name, enum_type in (
            ("kind", FeedbackKind),
            ("reason", ReasonCode),
            ("capability", Capability),
            ("source", FeedbackSource),
        ):
            value = getattr(self, name)
            if not isinstance(value, enum_type):
                try:
                    object.__setattr__(self, name, enum_type(value))
                except (TypeError, ValueError) as error:
                    raise ValueError(f"{name} is not supported") from error
        if not isinstance(self.cues, Cues):
            raise ValueError("cues must be a Cues instance")
        if self.actor_ref is not None:
            object.__setattr__(
                self,
                "actor_ref",
                bounded_text(self.actor_ref, name="actor_ref", maximum=512),
            )
        if self.episode_id is not None:
            object.__setattr__(
                self,
                "episode_id",
                bounded_text(self.episode_id, name="episode_id"),
            )
        object.__setattr__(self, "severity", unit(self.severity, name="severity"))
        object.__setattr__(self, "confidence", unit(self.confidence, name="confidence"))
        object.__setattr__(self, "reward", signed_unit(self.reward, name="reward"))
        if type(self.verified) is not bool:
            raise ValueError("verified must be a boolean")
        normalized_strategies = []
        for strategy in self.strategies:
            try:
                normalized_strategies.append(StrategyName(strategy))
            except (TypeError, ValueError) as error:
                raise ValueError(f"unsupported strategy: {strategy!r}") from error
        object.__setattr__(self, "strategies", tuple(normalized_strategies))
        if self.occurred_at is not None:
            object.__setattr__(
                self,
                "occurred_at",
                ensure_utc(self.occurred_at, name="occurred_at"),
            )
        if self.retracts_event_id is not None:
            object.__setattr__(
                self,
                "retracts_event_id",
                bounded_text(self.retracts_event_id, name="retracts_event_id"),
            )
        if self.kind is FeedbackKind.RETRACTION and not self.retracts_event_id:
            raise ValueError("retraction feedback requires retracts_event_id")
        if self.kind is not FeedbackKind.RETRACTION and self.retracts_event_id:
            raise ValueError("retracts_event_id is only valid for retractions")


@dataclass(frozen=True)
class OrganismicState:
    activation: float
    pleasantness: float
    interaction_safety: float
    agency: float
    uncertainty: float
    load: float
    updated_at: datetime
    revision: int = 0

    def __post_init__(self) -> None:
        for name in (
            "activation",
            "interaction_safety",
            "agency",
            "uncertainty",
            "load",
        ):
            object.__setattr__(self, name, unit(getattr(self, name), name=name))
        object.__setattr__(
            self,
            "pleasantness",
            signed_unit(self.pleasantness, name="pleasantness"),
        )
        object.__setattr__(
            self, "updated_at", ensure_utc(self.updated_at, name="updated_at")
        )
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("revision must be a non-negative integer")

    def to_dict(self) -> dict[str, object]:
        return {
            "activation": self.activation,
            "pleasantness": self.pleasantness,
            "interaction_safety": self.interaction_safety,
            "agency": self.agency,
            "uncertainty": self.uncertainty,
            "load": self.load,
            "updated_at": timestamp_text(self.updated_at),
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, values: Mapping[str, object]) -> "OrganismicState":
        if not isinstance(values, Mapping):
            raise ValueError("organismic state must be an object")
        return cls(
            activation=values["activation"],
            pleasantness=values["pleasantness"],
            interaction_safety=values["interaction_safety"],
            agency=values["agency"],
            uncertainty=values["uncertainty"],
            load=values["load"],
            updated_at=parse_timestamp(values["updated_at"]),
            revision=values.get("revision", 0),
        )


@dataclass(frozen=True)
class ProtectionSummary:
    posture: ProtectivePosture
    caution: float
    confidence: float
    uncertainty: float
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.posture, ProtectivePosture):
            try:
                object.__setattr__(self, "posture", ProtectivePosture(self.posture))
            except (TypeError, ValueError) as error:
                raise ValueError("protection posture is not supported") from error
        for name in ("caution", "confidence", "uncertainty"):
            object.__setattr__(self, name, unit(getattr(self, name), name=name))
        reasons = tuple(self.reason_codes)
        if any(reason not in PROTECTION_REASON_CODES for reason in reasons):
            raise ValueError("protection reason_codes contain an unsupported value")
        object.__setattr__(self, "reason_codes", reasons)


@dataclass(frozen=True)
class Recruitment:
    name: StrategyName
    score: float
    intention: str
    tendency_delta: Mapping[str, float]

    def __post_init__(self) -> None:
        if not isinstance(self.name, StrategyName):
            try:
                object.__setattr__(self, "name", StrategyName(self.name))
            except (TypeError, ValueError) as error:
                raise ValueError("recruitment strategy is not supported") from error
        object.__setattr__(self, "score", unit(self.score, name="strategy score"))
        if self.intention != STRATEGY_INTENTIONS[self.name]:
            raise ValueError("recruitment intention does not match its strategy")
        deltas = dict(self.tendency_delta)
        if not set(deltas) <= GUIDANCE_FIELDS:
            raise ValueError("recruitment changes an unsupported guidance field")
        for field_name, delta in deltas.items():
            deltas[field_name] = signed_unit(
                delta, name=f"recruitment delta {field_name}"
            )
        object.__setattr__(self, "tendency_delta", MappingProxyType(deltas))


@dataclass(frozen=True)
class FeelingPacket:
    generated_at: datetime
    expires_at: datetime
    state: OrganismicState
    modes: Mapping[AutonomicMode, float]
    dominant_mode: AutonomicMode
    guidance: Mapping[str, float]
    protection: ProtectionSummary
    recruitments: tuple[Recruitment, ...]
    motifs: RegulatoryMotifs = field(default_factory=RegulatoryMotifs)

    def __post_init__(self) -> None:
        object.__setattr__(self, "generated_at", ensure_utc(self.generated_at))
        object.__setattr__(self, "expires_at", ensure_utc(self.expires_at))
        if self.expires_at <= self.generated_at:
            raise ValueError("expires_at must be after generated_at")
        if not isinstance(self.state, OrganismicState):
            raise ValueError("state must be an OrganismicState")
        if not isinstance(self.protection, ProtectionSummary):
            raise ValueError("protection must be a ProtectionSummary")
        if not isinstance(self.motifs, RegulatoryMotifs):
            raise ValueError("motifs must be RegulatoryMotifs")
        try:
            modes = {AutonomicMode(mode): value for mode, value in self.modes.items()}
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("modes must map supported modes to weights") from error
        if set(modes) != set(AutonomicMode):
            raise ValueError("modes must contain every autonomic mode")
        try:
            dominant = AutonomicMode(self.dominant_mode)
        except (TypeError, ValueError) as error:
            raise ValueError("dominant_mode is not supported") from error
        if dominant not in modes:
            raise ValueError("dominant_mode must be present in modes")
        total = sum(
            unit(value, name=f"mode.{mode.value}") for mode, value in modes.items()
        )
        if abs(total - 1.0) > 1e-6:
            raise ValueError("mode weights must sum to one")
        object.__setattr__(self, "modes", MappingProxyType(modes))
        object.__setattr__(self, "dominant_mode", dominant)
        guidance = dict(self.guidance)
        if set(guidance) != GUIDANCE_FIELDS:
            raise ValueError("guidance must contain every supported field")
        for name, value in guidance.items():
            guidance[name] = unit(value, name=f"guidance.{name}")
        object.__setattr__(self, "guidance", MappingProxyType(guidance))
        recruitments = tuple(self.recruitments)
        if any(not isinstance(item, Recruitment) for item in recruitments):
            raise ValueError("recruitments must contain Recruitment instances")
        object.__setattr__(self, "recruitments", recruitments)

    def prompt_payload(self) -> dict[str, object]:
        from .prompt import packet_payload

        return packet_payload(self)

    def developer_message(self) -> dict[str, str]:
        from .prompt import developer_message

        return developer_message(self)

    def responses_input(
        self, user_text: str, *, now: datetime | None = None
    ) -> list[dict[str, str]]:
        from .prompt import responses_input

        return responses_input(self, user_text, now=now)


@dataclass(frozen=True)
class LearningReceipt:
    status: str
    event_key: str
    durable_learning_applied: bool
    actor_memory_applied: bool
    weight_revision: int
