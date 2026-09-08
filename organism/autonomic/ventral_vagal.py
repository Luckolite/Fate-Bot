"""Social-engagement branch metaphor: connection, orientation, and curiosity."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from .base import AutonomicBranch, AutonomicContext, RecruitmentStrategy
from ..models import AutonomicMode, OrganismicState, StrategyName, clamp, unit


class OrientAndVerify(RecruitmentStrategy):
    __slots__ = ()

    name = StrategyName.ORIENT_AND_VERIFY
    intention = "Resolve uncertainty before making assumptions."
    tendency_delta = MappingProxyType({"curiosity": 0.16, "verification": 0.25})

    def eligibility(self, context: AutonomicContext) -> float:
        return max(
            context.cues.uncertainty,
            0.8 * context.cues.novelty,
            0.65 * context.cues.profile_change,
        )


class VentralVagalBranch(AutonomicBranch):
    __slots__ = ()

    branch_name = "ventral_vagal"
    mode = AutonomicMode.SOCIAL_ENGAGEMENT
    strategies = (OrientAndVerify(),)

    def tendency(self, context: AutonomicContext) -> float:
        return clamp(
            0.35 * context.cues.safety
            + 0.25 * context.cues.connection
            + 0.15 * context.state.agency
            + 0.15 * (1.0 - context.state.activation)
            + 0.10 * context.cues.repair
            + 0.08 * context.cues.reciprocity
            + 0.06 * context.cues.familiarity
            + 0.06 * context.cues.engagement
            + 0.05 * context.cues.interaction_continuity
            - 0.2 * context.cues.threat
            - 0.08 * context.cues.social_exposure
        )

    def mode_drive(self, state: OrganismicState) -> float:
        pleasant = (state.pleasantness + 1.0) / 2.0
        return (
            1.35 * state.interaction_safety
            + 0.65 * (1.0 - state.activation)
            + 0.45 * state.agency
            + 0.35 * pleasant
            - 0.65 * state.load
        )

    def guidance(
        self,
        context: AutonomicContext,
        *,
        mode_weight: float,
        branch_tendency: float | None = None,
    ) -> Mapping[str, float]:
        tendency = (
            self.tendency(context)
            if branch_tendency is None
            else unit(branch_tendency, name="ventral_vagal tendency")
        )
        return {
            "warmth": clamp(
                0.2
                + 0.38 * mode_weight
                + 0.15 * tendency
                + 0.10 * context.cues.reciprocity
                + 0.08 * context.cues.engagement
            ),
            "curiosity": clamp(
                0.15
                + 0.35 * mode_weight
                + 0.2 * tendency
                + 0.3 * context.cues.uncertainty
                + 0.30 * context.motifs.riddle
                - 0.25 * context.cues.threat
                - 0.25 * context.motifs.panic
                - 0.10 * context.motifs.pain
            ),
            "attunement": clamp(
                0.15
                + 0.18 * context.cues.connection
                + 0.18 * context.cues.reciprocity
                + 0.16 * context.cues.engagement
                + 0.14 * context.cues.interaction_continuity
                + 0.12 * tendency
                - 0.18 * context.protection.caution
            ),
            "personalization": clamp(
                context.cues.personalization_consent
                * (
                    0.12
                    + 0.28 * context.cues.profile_presence
                    + 0.18 * context.cues.self_expression
                    + 0.16 * context.cues.familiarity
                    + 0.12 * context.cues.interaction_continuity
                )
                * (1.0 - 0.55 * context.protection.caution)
                * (1.0 - 0.30 * context.cues.social_exposure)
                * (1.0 - 0.40 * context.motifs.panic)
            ),
        }


VENTRAL_VAGAL = VentralVagalBranch()

__all__ = ["OrientAndVerify", "VENTRAL_VAGAL", "VentralVagalBranch"]
