"""Cross-mode recovery metaphor: settling, repair, and restored connection."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from .base import AutonomicBranch, AutonomicContext, RecruitmentStrategy
from ..models import StrategyName, clamp, unit


class DeescalateMobilization(RecruitmentStrategy):
    __slots__ = ()

    name = StrategyName.DEESCALATE_MOBILIZATION
    intention = "Keep necessary boundaries while responding to verified repair."
    tendency_delta = MappingProxyType(
        {"warmth": 0.2, "curiosity": 0.1, "brevity": -0.08}
    )

    def eligibility(self, context: AutonomicContext) -> float:
        return context.cues.repair * max(0.35, context.state.activation)


class ParasympatheticBranch(AutonomicBranch):
    """Recovery influence that overlaps all three public autonomic modes."""

    __slots__ = ()

    branch_name = "parasympathetic"
    mode = None
    strategies = (DeescalateMobilization(),)

    def tendency(self, context: AutonomicContext) -> float:
        return clamp(
            0.30 * context.cues.safety
            + 0.25 * context.cues.repair
            + 0.20 * context.cues.connection
            + 0.15 * (1.0 - context.state.activation)
            + 0.10 * context.cues.controllability
            + 0.07 * context.cues.reciprocity
            + 0.05 * context.cues.interaction_continuity
            + 0.05 * context.cues.support_availability
        )

    def guidance_adjustments(
        self,
        context: AutonomicContext,
        *,
        branch_tendency: float | None = None,
    ) -> Mapping[str, float]:
        tendency = (
            self.tendency(context)
            if branch_tendency is None
            else unit(branch_tendency, name="parasympathetic tendency")
        )
        # Restoration remains an adjustment across the three public modes. It
        # is deliberately not represented as a fourth mode weight.
        return {
            "warmth": (
                0.12 * tendency
                + 0.1 * context.cues.repair * tendency
                + 0.05 * context.motifs.pain * tendency
            )
        }


PARASYMPATHETIC = ParasympatheticBranch()

__all__ = [
    "DeescalateMobilization",
    "PARASYMPATHETIC",
    "ParasympatheticBranch",
]
