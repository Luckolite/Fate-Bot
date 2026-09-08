"""Conservation branch metaphor: reduced load and smaller next steps."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from .base import AutonomicBranch, AutonomicContext, RecruitmentStrategy
from ..models import AutonomicMode, OrganismicState, StrategyName, clamp, unit


class SlowTheExchange(RecruitmentStrategy):
    __slots__ = ()

    name = StrategyName.SLOW_THE_EXCHANGE
    intention = "Lower conversational load and take one clear step at a time."
    tendency_delta = MappingProxyType(
        {"brevity": 0.14, "curiosity": -0.08, "warmth": 0.05}
    )

    def eligibility(self, context: AutonomicContext) -> float:
        return max(context.state.activation, context.state.load)


class DorsalVagalBranch(AutonomicBranch):
    __slots__ = ()

    branch_name = "dorsal_vagal"
    mode = AutonomicMode.CONSERVATION
    strategies = (SlowTheExchange(),)

    def tendency(self, context: AutonomicContext) -> float:
        return clamp(
            0.4 * context.state.load
            + 0.25 * (1.0 - context.state.agency)
            + 0.2 * (1.0 - context.state.interaction_safety)
            + 0.15 * context.cues.energy_cost
        )

    def mode_drive(self, state: OrganismicState) -> float:
        pleasant = (state.pleasantness + 1.0) / 2.0
        return (
            1.35 * state.load
            + 0.95 * (1.0 - state.agency)
            + 0.65 * (1.0 - state.interaction_safety)
            + 0.25 * state.activation
            - 0.25 * pleasant
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
            else unit(branch_tendency, name="dorsal_vagal tendency")
        )
        return {
            "brevity": clamp(
                0.16
                + 0.42 * mode_weight
                + 0.16 * context.state.load
                + 0.2 * tendency
                + 0.18 * context.motifs.pain
                + 0.12 * context.motifs.panic
            ),
            "pacing_restraint": clamp(
                0.10
                + 0.30 * mode_weight
                + 0.18 * context.state.load
                + 0.16 * context.cues.demand
                + 0.16 * context.cues.social_exposure
                + 0.14 * (1.0 - context.cues.communication_clarity)
                + 0.12 * tendency
                + 0.20 * context.motifs.pain
                + 0.18 * context.motifs.panic
            ),
        }


DORSAL_VAGAL = DorsalVagalBranch()

__all__ = ["DORSAL_VAGAL", "DorsalVagalBranch", "SlowTheExchange"]
