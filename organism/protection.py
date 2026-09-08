"""Translate current cues and decaying evidence into a non-punitive posture."""

from __future__ import annotations

from typing import Mapping

from .config import OrganismConfig
from .models import Cues, ProtectionSummary, ProtectivePosture, clamp


def protective_summary(
    cues: Cues,
    predicted_risk: float,
    profile: Mapping[str, float | int | bool],
    config: OrganismConfig,
) -> ProtectionSummary:
    historical = float(profile["activation"])
    current = max(predicted_risk, cues.threat, 0.8 * cues.boundary_pressure)
    caution = clamp(max(0.78 * current + 0.22 * historical, 0.65 * historical))
    episodes = int(profile["adverse_episodes"])
    adverse_mass = float(profile["adverse_mass"])
    severe_recent = bool(profile["severe_recent"])
    current_confidence = max(cues.threat, cues.boundary_pressure) * (
        1.0 - 0.5 * cues.uncertainty
    )
    acute_guard = cues.threat >= 0.9 and current_confidence >= 0.65

    if (
        acute_guard
        or severe_recent
        or (
            historical >= 0.6
            and adverse_mass >= 2.0
            and episodes >= config.minimum_guarded_episodes
        )
    ):
        posture = ProtectivePosture.GUARDED
    elif caution >= 0.33 or cues.boundary_pressure >= 0.55:
        posture = ProtectivePosture.CAUTIOUS
    else:
        posture = ProtectivePosture.OPEN
    if posture is ProtectivePosture.GUARDED:
        caution = max(caution, 0.6)
    elif posture is ProtectivePosture.CAUTIOUS:
        caution = max(caution, 0.33)

    reasons = []
    if cues.threat >= 0.55:
        reasons.append("current_threat_signal")
    if cues.boundary_pressure >= 0.55:
        reasons.append("current_boundary_pressure")
    if cues.uncertainty >= 0.65:
        reasons.append("high_uncertainty")
    if historical >= 0.2:
        reasons.append("decaying_interaction_history")
    if cues.repair >= 0.55:
        reasons.append("current_repair_signal")
    confidence = clamp(max(float(profile["confidence"]), current_confidence))
    uncertainty = clamp(max(cues.uncertainty, 1.0 - confidence))
    return ProtectionSummary(
        posture=posture,
        caution=caution,
        confidence=confidence,
        uncertainty=uncertainty,
        reason_codes=tuple(reasons),
    )


def historical_state_bias(
    profile: Mapping[str, float | int | bool], config: OrganismConfig
) -> float:
    return min(
        config.historical_influence_cap,
        float(profile["activation"]) * config.historical_influence_cap,
    )
