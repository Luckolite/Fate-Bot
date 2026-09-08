"""Typed contracts for a simulated functional-experience workspace.

All values are numeric or enumerated.  These structures cannot carry raw
messages, identifiers, diagnoses, permissions, or claims of consciousness.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from ..models import (
    Cues,
    OrganismicState,
    ProtectionSummary,
    ProtectivePosture,
    ensure_utc,
    signed_unit,
    unit,
)

CUE_FIELDS = (
    "safety",
    "threat",
    "connection",
    "boundary_pressure",
    "uncertainty",
    "novelty",
    "demand",
    "controllability",
    "energy_cost",
    "repair",
    "profile_presence",
    "self_expression",
    "profile_change",
    "familiarity",
    "reciprocity",
    "personalization_consent",
    "social_exposure",
    "interaction_continuity",
    "communication_clarity",
    "engagement",
    "support_availability",
)

DYNAMIC_FIELDS = (
    "activation",
    "valence",
    "interaction_safety",
    "agency",
    "uncertainty",
    "load",
    "connection_readiness",
    "surprise",
)

MAX_DYNAMICS_REVISION = 65_535


class ExperienceDomain(str, Enum):
    PROTECTION = "protection"
    BOUNDARY = "boundary"
    VERIFICATION = "verification"
    CONNECTION = "connection"
    RECOVERY = "recovery"
    AGENCY = "agency"
    RESOURCE_PRESSURE = "resource_pressure"
    REPAIR = "repair"
    PERSON = "person"
    RELATIONSHIP = "relationship"
    COMMUNICATION = "communication"
    SOCIAL_CONTEXT = "social_context"


class NeedDomain(str, Enum):
    STABILITY = "stability"
    RECOVERY = "recovery"
    AGENCY = "agency"
    VERIFICATION = "verification"
    CONNECTION = "connection"


class ChangePattern(str, Enum):
    INITIAL = "initial"
    STEADY = "steady"
    INCREASING_STRAIN = "increasing_strain"
    DECREASING_STRAIN = "decreasing_strain"
    MIXED = "mixed"


@dataclass(frozen=True)
class CueExpectation:
    expected: Cues
    confidence: float = 0.5

    def __post_init__(self) -> None:
        if not isinstance(self.expected, Cues):
            raise ValueError("expected must be a Cues instance")
        object.__setattr__(
            self,
            "confidence",
            unit(self.confidence, name="expectation.confidence"),
        )


@dataclass(frozen=True)
class PredictiveError:
    signed: Mapping[str, float]
    absolute: Mapping[str, float]
    surprise: float
    confidence: float

    def __post_init__(self) -> None:
        signed = dict(self.signed)
        absolute = dict(self.absolute)
        expected = set(CUE_FIELDS)
        if set(signed) != expected or set(absolute) != expected:
            raise ValueError("prediction errors must contain every cue field")
        for name in CUE_FIELDS:
            signed[name] = signed_unit(signed[name], name=f"prediction.signed.{name}")
            absolute[name] = unit(absolute[name], name=f"prediction.absolute.{name}")
            if abs(abs(signed[name]) - absolute[name]) > 1e-9:
                raise ValueError("absolute prediction error must match signed error")
        object.__setattr__(self, "signed", MappingProxyType(signed))
        object.__setattr__(self, "absolute", MappingProxyType(absolute))
        object.__setattr__(
            self, "surprise", unit(self.surprise, name="prediction.surprise")
        )
        object.__setattr__(
            self,
            "confidence",
            unit(self.confidence, name="prediction.confidence"),
        )


@dataclass(frozen=True)
class FunctionalSelfModel:
    activation: float
    valence: float
    interaction_safety: float
    agency: float
    uncertainty: float
    load: float
    stability_need: float
    recovery_need: float
    agency_need: float
    verification_need: float
    connection_readiness: float
    caution: float
    protection_confidence: float
    resource_pressure: float
    profile_presence: float
    self_expression: float
    profile_change: float
    familiarity: float
    reciprocity: float
    personalization_consent: float
    social_exposure: float
    interaction_continuity: float
    communication_clarity: float
    engagement: float
    support_availability: float
    posture: ProtectivePosture
    dominant_need: NeedDomain
    pain: float = 0.0
    panic: float = 0.0
    riddle: float = 0.0

    def __post_init__(self) -> None:
        for name in (
            "activation",
            "interaction_safety",
            "agency",
            "uncertainty",
            "load",
            "stability_need",
            "recovery_need",
            "agency_need",
            "verification_need",
            "connection_readiness",
            "caution",
            "protection_confidence",
            "resource_pressure",
            "profile_presence",
            "self_expression",
            "profile_change",
            "familiarity",
            "reciprocity",
            "personalization_consent",
            "social_exposure",
            "interaction_continuity",
            "communication_clarity",
            "engagement",
            "support_availability",
            "pain",
            "panic",
            "riddle",
        ):
            object.__setattr__(
                self, name, unit(getattr(self, name), name=f"self_model.{name}")
            )
        object.__setattr__(
            self,
            "valence",
            signed_unit(self.valence, name="self_model.valence"),
        )
        try:
            object.__setattr__(self, "posture", ProtectivePosture(self.posture))
        except (TypeError, ValueError) as error:
            raise ValueError("self_model posture is unsupported") from error
        try:
            object.__setattr__(self, "dominant_need", NeedDomain(self.dominant_need))
        except (TypeError, ValueError) as error:
            raise ValueError("self_model dominant_need is unsupported") from error


@dataclass(frozen=True)
class ExperienceDynamics:
    """Identifier-free, short-lived change across consecutive moments."""

    impulse: Mapping[str, float]
    momentum: Mapping[str, float]
    flux: float
    volatility: float
    escalation: float
    settling: float
    retention: float
    pattern: ChangePattern
    revision: int = 0

    def __post_init__(self) -> None:
        impulse = dict(self.impulse)
        momentum = dict(self.momentum)
        expected = set(DYNAMIC_FIELDS)
        if set(impulse) != expected or set(momentum) != expected:
            raise ValueError("experience dynamics must contain every dynamic field")
        for name in DYNAMIC_FIELDS:
            impulse[name] = signed_unit(impulse[name], name=f"dynamics.impulse.{name}")
            momentum[name] = signed_unit(
                momentum[name], name=f"dynamics.momentum.{name}"
            )
        for name in (
            "flux",
            "volatility",
            "escalation",
            "settling",
            "retention",
        ):
            object.__setattr__(
                self, name, unit(getattr(self, name), name=f"dynamics.{name}")
            )
        try:
            object.__setattr__(self, "pattern", ChangePattern(self.pattern))
        except (TypeError, ValueError) as error:
            raise ValueError("dynamics pattern is unsupported") from error
        if (
            type(self.revision) is not int
            or not 0 <= self.revision <= MAX_DYNAMICS_REVISION
        ):
            raise ValueError("dynamics revision must be an integer between 0 and 65535")
        object.__setattr__(self, "impulse", MappingProxyType(impulse))
        object.__setattr__(self, "momentum", MappingProxyType(momentum))


@dataclass(frozen=True)
class AttentionCandidate:
    domain: ExperienceDomain
    intensity: float
    urgency: float = 0.0
    novelty: float = 0.0
    relevance: float = 0.5
    prediction_error_magnitude: float = 0.0
    temporal_pull: float = 0.0

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "domain", ExperienceDomain(self.domain))
        except (TypeError, ValueError) as error:
            raise ValueError("attention domain is unsupported") from error
        for name in (
            "intensity",
            "urgency",
            "novelty",
            "relevance",
            "prediction_error_magnitude",
            "temporal_pull",
        ):
            object.__setattr__(
                self, name, unit(getattr(self, name), name=f"candidate.{name}")
            )


@dataclass(frozen=True)
class FocusTrace:
    domain: ExperienceDomain
    strength: float

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "domain", ExperienceDomain(self.domain))
        except (TypeError, ValueError) as error:
            raise ValueError("focus domain is unsupported") from error
        object.__setattr__(self, "strength", unit(self.strength, name="focus.strength"))


@dataclass(frozen=True)
class TemporalContinuity:
    traces: tuple[FocusTrace, ...] = ()
    revision: int = 0

    def __post_init__(self) -> None:
        traces = tuple(self.traces)
        if len(traces) > 4 or any(not isinstance(item, FocusTrace) for item in traces):
            raise ValueError("continuity supports at most four focus traces")
        domains = [item.domain for item in traces]
        if len(domains) != len(set(domains)):
            raise ValueError("continuity focus domains must be unique")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("continuity revision must be a non-negative integer")
        object.__setattr__(self, "traces", traces)

    def strength_for(self, domain: ExperienceDomain) -> float:
        selected = ExperienceDomain(domain)
        return next(
            (trace.strength for trace in self.traces if trace.domain is selected),
            0.0,
        )


@dataclass(frozen=True)
class AttendedCandidate:
    candidate: AttentionCandidate
    salience: float
    share: float
    continuity_bias: float = 0.0
    temporal_pull: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, AttentionCandidate):
            raise ValueError("attended candidate must contain an AttentionCandidate")
        for name in ("salience", "share", "continuity_bias", "temporal_pull"):
            object.__setattr__(
                self, name, unit(getattr(self, name), name=f"attention.{name}")
            )
        if abs(self.temporal_pull - self.candidate.temporal_pull) > 1e-9:
            raise ValueError(
                "attended temporal_pull must match its attention candidate"
            )

    @property
    def domain(self) -> ExperienceDomain:
        return self.candidate.domain


@dataclass(frozen=True)
class WorkspaceBroadcast:
    capacity: int
    items: tuple[AttendedCandidate, ...]
    overall_salience: float
    surprise: float

    def __post_init__(self) -> None:
        overall_salience = unit(
            self.overall_salience, name="workspace.overall_salience"
        )
        surprise = unit(self.surprise, name="workspace.surprise")
        if type(self.capacity) is not int or not 1 <= self.capacity <= 4:
            raise ValueError("workspace capacity must be between one and four")
        items = tuple(self.items)
        if len(items) > self.capacity or any(
            not isinstance(item, AttendedCandidate) for item in items
        ):
            raise ValueError("workspace items exceed capacity or are invalid")
        domains = [item.domain for item in items]
        if len(domains) != len(set(domains)):
            raise ValueError("workspace domains must be unique")
        if items:
            expected_order = tuple(
                sorted(items, key=lambda item: (-item.salience, item.domain.value))
            )
            if items != expected_order:
                raise ValueError(
                    "workspace items must use deterministic salience order"
                )
            total_salience = sum(item.salience for item in items)
            expected_shares = _attention_shares(items, total_salience)
            if any(
                abs(item.share - expected) > 1e-6
                for item, expected in zip(items, expected_shares)
            ):
                raise ValueError("workspace shares must be derived from salience")
        expected_salience = max((item.salience for item in items), default=0.0)
        if abs(overall_salience - expected_salience) > 1e-9:
            raise ValueError("workspace overall_salience must match its leading item")
        object.__setattr__(self, "items", items)
        object.__setattr__(self, "overall_salience", overall_salience)
        object.__setattr__(self, "surprise", surprise)


@dataclass(frozen=True)
class ExperienceMoment:
    generated_at: datetime
    self_model: FunctionalSelfModel
    prediction: PredictiveError
    dynamics: ExperienceDynamics
    broadcast: WorkspaceBroadcast
    continuity: TemporalContinuity
    next_expectation: CueExpectation

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "generated_at",
            ensure_utc(self.generated_at, name="experience.generated_at"),
        )
        for name, expected in (
            ("self_model", FunctionalSelfModel),
            ("prediction", PredictiveError),
            ("dynamics", ExperienceDynamics),
            ("broadcast", WorkspaceBroadcast),
            ("continuity", TemporalContinuity),
            ("next_expectation", CueExpectation),
        ):
            if not isinstance(getattr(self, name), expected):
                raise ValueError(f"{name} must be a {expected.__name__}")
        if abs(self.broadcast.surprise - self.prediction.surprise) > 1e-9:
            raise ValueError("broadcast surprise must match predictive surprise")


def _attention_shares(
    items: tuple[AttendedCandidate, ...], total_salience: float
) -> tuple[float, ...]:
    if not items:
        return ()
    if total_salience <= 0.0:
        return (1.0,) + (0.0,) * (len(items) - 1)
    shares = [item.salience / total_salience for item in items]
    shares[0] += 1.0 - sum(shares)
    return tuple(shares)


def validate_experience_inputs(
    state: OrganismicState,
    protection: ProtectionSummary,
) -> None:
    if not isinstance(state, OrganismicState):
        raise ValueError("state must be an OrganismicState")
    if not isinstance(protection, ProtectionSummary):
        raise ValueError("protection must be a ProtectionSummary")


__all__ = [
    "AttentionCandidate",
    "AttendedCandidate",
    "CUE_FIELDS",
    "DYNAMIC_FIELDS",
    "MAX_DYNAMICS_REVISION",
    "ChangePattern",
    "CueExpectation",
    "ExperienceDomain",
    "ExperienceDynamics",
    "ExperienceMoment",
    "FocusTrace",
    "FunctionalSelfModel",
    "NeedDomain",
    "PredictiveError",
    "TemporalContinuity",
    "WorkspaceBroadcast",
]
