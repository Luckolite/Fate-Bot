"""Inspectable software drives derived from current organismic state."""

from __future__ import annotations

from dataclasses import dataclass

from ..models import OrganismicState, clamp, unit


@dataclass(frozen=True)
class HomeostaticNeeds:
    stability: float
    recovery: float
    agency: float
    verification: float
    connection_readiness: float

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            object.__setattr__(
                self,
                name,
                unit(getattr(self, name), name=f"homeostasis.{name}"),
            )


def homeostatic_needs(state: OrganismicState) -> HomeostaticNeeds:
    if not isinstance(state, OrganismicState):
        raise ValueError("state must be an OrganismicState")
    stability = clamp(
        0.35 * state.activation
        + 0.30 * state.load
        + 0.25 * state.uncertainty
        + 0.10 * (1.0 - state.agency)
    )
    recovery = clamp(
        0.50 * state.load + 0.30 * state.activation + 0.20 * (1.0 - state.agency)
    )
    agency = clamp(
        0.55 * (1.0 - state.agency) + 0.25 * state.uncertainty + 0.20 * state.load
    )
    verification = clamp(
        0.60 * state.uncertainty + 0.40 * (1.0 - state.interaction_safety)
    )
    connection_readiness = clamp(
        state.interaction_safety
        * state.agency
        * (1.0 - 0.65 * state.load)
        * (1.0 - 0.45 * state.activation)
    )
    return HomeostaticNeeds(
        stability=stability,
        recovery=recovery,
        agency=agency,
        verification=verification,
        connection_readiness=connection_readiness,
    )


__all__ = ["HomeostaticNeeds", "homeostatic_needs"]
