"""Shared contracts for the metaphorical autonomic branches.

These classes organize response-regulation code.  They are not anatomical,
clinical, or experiential models of a biological nervous system.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Mapping

from ..models import (
    AutonomicMode,
    Cues,
    GUIDANCE_FIELDS,
    OrganismicState,
    ProtectionSummary,
    RegulatoryMotifs,
    Recruitment,
    StrategyName,
    clamp,
    unit,
)


@dataclass(frozen=True)
class AutonomicContext:
    """Content-free inputs shared by every branch."""

    cues: Cues
    state: OrganismicState
    protection: ProtectionSummary
    predicted_risk: float = 0.0
    motifs: RegulatoryMotifs = field(default_factory=RegulatoryMotifs)

    def __post_init__(self) -> None:
        if not isinstance(self.cues, Cues):
            raise ValueError("cues must be a Cues instance")
        if not isinstance(self.state, OrganismicState):
            raise ValueError("state must be an OrganismicState")
        if not isinstance(self.protection, ProtectionSummary):
            raise ValueError("protection must be a ProtectionSummary")
        if not isinstance(self.motifs, RegulatoryMotifs):
            raise ValueError("motifs must be RegulatoryMotifs")
        object.__setattr__(
            self,
            "predicted_risk",
            unit(self.predicted_risk, name="predicted_risk"),
        )


class RecruitmentStrategy(ABC):
    """One bounded, nonviolent response strategy owned by a branch."""

    __slots__ = ()

    name: StrategyName
    intention: str
    tendency_delta: Mapping[str, float]

    @abstractmethod
    def eligibility(self, context: AutonomicContext) -> float:
        raise NotImplementedError

    def recruit(
        self,
        context: AutonomicContext,
        affinity: float,
        *,
        branch_tendency: float = 1.0,
        tendency_floor: float = 0.55,
    ) -> Recruitment:
        """Return a recruitment gated by both strategy and branch signals.

        ``tendency_floor`` prevents a weak branch signal from erasing an
        independently strong safety strategy.  The branch still has a causal,
        bounded influence on the final ranking.
        """
        base_score = clamp(self.eligibility(context))
        tendency = unit(branch_tendency, name="branch_tendency")
        floor = unit(tendency_floor, name="tendency_floor")
        branch_factor = floor + (1.0 - floor) * tendency
        score = clamp(base_score * branch_factor * (0.75 + 0.5 * clamp(affinity)))
        for field_name, delta in self.tendency_delta.items():
            if field_name not in GUIDANCE_FIELDS:
                raise ValueError(f"strategy changes unsupported field {field_name!r}")
            if not math.isfinite(delta) or not -1.0 <= delta <= 1.0:
                raise ValueError("strategy deltas must be finite and between -1 and 1")
        return Recruitment(
            name=self.name,
            score=score,
            intention=self.intention,
            tendency_delta=dict(self.tendency_delta),
        )


class AutonomicBranch(ABC):
    """Interface implemented by each overlapping regulatory metaphor."""

    __slots__ = ()

    branch_name: str
    mode: AutonomicMode | None = None
    strategies: tuple[RecruitmentStrategy, ...] = ()

    @abstractmethod
    def tendency(self, context: AutonomicContext) -> float:
        """Return the bounded branch proposal consumed by the coordinator."""
        raise NotImplementedError

    def mode_drive(self, state: OrganismicState) -> float:
        """Return an unnormalized mode drive for three-mode softmax blending."""
        raise NotImplementedError(f"{self.branch_name} is not an exclusive mode")

    def guidance(
        self,
        context: AutonomicContext,
        *,
        mode_weight: float,
        branch_tendency: float | None = None,
    ) -> Mapping[str, float]:
        """Return absolute values for guidance fields owned by this branch."""
        return {}

    def guidance_adjustments(
        self,
        context: AutonomicContext,
        *,
        branch_tendency: float | None = None,
    ) -> Mapping[str, float]:
        """Return signed cross-mode adjustments applied after base guidance."""
        return {}


def apply_recruitments(
    guidance: Mapping[str, float], recruitments: tuple[Recruitment, ...]
) -> dict[str, float]:
    """Apply validated strategy deltas while preserving hard safety bounds."""
    values = dict(guidance)
    for recruitment in recruitments:
        for field_name, delta in recruitment.tendency_delta.items():
            values[field_name] = clamp(values[field_name] + delta * recruitment.score)
    values["warmth"] = max(0.2, values["warmth"])
    # An autonomic hint is downward-only and can never grant tool authority.
    values["tool_autonomy_scale"] = min(
        guidance["tool_autonomy_scale"], values["tool_autonomy_scale"]
    )
    return values
