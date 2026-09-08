"""Typed sensory channels for the computational body.

Profile strings are accepted only as short-lived sensory input.  The body
reduces them to bounded structural signals before an ``Observation`` is
formed; raw profile text is never part of organism state or learning evidence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..models import bounded_text, clamp, unit


def _optional_profile_text(
    value: str | None,
    *,
    name: str,
    maximum: int,
) -> str | None:
    if value is None:
        return None
    return bounded_text(value, name=name, maximum=maximum)


def _optional_age(value: float | None, *, name: str) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0.0 <= float(value) <= 36500.0
    ):
        raise ValueError(f"{name} must be finite and between 0 and 36500 days")
    return float(value)


@dataclass(frozen=True)
class ProfileSenses:
    """Ephemeral, user-facing profile information and visible metadata.

    The raw strings make a current profile available to the body, but
    ``SensorFrame.cues()`` exposes only numeric structure.  Supplying a name or
    bio does not make a person safer, riskier, more truthful, or more trusted.
    The strings may cross the language-cortex boundary only when
    ``personalization_consent`` is explicitly true.
    """

    display_name: str | None = None
    bio: str | None = None
    pronouns: str | None = None
    avatar_present: bool = False
    banner_present: bool = False
    custom_status_present: bool = False
    role_count: int = 0
    account_age_days: float | None = None
    membership_age_days: float | None = None
    profile_change: float = 0.0
    personalization_consent: bool = False
    name_use_allowed: bool = False

    def __post_init__(self) -> None:
        for name, maximum in (
            ("display_name", 128),
            ("bio", 2048),
            ("pronouns", 64),
        ):
            object.__setattr__(
                self,
                name,
                _optional_profile_text(
                    getattr(self, name),
                    name=f"profile.{name}",
                    maximum=maximum,
                ),
            )
        for name in (
            "avatar_present",
            "banner_present",
            "custom_status_present",
            "personalization_consent",
            "name_use_allowed",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"profile.{name} must be a boolean")
        if type(self.role_count) is not int or not 0 <= self.role_count <= 250:
            raise ValueError("profile.role_count must be between 0 and 250")
        for name in ("account_age_days", "membership_age_days"):
            object.__setattr__(
                self,
                name,
                _optional_age(getattr(self, name), name=f"profile.{name}"),
            )
        object.__setattr__(
            self,
            "profile_change",
            unit(self.profile_change, name="profile.profile_change"),
        )
        if self.name_use_allowed and not self.personalization_consent:
            raise ValueError(
                "profile.name_use_allowed requires personalization_consent"
            )
        if self.name_use_allowed and self.display_name is None:
            raise ValueError("profile.name_use_allowed requires display_name")

    @property
    def presence(self) -> float:
        """Return profile richness without interpreting what the text means."""

        bio_density = clamp(len(self.bio or "") / 160.0)
        return clamp(
            0.15 * (self.display_name is not None)
            + 0.25 * bio_density
            + 0.10 * (self.pronouns is not None)
            + 0.15 * self.avatar_present
            + 0.10 * self.banner_present
            + 0.05 * self.custom_status_present
            + 0.10 * clamp(self.role_count / 8.0)
            + 0.05 * (self.account_age_days is not None)
            + 0.05 * (self.membership_age_days is not None)
        )

    @property
    def self_expression(self) -> float:
        """Return visible expressive density, not a personality inference."""

        bio_density = clamp(len(self.bio or "") / 160.0)
        return clamp(
            0.15 * (self.display_name is not None)
            + 0.45 * bio_density
            + 0.15 * (self.pronouns is not None)
            + 0.10 * self.avatar_present
            + 0.10 * self.banner_present
            + 0.05 * self.custom_status_present
        )

    @property
    def account_continuity(self) -> float:
        return (
            0.0
            if self.account_age_days is None
            else clamp(self.account_age_days / 3650.0)
        )

    @property
    def membership_continuity(self) -> float:
        return (
            0.0
            if self.membership_age_days is None
            else clamp(self.membership_age_days / 730.0)
        )


@dataclass(frozen=True)
class RelationalSenses:
    """Current relationship shape supplied by a trusted application layer."""

    familiarity: float = 0.0
    reciprocity: float = 0.0
    continuity: float = 0.0
    shared_context: float = 0.0
    repair_progress: float = 0.0

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            object.__setattr__(
                self,
                name,
                unit(getattr(self, name), name=f"relationship.{name}"),
            )


@dataclass(frozen=True)
class InteractionDynamics:
    """Content-free dynamics of the present exchange."""

    engagement: float = 0.0
    clarity: float = 0.5
    responsiveness: float = 0.0
    topic_continuity: float = 0.0
    pace_pressure: float = 0.0
    repetition: float = 0.0
    correction_pressure: float = 0.0
    boundary_alignment: float = 1.0

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            object.__setattr__(
                self,
                name,
                unit(getattr(self, name), name=f"interaction.{name}"),
            )


@dataclass(frozen=True)
class EnvironmentalSenses:
    """Social and situational conditions surrounding the exchange."""

    audience_exposure: float = 0.0
    privacy: float = 1.0
    channel_familiarity: float = 0.0
    interruption_pressure: float = 0.0
    human_support_available: float = 0.0
    context_stability: float = 0.5

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            object.__setattr__(
                self,
                name,
                unit(getattr(self, name), name=f"environment.{name}"),
            )


@dataclass(frozen=True)
class ExternalSenses:
    """Trusted observations about the current interaction and environment.

    These values are deliberately numeric. Raw messages and model-authored
    interpretations do not belong in the body layer.
    """

    safety: float = 0.5
    connection: float = 0.5
    threat: float = 0.0
    boundary_pressure: float = 0.0
    uncertainty: float = 0.25
    novelty: float = 0.0
    demand: float = 0.0
    controllability: float = 0.7
    repair: float = 0.0

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            object.__setattr__(
                self,
                name,
                unit(getattr(self, name), name=f"external_senses.{name}"),
            )


@dataclass(frozen=True)
class OperationalInteroception:
    """The software body's own operational and resource condition."""

    latency_pressure: float = 0.0
    queue_pressure: float = 0.0
    rate_limit_pressure: float = 0.0
    compute_pressure: float = 0.0
    memory_pressure: float = 0.0
    error_pressure: float = 0.0
    energy_reserve: float = 0.8

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            object.__setattr__(
                self,
                name,
                unit(getattr(self, name), name=f"interoception.{name}"),
            )

    @property
    def pressure(self) -> float:
        return max(
            self.latency_pressure,
            self.queue_pressure,
            self.rate_limit_pressure,
            self.compute_pressure,
            self.memory_pressure,
            self.error_pressure,
        )


@dataclass(frozen=True)
class ActionProprioception:
    """Awareness of the action currently being attempted."""

    commitment: float = 0.0
    tool_activity: float = 0.0
    action_cost: float = 0.0
    uncertainty: float = 0.0
    controllability: float = 1.0
    reversibility: float = 1.0

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            object.__setattr__(
                self,
                name,
                unit(getattr(self, name), name=f"proprioception.{name}"),
            )


@dataclass(frozen=True)
class RhythmSignals:
    """Time-shaped load and opportunities for software recovery."""

    sustained_load: float = 0.0
    time_pressure: float = 0.0
    rest_debt: float = 0.0
    recovery_opportunity: float = 0.5

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            object.__setattr__(
                self,
                name,
                unit(getattr(self, name), name=f"rhythms.{name}"),
            )


__all__ = [
    "ActionProprioception",
    "EnvironmentalSenses",
    "ExternalSenses",
    "InteractionDynamics",
    "OperationalInteroception",
    "ProfileSenses",
    "RelationalSenses",
    "RhythmSignals",
]
