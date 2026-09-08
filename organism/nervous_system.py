"""Cue-weighted dimensional state transition."""

from __future__ import annotations

from datetime import datetime
from typing import Mapping

from .config import OrganismConfig
from .healing import recover_state
from .models import Cues, OrganismicState, clamp
from .weights import predict_risk

_SAFETY_ORIENTATION = {
    "activation": -1.0,
    "pleasantness": 1.0,
    "interaction_safety": 1.0,
    "agency": 1.0,
    "uncertainty": -1.0,
    "load": -1.0,
}


def _event_gain(
    field_name: str,
    current: float,
    target: float,
    cues: Cues,
    config: OrganismConfig,
) -> float:
    """Recruit protection quickly and release it through sustained support.

    A target moving in the field's safer direction uses a smaller gain.  This
    preserves a recent defensive state across one reassuring frame while
    repeated safety, connection, controllability, and repair can progressively
    settle it.  Moves in the defensive direction retain the full event gain.
    """
    safety_direction = _SAFETY_ORIENTATION[field_name]
    is_release = safety_direction * (target - current) > 0.0
    if not is_release:
        return config.event_gain
    regulated_support = (
        0.35 * cues.safety
        + 0.25 * cues.connection
        + 0.2 * cues.controllability
        + 0.2 * cues.repair
    )
    release_ratio = clamp(
        config.defensive_release_ratio
        + config.regulated_release_boost * regulated_support
    )
    return config.event_gain * release_ratio


def transition_state(
    previous: OrganismicState,
    cues: Cues,
    cue_weights: Mapping[str, float],
    capacities: Mapping[str, float],
    historical_bias: float,
    config: OrganismConfig,
    now: datetime,
) -> tuple[OrganismicState, float]:
    recovered = recover_state(previous, config, now)
    risk = predict_risk(cues, cue_weights, config)
    baseline = config.baseline_state
    targets = {
        "activation": clamp(
            baseline["activation"]
            + 0.65 * risk
            + 0.2 * cues.demand
            + 0.15 * cues.novelty
            + 0.08 * cues.profile_change
            + 0.10 * cues.social_exposure
            + 0.06 * cues.engagement
            - 0.25 * (cues.safety - 0.5)
            - 0.1 * cues.repair
            + historical_bias
        ),
        "pleasantness": clamp(
            baseline["pleasantness"]
            + 0.3 * (cues.safety - 0.5)
            + 0.2 * (cues.connection - 0.5)
            + 0.15 * cues.repair
            + 0.12 * cues.reciprocity
            + 0.08 * cues.engagement
            + 0.06 * cues.interaction_continuity
            + 0.04 * cues.support_availability
            - 0.55 * risk
            - 0.2 * cues.boundary_pressure
            - 0.08 * cues.social_exposure,
            -1.0,
            1.0,
        ),
        "interaction_safety": clamp(
            baseline["interaction_safety"]
            + 0.45 * (cues.safety - 0.5)
            + 0.2 * (cues.connection - 0.5)
            + 0.15 * cues.repair
            + 0.08 * cues.reciprocity
            + 0.10 * (cues.communication_clarity - 0.5)
            + 0.05 * cues.support_availability
            - 0.55 * risk
            - 0.12 * cues.social_exposure
            - historical_bias
        ),
        "agency": clamp(
            baseline["agency"]
            + 0.55 * (cues.controllability - 0.7)
            + 0.15 * (capacities["boundary"] - 0.5)
            + 0.10 * (cues.communication_clarity - 0.5)
            + 0.08 * cues.support_availability
            - 0.25 * risk * (1.0 - cues.controllability)
            - 0.12 * cues.social_exposure
            - 0.3 * historical_bias
        ),
        "uncertainty": clamp(
            baseline["uncertainty"]
            + 0.55 * (cues.uncertainty - 0.25)
            + 0.15 * cues.novelty
            + 0.08 * cues.profile_change
            + 0.25 * risk
            - 0.2 * cues.repair
            - 0.15 * (cues.communication_clarity - 0.5)
            - 0.10 * cues.interaction_continuity
            - 0.15 * (capacities["uncertainty_tolerance"] - 0.5)
            + 0.5 * historical_bias
        ),
        "load": clamp(
            baseline["load"]
            + 0.35 * cues.demand
            + 0.25 * cues.energy_cost
            + 0.25 * risk
            + 0.14 * cues.social_exposure
            + 0.08 * max(0.0, 0.5 - cues.communication_clarity)
            - 0.15 * cues.repair
            - 0.04 * cues.support_availability
            - 0.1 * (cues.safety - 0.5)
            + 0.6 * historical_bias
        ),
    }
    values = {}
    for name, target in targets.items():
        current = getattr(recovered, name)
        gain = _event_gain(name, current, target, cues, config)
        values[name] = current + gain * (target - current)
    state = OrganismicState(
        **values,
        updated_at=now,
        revision=recovered.revision + 1,
    )
    return state, risk
