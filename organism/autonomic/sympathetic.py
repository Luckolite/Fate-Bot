"""Mobilization branch metaphor: boundaries, verification, and exposure limits."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from .base import AutonomicBranch, AutonomicContext, RecruitmentStrategy
from ..models import AutonomicMode, OrganismicState, StrategyName, clamp, unit


class HoldBoundary(RecruitmentStrategy):
    __slots__ = ()

    name = StrategyName.HOLD_BOUNDARY
    intention = "State a clear, non-hostile limit and preserve safety rules."
    tendency_delta = MappingProxyType(
        {
            "firmness": 0.3,
            "disclosure_restraint": 0.2,
            "warmth": -0.04,
        }
    )

    def eligibility(self, context: AutonomicContext) -> float:
        return max(context.cues.boundary_pressure, 0.75 * context.cues.threat)


class ReduceExposure(RecruitmentStrategy):
    __slots__ = ()

    name = StrategyName.REDUCE_EXPOSURE
    intention = "Limit unnecessary detail and sensitive-action autonomy."
    tendency_delta = MappingProxyType(
        {
            "brevity": 0.22,
            "disclosure_restraint": 0.28,
            "tool_autonomy_scale": -0.18,
        }
    )

    def eligibility(self, context: AutonomicContext) -> float:
        return max(context.protection.caution, 0.75 * context.cues.threat)


class RequestHumanReview(RecruitmentStrategy):
    __slots__ = ()

    name = StrategyName.REQUEST_HUMAN_REVIEW
    intention = "Surface a review hint without reporting or punishing automatically."
    tendency_delta = MappingProxyType(
        {
            "human_review_hint": 0.5,
            "tool_autonomy_scale": -0.25,
        }
    )

    def eligibility(self, context: AutonomicContext) -> float:
        return max(
            0.0,
            1.2 * context.cues.threat - 0.35,
            1.1 * context.protection.caution - 0.35,
        )


class SympatheticBranch(AutonomicBranch):
    __slots__ = ()

    branch_name = "sympathetic"
    mode = AutonomicMode.MOBILIZATION
    strategies = (HoldBoundary(), ReduceExposure(), RequestHumanReview())

    def tendency(self, context: AutonomicContext) -> float:
        return clamp(
            0.35 * context.state.activation
            + 0.25 * context.cues.threat
            + 0.2 * context.cues.boundary_pressure
            + 0.1 * context.cues.uncertainty
            + 0.1 * context.state.agency
            - 0.15 * context.cues.safety
        )

    def mode_drive(self, state: OrganismicState) -> float:
        return (
            1.25 * state.activation
            + 0.65 * state.agency
            + 0.8 * (1.0 - state.interaction_safety)
            + 0.25 * state.uncertainty
            - 0.25 * state.load
        )

    def guidance(
        self,
        context: AutonomicContext,
        *,
        mode_weight: float,
        branch_tendency: float | None = None,
    ) -> Mapping[str, float]:
        cues = context.cues
        protection = context.protection
        tendency = (
            self.tendency(context)
            if branch_tendency is None
            else unit(branch_tendency, name="sympathetic tendency")
        )
        return {
            "firmness": clamp(
                0.24
                + 0.3 * mode_weight
                + 0.3 * cues.boundary_pressure
                + 0.18 * tendency
            ),
            "verification": clamp(
                0.18
                + 0.45 * cues.uncertainty
                + 0.15 * (1.0 - cues.communication_clarity)
                + 0.22 * mode_weight
                + 0.2 * protection.caution
                + 0.1 * tendency
                + 0.18 * context.motifs.panic
                + 0.12 * context.motifs.riddle
            ),
            "disclosure_restraint": clamp(
                0.18
                + 0.4 * protection.caution
                + 0.28 * context.predicted_risk
                + 0.22 * cues.social_exposure
                + 0.12 * tendency
            ),
            "human_review_hint": clamp(
                0.05
                + 1.2 * max(0.0, protection.caution - 0.5)
                + 0.35 * max(0.0, cues.threat - 0.75)
                + 0.08 * tendency
            ),
            "tool_autonomy_scale": clamp(
                1.0
                - 0.7 * protection.caution
                - 0.25 * cues.uncertainty
                - 0.2 * cues.threat
                - 0.15 * tendency
                - 0.20 * context.motifs.panic
                - 0.10 * context.motifs.pain
            ),
        }


SYMPATHETIC = SympatheticBranch()

__all__ = [
    "HoldBoundary",
    "ReduceExposure",
    "RequestHumanReview",
    "SYMPATHETIC",
    "SympatheticBranch",
]
