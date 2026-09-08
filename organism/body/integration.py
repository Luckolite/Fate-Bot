"""Combine the body's independent sensory channels into nervous-system cues."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .senses import (
    ActionProprioception,
    EnvironmentalSenses,
    ExternalSenses,
    InteractionDynamics,
    OperationalInteroception,
    ProfileSenses,
    RelationalSenses,
    RhythmSignals,
)
from ..models import (
    Capability,
    Cues,
    Observation,
    bounded_text,
    clamp,
    ensure_utc,
    unit,
)


def _noisy_or(*values: float) -> float:
    """Combine independent pressures without allowing their sum to exceed one."""

    remaining = 1.0
    for value in values:
        remaining *= 1.0 - clamp(value)
    return clamp(1.0 - remaining)


def memory_constraint_signal(memory_utilization: float) -> float:
    """Map host utilization to a near-capacity constraint signal.

    Utilization through 70% is ordinary operating headroom.  Above that point
    the signal rises quadratically, reaching one only at full utilization.
    """

    utilization = unit(memory_utilization, name="memory_utilization")
    excess = clamp((utilization - 0.70) / 0.30)
    return excess * excess


@dataclass(frozen=True)
class SensorFrame:
    """One content-free, multi-channel observation of the software body.

    ``scope`` defaults to the direct-engine ``local`` namespace.  Sector-aware
    service boundaries replace it with their authoritative isolated scope, so
    service callers do not need to duplicate that external routing value here.
    """

    event_id: str
    scope: str = "local"
    external: ExternalSenses = field(default_factory=ExternalSenses)
    interoception: OperationalInteroception = field(
        default_factory=OperationalInteroception
    )
    proprioception: ActionProprioception = field(default_factory=ActionProprioception)
    rhythms: RhythmSignals = field(default_factory=RhythmSignals)
    profile: ProfileSenses = field(default_factory=ProfileSenses)
    relationship: RelationalSenses = field(default_factory=RelationalSenses)
    interaction: InteractionDynamics = field(default_factory=InteractionDynamics)
    environment: EnvironmentalSenses = field(default_factory=EnvironmentalSenses)
    capability: Capability = Capability.CONVERSATION
    actor_ref: str | None = None
    occurred_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "event_id", bounded_text(self.event_id, name="event_id")
        )
        object.__setattr__(self, "scope", bounded_text(self.scope, name="scope"))
        for name, expected in (
            ("external", ExternalSenses),
            ("interoception", OperationalInteroception),
            ("proprioception", ActionProprioception),
            ("rhythms", RhythmSignals),
            ("profile", ProfileSenses),
            ("relationship", RelationalSenses),
            ("interaction", InteractionDynamics),
            ("environment", EnvironmentalSenses),
        ):
            if not isinstance(getattr(self, name), expected):
                raise ValueError(f"{name} must be a {expected.__name__}")
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

    def cues(self) -> Cues:
        external = self.external
        internal = self.interoception
        action = self.proprioception
        rhythms = self.rhythms
        profile = self.profile
        relationship = self.relationship
        interaction = self.interaction
        environment = self.environment

        profile_presence = profile.presence
        self_expression = profile.self_expression
        familiarity = _noisy_or(
            relationship.familiarity,
            relationship.shared_context * 0.55,
            environment.channel_familiarity * 0.30,
        )
        reciprocity = _noisy_or(
            relationship.reciprocity,
            interaction.responsiveness * 0.45,
        )
        continuity = _noisy_or(
            relationship.continuity,
            interaction.topic_continuity * 0.65,
            max(0.0, environment.context_stability - 0.5) * 0.50,
            profile.account_continuity * 0.10,
            profile.membership_continuity * 0.20,
        )
        engagement = _noisy_or(
            interaction.engagement,
            interaction.responsiveness * 0.35,
        )
        social_exposure = _noisy_or(
            environment.audience_exposure,
            (1.0 - environment.privacy) * 0.75,
            environment.interruption_pressure * 0.30,
        )
        memory_constraint = memory_constraint_signal(internal.memory_pressure)

        operational_load = _noisy_or(
            internal.queue_pressure * 0.70,
            internal.compute_pressure * 0.55,
            memory_constraint * 0.45,
            internal.latency_pressure * 0.45,
            internal.rate_limit_pressure * 0.60,
            internal.error_pressure * 0.70,
        )
        action_load = _noisy_or(
            action.commitment * 0.35,
            action.action_cost * 0.65,
            action.tool_activity * (1.0 - action.reversibility) * 0.45,
        )
        rhythmic_load = _noisy_or(
            rhythms.sustained_load * 0.65,
            rhythms.time_pressure * 0.50,
            rhythms.rest_debt * (1.0 - rhythms.recovery_opportunity) * 0.70,
        )
        social_load = _noisy_or(
            interaction.pace_pressure * 0.60,
            interaction.correction_pressure * 0.35,
            environment.interruption_pressure * 0.55,
            social_exposure * 0.35,
        )
        energy_cost = _noisy_or(
            1.0 - internal.energy_reserve,
            operational_load * 0.55,
            action_load * 0.65,
            rhythmic_load * 0.70,
            social_load * 0.45,
        )
        demand = _noisy_or(
            external.demand,
            operational_load * 0.65,
            action_load * 0.75,
            rhythms.time_pressure * 0.55,
            social_load * 0.60,
        )
        uncertainty = _noisy_or(
            external.uncertainty,
            internal.error_pressure * 0.65,
            action.uncertainty * 0.75,
            max(0.0, 0.5 - interaction.clarity) * 0.70,
            interaction.repetition * 0.20,
            interaction.correction_pressure * 0.35,
        )
        # Resource use is ordinary until it approaches exhaustion. Near the
        # cap, memory pressure becomes a nonlinear constraint signal: the
        # software body has less room to act, switch context, or recover.
        internal_threat = memory_constraint * 0.45
        controllability = min(
            external.controllability,
            action.controllability,
            1.0 - internal.rate_limit_pressure * 0.70,
            1.0 - internal.error_pressure * 0.60,
            1.0 - memory_constraint * 0.85,
            1.0 - internal.queue_pressure * 0.55,
            1.0 - interaction.pace_pressure * 0.30,
            1.0 - environment.interruption_pressure * 0.45,
        )
        boundary_pressure = _noisy_or(
            external.boundary_pressure,
            action.tool_activity * (1.0 - action.reversibility) * 0.35,
            (1.0 - interaction.boundary_alignment) * 0.70,
            interaction.correction_pressure * 0.20,
        )
        connection = _noisy_or(
            external.connection,
            familiarity * 0.35,
            reciprocity * 0.30,
            engagement * 0.25,
            relationship.shared_context * 0.20,
            environment.human_support_available * 0.15,
        )
        novelty = _noisy_or(
            external.novelty,
            (1.0 - environment.context_stability) * engagement * 0.25,
        )
        repair = _noisy_or(external.repair, relationship.repair_progress)

        return Cues(
            safety=external.safety,
            threat=_noisy_or(external.threat, internal_threat),
            connection=connection,
            boundary_pressure=boundary_pressure,
            uncertainty=uncertainty,
            novelty=novelty,
            demand=demand,
            controllability=controllability,
            energy_cost=energy_cost,
            repair=repair,
            profile_presence=profile_presence,
            self_expression=self_expression,
            profile_change=profile.profile_change,
            familiarity=familiarity,
            reciprocity=reciprocity,
            personalization_consent=(1.0 if profile.personalization_consent else 0.0),
            social_exposure=social_exposure,
            interaction_continuity=continuity,
            communication_clarity=interaction.clarity,
            engagement=engagement,
            support_availability=environment.human_support_available,
        )

    def observation(self) -> Observation:
        return Observation(
            event_id=self.event_id,
            cues=self.cues(),
            scope=self.scope,
            capability=self.capability,
            actor_ref=self.actor_ref,
            occurred_at=self.occurred_at,
        )


__all__ = ["SensorFrame", "memory_constraint_signal"]
