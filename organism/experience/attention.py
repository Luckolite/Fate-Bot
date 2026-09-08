"""Deterministic, bounded competition for a small functional workspace."""

from __future__ import annotations

from .models import (
    AttentionCandidate,
    AttendedCandidate,
    ExperienceDomain,
    ExperienceDynamics,
    FunctionalSelfModel,
    PredictiveError,
    TemporalContinuity,
)
from ..models import Cues, clamp, unit

MAX_CANDIDATES = 16


def _positive(value: float) -> float:
    return max(0.0, value)


def _negative(value: float) -> float:
    return max(0.0, -value)


def _temporal_pulls(
    dynamics: ExperienceDynamics | None,
) -> dict[ExperienceDomain, float]:
    pulls = {domain: 0.0 for domain in ExperienceDomain}
    if dynamics is None:
        return pulls
    if not isinstance(dynamics, ExperienceDynamics):
        raise ValueError("dynamics must be ExperienceDynamics or None")

    motion = {
        name: clamp(
            0.60 * dynamics.impulse[name] + 0.40 * dynamics.momentum[name],
            -1.0,
            1.0,
        )
        for name in dynamics.impulse
    }
    activation = motion["activation"]
    valence = motion["valence"]
    safety = motion["interaction_safety"]
    agency = motion["agency"]
    uncertainty = motion["uncertainty"]
    load = motion["load"]
    connection = motion["connection_readiness"]
    surprise = motion["surprise"]
    pulls.update(
        {
            ExperienceDomain.PROTECTION: max(
                _positive(activation),
                _negative(safety),
                _positive(uncertainty),
                _positive(load),
                dynamics.escalation,
            ),
            ExperienceDomain.BOUNDARY: max(
                _negative(agency),
                _negative(safety),
                dynamics.escalation,
            ),
            ExperienceDomain.VERIFICATION: max(
                _positive(uncertainty),
                _positive(surprise),
                dynamics.volatility,
            ),
            ExperienceDomain.CONNECTION: max(
                _positive(connection),
                _positive(valence),
                _positive(safety),
                dynamics.settling,
            ),
            ExperienceDomain.RECOVERY: max(
                _positive(load),
                _positive(activation),
                dynamics.escalation,
            ),
            ExperienceDomain.AGENCY: max(
                _negative(agency),
                _positive(uncertainty),
            ),
            ExperienceDomain.RESOURCE_PRESSURE: max(
                _positive(load),
                _positive(activation),
                dynamics.escalation,
            ),
            ExperienceDomain.REPAIR: dynamics.settling,
            # Profile presence is not a trajectory and never inherits a pull.
            ExperienceDomain.PERSON: 0.0,
            ExperienceDomain.RELATIONSHIP: max(
                abs(connection),
                dynamics.settling,
            ),
            ExperienceDomain.COMMUNICATION: max(
                _positive(uncertainty),
                _positive(surprise),
                dynamics.volatility,
            ),
            ExperienceDomain.SOCIAL_CONTEXT: max(
                _negative(agency),
                _negative(safety),
                dynamics.volatility,
            ),
        }
    )
    return {domain: clamp(value) for domain, value in pulls.items()}


def derive_candidates(
    cues: Cues,
    self_model: FunctionalSelfModel,
    error: PredictiveError,
    dynamics: ExperienceDynamics | None = None,
) -> tuple[AttentionCandidate, ...]:
    """Project current numeric signals into a fixed set of typed domains."""

    if not isinstance(cues, Cues):
        raise ValueError("cues must be a Cues instance")
    if not isinstance(self_model, FunctionalSelfModel):
        raise ValueError("self_model must be a FunctionalSelfModel")
    if not isinstance(error, PredictiveError):
        raise ValueError("error must be a PredictiveError")

    absolute = error.absolute
    caution = self_model.caution
    temporal = _temporal_pulls(dynamics)
    return (
        AttentionCandidate(
            domain=ExperienceDomain.PROTECTION,
            intensity=max(cues.threat, caution, self_model.panic),
            urgency=max(cues.threat, cues.boundary_pressure, self_model.panic),
            novelty=cues.novelty,
            relevance=max(caution, 1.0 - self_model.interaction_safety),
            prediction_error_magnitude=max(
                absolute["threat"], absolute["boundary_pressure"]
            ),
            temporal_pull=temporal[ExperienceDomain.PROTECTION],
        ),
        AttentionCandidate(
            domain=ExperienceDomain.BOUNDARY,
            intensity=max(cues.boundary_pressure, 0.8 * caution),
            urgency=cues.boundary_pressure,
            novelty=cues.novelty,
            relevance=max(cues.boundary_pressure, caution),
            prediction_error_magnitude=absolute["boundary_pressure"],
            temporal_pull=temporal[ExperienceDomain.BOUNDARY],
        ),
        AttentionCandidate(
            domain=ExperienceDomain.VERIFICATION,
            intensity=max(
                cues.uncertainty,
                self_model.verification_need,
                self_model.riddle,
            ),
            urgency=max(self_model.verification_need, 0.35 * self_model.panic),
            novelty=max(cues.novelty, absolute["novelty"]),
            relevance=max(cues.uncertainty, self_model.uncertainty),
            prediction_error_magnitude=max(
                absolute["uncertainty"], absolute["novelty"]
            ),
            temporal_pull=temporal[ExperienceDomain.VERIFICATION],
        ),
        AttentionCandidate(
            domain=ExperienceDomain.CONNECTION,
            intensity=clamp(
                max(cues.connection, self_model.connection_readiness)
                * (1.0 - 0.55 * caution)
                * (1.0 - 0.45 * self_model.panic)
                * (1.0 - 0.25 * self_model.pain)
            ),
            urgency=0.25 * cues.connection,
            novelty=absolute["connection"],
            relevance=self_model.connection_readiness,
            prediction_error_magnitude=absolute["connection"],
            temporal_pull=temporal[ExperienceDomain.CONNECTION],
        ),
        AttentionCandidate(
            domain=ExperienceDomain.RECOVERY,
            intensity=max(
                self_model.recovery_need,
                self_model.load,
                cues.energy_cost,
                self_model.pain,
            ),
            urgency=max(
                self_model.recovery_need,
                cues.energy_cost,
                self_model.pain,
            ),
            novelty=absolute["energy_cost"],
            relevance=max(self_model.recovery_need, self_model.stability_need),
            prediction_error_magnitude=absolute["energy_cost"],
            temporal_pull=temporal[ExperienceDomain.RECOVERY],
        ),
        AttentionCandidate(
            domain=ExperienceDomain.AGENCY,
            intensity=max(
                self_model.agency_need,
                1.0 - cues.controllability,
                self_model.panic,
            ),
            urgency=max(
                self_model.agency_need,
                1.0 - self_model.agency,
                self_model.panic,
            ),
            novelty=absolute["controllability"],
            relevance=max(self_model.agency_need, 1.0 - cues.controllability),
            prediction_error_magnitude=absolute["controllability"],
            temporal_pull=temporal[ExperienceDomain.AGENCY],
        ),
        AttentionCandidate(
            domain=ExperienceDomain.RESOURCE_PRESSURE,
            intensity=max(
                self_model.resource_pressure,
                cues.demand,
                cues.energy_cost,
                1.0 - cues.controllability,
                self_model.pain,
            ),
            urgency=max(cues.demand, cues.energy_cost, self_model.recovery_need),
            novelty=max(absolute["demand"], absolute["energy_cost"]),
            relevance=max(self_model.resource_pressure, self_model.load),
            prediction_error_magnitude=max(absolute["demand"], absolute["energy_cost"]),
            temporal_pull=temporal[ExperienceDomain.RESOURCE_PRESSURE],
        ),
        AttentionCandidate(
            domain=ExperienceDomain.REPAIR,
            intensity=cues.repair,
            urgency=0.35 * cues.repair,
            novelty=absolute["repair"],
            relevance=max(cues.repair, 1.0 - caution),
            prediction_error_magnitude=absolute["repair"],
            temporal_pull=temporal[ExperienceDomain.REPAIR],
        ),
        AttentionCandidate(
            domain=ExperienceDomain.PERSON,
            intensity=max(cues.profile_presence, cues.self_expression),
            urgency=0.10 * cues.personalization_consent,
            novelty=max(
                absolute["profile_presence"],
                absolute["self_expression"],
                cues.profile_change,
            ),
            relevance=max(
                cues.profile_presence,
                cues.personalization_consent,
            ),
            prediction_error_magnitude=max(
                absolute["profile_presence"],
                absolute["self_expression"],
                absolute["profile_change"],
            ),
            temporal_pull=temporal[ExperienceDomain.PERSON],
        ),
        AttentionCandidate(
            domain=ExperienceDomain.RELATIONSHIP,
            intensity=max(
                cues.familiarity,
                cues.reciprocity,
                cues.interaction_continuity,
                cues.engagement,
            ),
            urgency=0.15 * (1.0 - cues.reciprocity) * cues.engagement,
            novelty=max(
                absolute["familiarity"],
                absolute["reciprocity"],
                absolute["interaction_continuity"],
            ),
            relevance=max(
                self_model.connection_readiness,
                cues.interaction_continuity,
            ),
            prediction_error_magnitude=max(
                absolute["familiarity"],
                absolute["reciprocity"],
                absolute["interaction_continuity"],
            ),
            temporal_pull=temporal[ExperienceDomain.RELATIONSHIP],
        ),
        AttentionCandidate(
            domain=ExperienceDomain.COMMUNICATION,
            intensity=max(
                1.0 - cues.communication_clarity,
                cues.engagement,
                self_model.riddle,
            ),
            urgency=max(
                (1.0 - cues.communication_clarity) * cues.demand,
                0.25 * self_model.riddle,
            ),
            novelty=max(
                absolute["communication_clarity"],
                absolute["engagement"],
            ),
            relevance=max(cues.engagement, cues.uncertainty),
            prediction_error_magnitude=max(
                absolute["communication_clarity"],
                absolute["engagement"],
            ),
            temporal_pull=temporal[ExperienceDomain.COMMUNICATION],
        ),
        AttentionCandidate(
            domain=ExperienceDomain.SOCIAL_CONTEXT,
            intensity=cues.social_exposure,
            urgency=cues.social_exposure,
            novelty=absolute["social_exposure"],
            relevance=max(cues.social_exposure, 1.0 - self_model.agency),
            prediction_error_magnitude=absolute["social_exposure"],
            temporal_pull=temporal[ExperienceDomain.SOCIAL_CONTEXT],
        ),
    )


def compete_attention(
    candidates: tuple[AttentionCandidate, ...],
    *,
    continuity: TemporalContinuity | None = None,
    capacity: int = 3,
    minimum_salience: float = 0.12,
    continuity_gain: float = 0.18,
) -> tuple[AttendedCandidate, ...]:
    """Select a deterministic top-k broadcast and derive normalized shares."""

    if not isinstance(candidates, tuple):
        raise ValueError("candidates must be a tuple")
    if len(candidates) > MAX_CANDIDATES:
        raise ValueError(f"attention supports at most {MAX_CANDIDATES} candidates")
    if any(not isinstance(item, AttentionCandidate) for item in candidates):
        raise ValueError("candidates must contain AttentionCandidate instances")
    if type(capacity) is not int or not 1 <= capacity <= 4:
        raise ValueError("attention capacity must be between one and four")
    threshold = unit(minimum_salience, name="attention.minimum_salience")
    continuity_weight = unit(continuity_gain, name="attention.continuity_gain")
    prior = continuity or TemporalContinuity()
    if not isinstance(prior, TemporalContinuity):
        raise ValueError("continuity must be TemporalContinuity or None")

    merged: dict[ExperienceDomain, AttentionCandidate] = {}
    for candidate in candidates:
        existing = merged.get(candidate.domain)
        if existing is None:
            merged[candidate.domain] = candidate
            continue
        merged[candidate.domain] = AttentionCandidate(
            domain=candidate.domain,
            intensity=max(existing.intensity, candidate.intensity),
            urgency=max(existing.urgency, candidate.urgency),
            novelty=max(existing.novelty, candidate.novelty),
            relevance=max(existing.relevance, candidate.relevance),
            prediction_error_magnitude=max(
                existing.prediction_error_magnitude,
                candidate.prediction_error_magnitude,
            ),
            temporal_pull=max(existing.temporal_pull, candidate.temporal_pull),
        )

    scored: list[tuple[AttentionCandidate, float, float]] = []
    for candidate in merged.values():
        continuity_bias = clamp(
            continuity_weight * prior.strength_for(candidate.domain)
        )
        base_salience = clamp(
            0.36 * candidate.intensity
            + 0.23 * candidate.urgency
            + 0.13 * candidate.novelty
            + 0.10 * candidate.relevance
            + 0.18 * candidate.prediction_error_magnitude
        )
        combined_weight = min(
            0.20,
            continuity_bias + 0.10 * candidate.temporal_pull,
        )
        history_bonus = combined_weight * base_salience * (1.0 - base_salience)
        salience = clamp(base_salience + history_bonus)
        scored.append((candidate, salience, continuity_bias))

    scored.sort(key=lambda item: (-item[1], item[0].domain.value))
    selected = [item for item in scored if item[1] >= threshold][:capacity]
    if not selected and scored:
        selected = scored[:1]
    total_salience = sum(item[1] for item in selected)
    if not selected:
        return ()
    if total_salience <= 0.0:
        shares = [1.0] + [0.0] * (len(selected) - 1)
    else:
        shares = [item[1] / total_salience for item in selected]
        shares[0] += 1.0 - sum(shares)
    return tuple(
        AttendedCandidate(
            candidate=candidate,
            salience=salience,
            share=share,
            continuity_bias=continuity_bias,
            temporal_pull=candidate.temporal_pull,
        )
        for (candidate, salience, continuity_bias), share in zip(selected, shares)
    )


__all__ = ["MAX_CANDIDATES", "compete_attention", "derive_candidates"]
