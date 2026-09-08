"""Computational recovery toward baseline; not a therapeutic intervention."""

from __future__ import annotations

from datetime import datetime

from .config import OrganismConfig
from .models import OrganismicState, unit

STATE_FIELDS = (
    "activation",
    "pleasantness",
    "interaction_safety",
    "agency",
    "uncertainty",
    "load",
)


def decay_factor(elapsed_hours: float, half_life_hours: float) -> float:
    if elapsed_hours <= 0:
        return 1.0
    return 2.0 ** (-elapsed_hours / half_life_hours)


def recover_state(
    state: OrganismicState,
    config: OrganismConfig,
    now: datetime,
    *,
    additional_hours: float = 0.0,
    support: float = 0.5,
) -> OrganismicState:
    """Move every state dimension monotonically toward its configured baseline."""
    if isinstance(additional_hours, bool) or additional_hours < 0:
        raise ValueError("additional_hours must be non-negative")
    support = unit(support, name="support")
    elapsed = max(0.0, (now - state.updated_at).total_seconds() / 3600.0)
    elapsed += float(additional_hours) * (0.5 + support)
    factor = decay_factor(elapsed, config.recovery_half_life_hours)
    values = {}
    for field_name in STATE_FIELDS:
        baseline = config.baseline_state[field_name]
        values[field_name] = baseline + (getattr(state, field_name) - baseline) * factor
    return OrganismicState(
        **values,
        updated_at=now,
        revision=state.revision + (1 if elapsed > 0 else 0),
    )


def evidence_decay(age_days: float, severity: float) -> float:
    """Apply shorter memory to minor evidence and longer memory to severe evidence."""
    severity = unit(severity, name="severity")
    if severity < 0.35:
        half_life_days = 7.0
    elif severity < 0.8:
        half_life_days = 30.0
    else:
        half_life_days = 90.0
    return decay_factor(max(0.0, age_days) * 24.0, half_life_days * 24.0)


def distance_from_baseline(state: OrganismicState, config: OrganismConfig) -> float:
    return sum(
        abs(getattr(state, name) - config.baseline_state[name]) for name in STATE_FIELDS
    )
