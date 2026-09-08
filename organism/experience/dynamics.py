"""Bounded short-horizon dynamics for consecutive experience moments.

The trajectory is an identifier-free RAM value.  It can shape only the
functional attention workspace; it is not evidence, a risk signal, an
autonomic input, or a claim of literal feeling.
"""

from __future__ import annotations

import math
from datetime import datetime

from .models import (
    DYNAMIC_FIELDS,
    MAX_DYNAMICS_REVISION,
    ChangePattern,
    ExperienceDynamics,
    ExperienceMoment,
    FunctionalSelfModel,
)
from ..models import clamp, ensure_utc, unit

DYNAMICS_HALF_LIFE_SECONDS = 30.0
DYNAMICS_HORIZON_SECONDS = 120.0

_STRAIN_ORIENTATION = {
    "activation": 1.0,
    "valence": -1.0,
    "interaction_safety": -1.0,
    "agency": -1.0,
    "uncertainty": 1.0,
    "load": 1.0,
    "connection_readiness": -1.0,
    "surprise": 1.0,
}


def _signed(value: float) -> float:
    return clamp(value, -1.0, 1.0)


def _zero_map() -> dict[str, float]:
    return {name: 0.0 for name in DYNAMIC_FIELDS}


def _neutral_dynamics() -> ExperienceDynamics:
    return ExperienceDynamics(
        impulse=_zero_map(),
        momentum=_zero_map(),
        flux=0.0,
        volatility=0.0,
        escalation=0.0,
        settling=0.0,
        retention=0.0,
        pattern=ChangePattern.INITIAL,
        revision=0,
    )


def _point(self_model: FunctionalSelfModel, surprise: float) -> dict[str, float]:
    return {
        "activation": self_model.activation,
        "valence": (self_model.valence + 1.0) / 2.0,
        "interaction_safety": self_model.interaction_safety,
        "agency": self_model.agency,
        "uncertainty": self_model.uncertainty,
        "load": self_model.load,
        "connection_readiness": self_model.connection_readiness,
        "surprise": unit(surprise, name="dynamics.surprise"),
    }


def _aggregate(values: tuple[float, ...]) -> float:
    if not values:
        return 0.0
    return clamp(0.65 * (sum(values) / len(values)) + 0.35 * max(values))


def derive_dynamics(
    previous: ExperienceMoment | None,
    current: FunctionalSelfModel,
    surprise: float,
    generated_at: datetime,
) -> ExperienceDynamics:
    """Derive a deterministic trajectory from one trusted consecutive moment.

    A missing predecessor, non-forward clock, or gap of two minutes or more
    starts a neutral trajectory.  Callers remain responsible for ensuring that
    a predecessor came from the same isolation slot.
    """

    if previous is not None and not isinstance(previous, ExperienceMoment):
        raise ValueError("previous must be ExperienceMoment or None")
    if not isinstance(current, FunctionalSelfModel):
        raise ValueError("current must be a FunctionalSelfModel")
    current_at = ensure_utc(generated_at, name="dynamics.generated_at")
    current_surprise = unit(surprise, name="dynamics.surprise")
    if previous is None:
        return _neutral_dynamics()

    elapsed = (current_at - previous.generated_at).total_seconds()
    if not math.isfinite(elapsed) or not 0.0 < elapsed < DYNAMICS_HORIZON_SECONDS:
        return _neutral_dynamics()

    retention = 2.0 ** (-elapsed / DYNAMICS_HALF_LIFE_SECONDS)
    current_point = _point(current, current_surprise)
    previous_point = _point(previous.self_model, previous.prediction.surprise)
    impulse = {
        name: _signed(current_point[name] - previous_point[name])
        for name in DYNAMIC_FIELDS
    }
    momentum = {
        name: _signed(retention * previous.dynamics.momentum[name] + impulse[name])
        for name in DYNAMIC_FIELDS
    }

    impulse_magnitudes = tuple(abs(impulse[name]) for name in DYNAMIC_FIELDS)
    flux = _aggregate(impulse_magnitudes)
    innovations = tuple(
        clamp(abs(impulse[name] - retention * previous.dynamics.impulse[name]) / 2.0)
        for name in DYNAMIC_FIELDS
    )
    volatility = clamp(
        max(_aggregate(innovations), retention * previous.dynamics.volatility)
    )

    motion = {
        name: _signed(0.60 * impulse[name] + 0.40 * momentum[name])
        for name in DYNAMIC_FIELDS
    }
    oriented = tuple(
        _STRAIN_ORIENTATION[name] * motion[name] for name in DYNAMIC_FIELDS
    )
    escalation = _aggregate(tuple(max(0.0, value) for value in oriented))
    settling = _aggregate(tuple(max(0.0, -value) for value in oriented))
    peak_momentum = max(abs(value) for value in momentum.values())
    if flux < 0.03 and peak_momentum < 0.05:
        pattern = ChangePattern.STEADY
    elif escalation >= settling + 0.05:
        pattern = ChangePattern.INCREASING_STRAIN
    elif settling >= escalation + 0.05:
        pattern = ChangePattern.DECREASING_STRAIN
    else:
        pattern = ChangePattern.MIXED

    return ExperienceDynamics(
        impulse=impulse,
        momentum=momentum,
        flux=flux,
        volatility=volatility,
        escalation=escalation,
        settling=settling,
        retention=retention,
        pattern=pattern,
        revision=min(previous.dynamics.revision + 1, MAX_DYNAMICS_REVISION),
    )


__all__ = [
    "DYNAMICS_HALF_LIFE_SECONDS",
    "DYNAMICS_HORIZON_SECONDS",
    "derive_dynamics",
]
