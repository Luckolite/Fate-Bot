"""Build an identifier-free, numeric model of current software condition."""

from __future__ import annotations

from .models import FunctionalSelfModel, NeedDomain, validate_experience_inputs
from ..body.homeostasis import HomeostaticNeeds
from ..models import (
    Cues,
    OrganismicState,
    ProtectionSummary,
    RegulatoryMotifs,
    clamp,
)


def build_self_model(
    state: OrganismicState,
    needs: HomeostaticNeeds,
    protection: ProtectionSummary,
    cues: Cues,
    motifs: RegulatoryMotifs | None = None,
) -> FunctionalSelfModel:
    validate_experience_inputs(state, protection)
    if not isinstance(needs, HomeostaticNeeds):
        raise ValueError("needs must be HomeostaticNeeds")
    if not isinstance(cues, Cues):
        raise ValueError("cues must be Cues")
    selected_motifs = RegulatoryMotifs() if motifs is None else motifs
    if not isinstance(selected_motifs, RegulatoryMotifs):
        raise ValueError("motifs must be RegulatoryMotifs or None")
    protective_need = max(
        needs.stability,
        needs.recovery,
        needs.agency,
        needs.verification,
    )
    # Connection is an opportunity only when the state has capacity for it.
    # Treating low connection readiness as a large "connection need" would
    # perversely make social approach dominant under overload or threat.
    connection_opportunity = needs.connection_readiness * (1.0 - protective_need)
    need_values = {
        NeedDomain.STABILITY: needs.stability,
        NeedDomain.RECOVERY: needs.recovery,
        NeedDomain.AGENCY: needs.agency,
        NeedDomain.VERIFICATION: needs.verification,
        NeedDomain.CONNECTION: connection_opportunity,
    }
    dominant_need = max(
        NeedDomain,
        key=lambda domain: (need_values[domain], -list(NeedDomain).index(domain)),
    )
    resource_pressure = clamp(
        max(state.load, needs.recovery, needs.stability, 1.0 - state.agency)
    )
    return FunctionalSelfModel(
        activation=state.activation,
        valence=state.pleasantness,
        interaction_safety=state.interaction_safety,
        agency=state.agency,
        uncertainty=state.uncertainty,
        load=state.load,
        stability_need=needs.stability,
        recovery_need=needs.recovery,
        agency_need=needs.agency,
        verification_need=needs.verification,
        connection_readiness=needs.connection_readiness,
        caution=protection.caution,
        protection_confidence=protection.confidence,
        resource_pressure=resource_pressure,
        profile_presence=cues.profile_presence,
        self_expression=cues.self_expression,
        profile_change=cues.profile_change,
        familiarity=cues.familiarity,
        reciprocity=cues.reciprocity,
        personalization_consent=cues.personalization_consent,
        social_exposure=cues.social_exposure,
        interaction_continuity=cues.interaction_continuity,
        communication_clarity=cues.communication_clarity,
        engagement=cues.engagement,
        support_availability=cues.support_availability,
        pain=selected_motifs.pain,
        panic=selected_motifs.panic,
        riddle=selected_motifs.riddle,
        posture=protection.posture,
        dominant_need=dominant_need,
    )


__all__ = ["build_self_model"]
