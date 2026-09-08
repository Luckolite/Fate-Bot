"""Blend overlapping autonomic branch metaphors into response guidance."""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .base import AutonomicBranch, AutonomicContext, apply_recruitments
from .dorsal_vagal import DORSAL_VAGAL
from .parasympathetic import PARASYMPATHETIC
from .sympathetic import SYMPATHETIC
from .ventral_vagal import VENTRAL_VAGAL
from ..config import OrganismConfig
from ..models import (
    AutonomicMode,
    Cues,
    GUIDANCE_FIELDS,
    OrganismicState,
    ProtectionSummary,
    RegulatoryMotifs,
    Recruitment,
    clamp,
)
from ..motifs import derive_regulatory_motifs

MODE_BRANCHES: Mapping[AutonomicMode, AutonomicBranch] = MappingProxyType(
    {
        AutonomicMode.SOCIAL_ENGAGEMENT: VENTRAL_VAGAL,
        AutonomicMode.MOBILIZATION: SYMPATHETIC,
        AutonomicMode.CONSERVATION: DORSAL_VAGAL,
    }
)
BRANCHES: tuple[AutonomicBranch, ...] = (
    VENTRAL_VAGAL,
    SYMPATHETIC,
    DORSAL_VAGAL,
    PARASYMPATHETIC,
)


@dataclass(frozen=True)
class AutonomicCalibration:
    """Small, inspectable set of offline-calibratable composition values.

    These values tune deterministic branch composition.  They are not neural
    weights and do not imply that the organism has been trained on people.
    """

    mode_tendency_gain: float = 0.9
    restoration_mode_gain: float = 0.25
    recruitment_tendency_floor: float = 0.55

    def __post_init__(self) -> None:
        for name in ("mode_tendency_gain", "restoration_mode_gain"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0.0 <= float(value) <= 2.0
            ):
                raise ValueError(f"{name} must be finite and between 0 and 2")
            object.__setattr__(self, name, float(value))
        value = self.recruitment_tendency_floor
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0.0 <= float(value) <= 1.0
        ):
            raise ValueError(
                "recruitment_tendency_floor must be finite and between 0 and 1"
            )
        object.__setattr__(self, "recruitment_tendency_floor", float(value))


DEFAULT_AUTONOMIC_CALIBRATION = AutonomicCalibration()


def branch_tendencies(context: AutonomicContext) -> dict[str, float]:
    """Compute the branch proposals used by live autonomic synthesis."""
    return {branch.branch_name: clamp(branch.tendency(context)) for branch in BRANCHES}


def _validated_tendencies(values: Mapping[str, float]) -> dict[str, float]:
    expected = {branch.branch_name for branch in BRANCHES}
    if set(values) != expected:
        missing = ", ".join(sorted(expected - set(values)))
        extra = ", ".join(sorted(set(values) - expected))
        details = "; ".join(
            part
            for part in (
                f"missing: {missing}" if missing else "",
                f"unexpected: {extra}" if extra else "",
            )
            if part
        )
        raise ValueError(f"branch tendencies do not match the hierarchy ({details})")
    result: dict[str, float] = {}
    for name, value in values.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0.0 <= float(value) <= 1.0
        ):
            raise ValueError(f"branch tendency {name!r} must be between 0 and 1")
        result[name] = float(value)
    return result


def mode_weights(
    state: OrganismicState,
    config: OrganismConfig,
    *,
    tendencies: Mapping[str, float] | None = None,
    calibration: AutonomicCalibration = DEFAULT_AUTONOMIC_CALIBRATION,
) -> dict[AutonomicMode, float]:
    """Blend three public modes from state plus causal branch proposals."""
    if not isinstance(calibration, AutonomicCalibration):
        raise ValueError("calibration must be an AutonomicCalibration")
    proposals = (
        {branch.branch_name: 0.5 for branch in BRANCHES}
        if tendencies is None
        else _validated_tendencies(tendencies)
    )
    logits = {
        mode: branch.mode_drive(state)
        + calibration.mode_tendency_gain * (proposals[branch.branch_name] - 0.5)
        for mode, branch in MODE_BRANCHES.items()
    }
    # Parasympathetic is restoration across modes, not a fourth competing
    # mode.  A verified above-neutral restoration proposal gently favors
    # social engagement and relieves mobilization/conservation logits.
    restoration = max(0.0, proposals[PARASYMPATHETIC.branch_name] - 0.5)
    restoration_shift = calibration.restoration_mode_gain * restoration
    logits[AutonomicMode.SOCIAL_ENGAGEMENT] += restoration_shift
    logits[AutonomicMode.MOBILIZATION] -= 0.6 * restoration_shift
    logits[AutonomicMode.CONSERVATION] -= 0.4 * restoration_shift
    maximum = max(logits.values())
    exponentials = {
        mode: math.exp((value - maximum) / config.mode_temperature)
        for mode, value in logits.items()
    }
    total = sum(exponentials.values())
    weights = {mode: value / total for mode, value in exponentials.items()}
    largest = max(weights, key=weights.get)
    weights[largest] += 1.0 - sum(weights.values())
    return weights


def dominant_mode(
    weights: Mapping[AutonomicMode, float],
    previous: AutonomicMode | None,
    config: OrganismConfig,
) -> AutonomicMode:
    challenger = max(weights, key=weights.get)
    if previous is None or previous not in weights or challenger is previous:
        return challenger
    if weights[challenger] < weights[previous] + config.mode_hysteresis:
        return previous
    return challenger


def response_guidance(
    context: AutonomicContext,
    modes: Mapping[AutonomicMode, float],
    *,
    tendencies: Mapping[str, float] | None = None,
) -> dict[str, float]:
    proposals = _validated_tendencies(
        branch_tendencies(context) if tendencies is None else tendencies
    )
    values: dict[str, float] = {}
    for mode, branch in MODE_BRANCHES.items():
        proposal = dict(
            branch.guidance(
                context,
                mode_weight=modes[mode],
                branch_tendency=proposals[branch.branch_name],
            )
        )
        overlap = set(values) & set(proposal)
        if overlap:
            fields = ", ".join(sorted(overlap))
            raise ValueError(f"autonomic guidance ownership overlaps: {fields}")
        values.update(proposal)
    for branch in BRANCHES:
        adjustments = branch.guidance_adjustments(
            context,
            branch_tendency=proposals[branch.branch_name],
        )
        for field_name, delta in adjustments.items():
            if field_name not in GUIDANCE_FIELDS:
                raise ValueError(
                    f"autonomic adjustment targets unsupported field {field_name!r}"
                )
            values[field_name] = clamp(values.get(field_name, 0.0) + delta)
    if set(values) != GUIDANCE_FIELDS:
        missing = ", ".join(sorted(GUIDANCE_FIELDS - set(values)))
        raise ValueError(
            f"autonomic branches did not provide guidance fields: {missing}"
        )
    return values


def recruit_strategies(
    context: AutonomicContext,
    affinities: Mapping[str, float],
    *,
    limit: int = 3,
    tendencies: Mapping[str, float] | None = None,
    calibration: AutonomicCalibration = DEFAULT_AUTONOMIC_CALIBRATION,
) -> tuple[Recruitment, ...]:
    if type(limit) is not int or limit < 0:
        raise ValueError("limit must be a non-negative integer")
    if not isinstance(calibration, AutonomicCalibration):
        raise ValueError("calibration must be an AutonomicCalibration")
    proposals = _validated_tendencies(
        branch_tendencies(context) if tendencies is None else tendencies
    )
    owned_strategies = tuple(
        (branch, strategy) for branch in BRANCHES for strategy in branch.strategies
    )
    names = [strategy.name for _, strategy in owned_strategies]
    if len(names) != len(set(names)):
        raise ValueError("each recruitment strategy must have one branch owner")
    candidates = [
        strategy.recruit(
            context,
            affinities.get(strategy.name.value, 0.5),
            branch_tendency=proposals[branch.branch_name],
            tendency_floor=calibration.recruitment_tendency_floor,
        )
        for branch, strategy in owned_strategies
    ]
    candidates = [candidate for candidate in candidates if candidate.score >= 0.33]
    candidates.sort(key=lambda item: (-item.score, item.name.value))
    return tuple(candidates[:limit])


def synthesize_autonomic_response(
    *,
    state: OrganismicState,
    cues: Cues,
    protection: ProtectionSummary,
    predicted_risk: float,
    strategy_affinities: Mapping[str, float],
    previous_mode: AutonomicMode | None,
    config: OrganismConfig,
    calibration: AutonomicCalibration = DEFAULT_AUTONOMIC_CALIBRATION,
    motifs: RegulatoryMotifs | None = None,
) -> tuple[
    dict[AutonomicMode, float],
    AutonomicMode,
    dict[str, float],
    tuple[Recruitment, ...],
]:
    selected_motifs = (
        derive_regulatory_motifs(cues, state, predicted_risk)
        if motifs is None
        else motifs
    )
    if not isinstance(selected_motifs, RegulatoryMotifs):
        raise ValueError("motifs must be RegulatoryMotifs or None")
    context = AutonomicContext(
        cues=cues,
        state=state,
        protection=protection,
        predicted_risk=predicted_risk,
        motifs=selected_motifs,
    )
    tendencies = branch_tendencies(context)
    modes = mode_weights(
        state,
        config,
        tendencies=tendencies,
        calibration=calibration,
    )
    dominant = dominant_mode(modes, previous_mode, config)
    base_guidance = response_guidance(context, modes, tendencies=tendencies)
    recruitments = recruit_strategies(
        context,
        strategy_affinities,
        tendencies=tendencies,
        calibration=calibration,
    )
    guidance = apply_recruitments(base_guidance, recruitments)
    return modes, dominant, guidance, recruitments


__all__ = [
    "AutonomicCalibration",
    "BRANCHES",
    "DEFAULT_AUTONOMIC_CALIBRATION",
    "MODE_BRANCHES",
    "branch_tendencies",
    "dominant_mode",
    "mode_weights",
    "recruit_strategies",
    "response_guidance",
    "synthesize_autonomic_response",
]
