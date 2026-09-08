"""Bounded cue-weight prediction and calibration."""

from __future__ import annotations

from typing import Mapping

from .config import OrganismConfig
from .models import Cues, clamp

FEATURE_NAMES = (
    "threat",
    "boundary_pressure",
    "uncertainty",
    "novelty",
    "demand",
    "low_controllability",
    "energy_cost",
)


def cue_features(cues: Cues) -> dict[str, float]:
    return {
        "threat": cues.threat,
        "boundary_pressure": cues.boundary_pressure,
        "uncertainty": cues.uncertainty,
        "novelty": cues.novelty,
        "demand": cues.demand,
        "low_controllability": 1.0 - cues.controllability,
        "energy_cost": cues.energy_cost,
    }


def baseline_cue_weights() -> dict[str, float]:
    return {name: 1.0 for name in FEATURE_NAMES}


def predict_risk(
    cues: Cues,
    weights: Mapping[str, float],
    config: OrganismConfig,
) -> float:
    features = cue_features(cues)
    numerator = sum(
        config.cue_coefficients[name] * weights[name] * features[name]
        for name in FEATURE_NAMES
    )
    denominator = sum(
        config.cue_coefficients[name] * weights[name] for name in FEATURE_NAMES
    )
    return clamp(numerator / denominator if denominator else 0.0)


def update_cue_weights(
    current: Mapping[str, float],
    features: Mapping[str, float],
    *,
    target: float,
    strength: float,
    config: OrganismConfig,
) -> dict[str, float]:
    values = dict(current)
    synthetic = Cues(
        threat=features["threat"],
        boundary_pressure=features["boundary_pressure"],
        uncertainty=features["uncertainty"],
        novelty=features["novelty"],
        demand=features["demand"],
        controllability=1.0 - features["low_controllability"],
        energy_cost=features["energy_cost"],
    )
    error = clamp(target) - predict_risk(synthetic, values, config)
    for name in FEATURE_NAMES:
        raw_delta = config.cue_learning_rate * error * features[name] * strength
        delta = max(
            -config.cue_event_delta_cap,
            min(config.cue_event_delta_cap, raw_delta),
        )
        values[name] = max(
            config.cue_weight_min,
            min(config.cue_weight_max, values[name] + delta),
        )
    return values
