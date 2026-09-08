"""Derive nonliteral pain, panic, and riddle regulation motifs.

The outputs summarize Fate's present software condition.  They do not infer a
person's physical or mental state, create durable evidence, or grant authority.
"""

from __future__ import annotations

from .models import Cues, OrganismicState, RegulatoryMotifs, clamp, unit


def _noisy_or(*values: float) -> float:
    """Combine bounded contributors without treating them as additive facts."""

    remaining = 1.0
    for value in values:
        remaining *= 1.0 - clamp(value)
    return clamp(1.0 - remaining)


def derive_regulatory_motifs(
    cues: Cues,
    state: OrganismicState,
    predicted_risk: float,
) -> RegulatoryMotifs:
    """Return three independent, bounded appraisals for the current step.

    Pain compresses current integrity and resource strain. Panic distinguishes
    urgent threat under diminished control from ordinary mobilization. Riddle
    treats uncertainty as an invitation to investigate only while sufficient
    safety and agency remain.  Consequently the same ambiguity can produce a
    strong riddle in a safe context and panic in a dangerous, low-control one.
    """

    if not isinstance(cues, Cues):
        raise ValueError("cues must be a Cues instance")
    if not isinstance(state, OrganismicState):
        raise ValueError("state must be an OrganismicState")
    risk = unit(predicted_risk, name="predicted_risk")

    regulated_support = clamp(
        0.35 * cues.safety
        + 0.20 * cues.connection
        + 0.25 * cues.controllability
        + 0.12 * cues.repair
        + 0.08 * cues.support_availability
    )
    threat_impact = cues.threat * max(
        cues.boundary_pressure,
        cues.energy_cost,
        cues.demand,
    )
    acute_strain = _noisy_or(
        0.35 * threat_impact,
        0.60 * cues.boundary_pressure,
        0.65 * cues.energy_cost,
        0.35 * cues.demand,
    )
    negative_valence = max(0.0, -state.pleasantness)
    embodied_strain = clamp(
        0.42 * state.load
        + 0.25 * state.activation
        + 0.20 * (1.0 - state.interaction_safety)
        + 0.13 * negative_valence
    )
    pain = clamp(
        0.62 * acute_strain
        + 0.38 * embodied_strain
        - 0.18 * regulated_support
    )

    risk_alarm = clamp((risk - 0.30) / 0.70)
    alarm = _noisy_or(
        0.80 * cues.threat,
        0.65 * cues.boundary_pressure,
        0.55 * risk_alarm,
    )
    control_gap = max(1.0 - cues.controllability, 1.0 - state.agency)
    arousal_evidence = _noisy_or(
        0.70 * state.activation,
        0.35 * cues.uncertainty,
        0.25 * cues.novelty,
    )
    stabilizing_capacity = clamp(
        0.30 * cues.safety
        + 0.20 * cues.repair
        + 0.15 * cues.support_availability
        + 0.35 * state.agency
    )
    panic_drive = (alarm * control_gap * arousal_evidence) ** (1.0 / 3.0)
    panic = clamp(panic_drive - 0.12 * stabilizing_capacity)

    raw_gap = _noisy_or(
        0.65 * cues.uncertainty,
        0.75 * cues.novelty,
        0.55 * (1.0 - cues.communication_clarity),
    )
    information_gap = clamp((raw_gap - 0.15) / 0.85)
    safety_gate = clamp(
        0.60 * cues.safety + 0.40 * state.interaction_safety
    )
    agency_gate = clamp(
        0.55 * cues.controllability + 0.45 * state.agency
    )
    inquiry_capacity = clamp(
        (safety_gate * agency_gate) ** 0.5
        * (0.75 + 0.25 * (1.0 - state.load))
        * (0.70 + 0.30 * cues.engagement)
    )
    danger_gate = clamp(
        1.0 - 0.75 * panic - 0.35 * pain - 0.25 * cues.threat
    )
    riddle = clamp(information_gap * inquiry_capacity * danger_gate)

    return RegulatoryMotifs(pain=pain, panic=panic, riddle=riddle)


__all__ = ["derive_regulatory_motifs"]
