"""Trusted actuator consequences that can re-enter the body's sensory loop."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .integration import SensorFrame
from .senses import ActionProprioception, ExternalSenses, OperationalInteroception
from ..models import Capability, ensure_utc, unit


class ActionKind(str, Enum):
    SPEAK = "speak"
    WAIT = "wait"
    ASK = "ask"
    VERIFY = "verify"
    HOLD_BOUNDARY = "hold_boundary"
    REDUCE_EXPOSURE = "reduce_exposure"
    REQUEST_HUMAN_REVIEW = "request_human_review"
    USE_AUTHORIZED_TOOL = "use_authorized_tool"


@dataclass(frozen=True)
class ActuatorOutcome:
    """A deterministic application report about an attempted action.

    This is an immediate sensory consequence, not durable learning authority.
    Applications still need signed reviewed feedback before calling ``learn``.
    """

    kind: ActionKind
    success: float
    cost: float = 0.0
    reversibility: float = 1.0
    boundary_respected: float = 0.5
    repair: float = 0.0
    error_pressure: float = 0.0
    occurred_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ActionKind):
            try:
                object.__setattr__(self, "kind", ActionKind(self.kind))
            except (TypeError, ValueError) as error:
                raise ValueError("action kind is not supported") from error
        for name in (
            "success",
            "cost",
            "reversibility",
            "boundary_respected",
            "repair",
            "error_pressure",
        ):
            object.__setattr__(
                self, name, unit(getattr(self, name), name=f"outcome.{name}")
            )
        if self.occurred_at is not None:
            object.__setattr__(
                self,
                "occurred_at",
                ensure_utc(self.occurred_at, name="occurred_at"),
            )

    def sensor_frame(
        self,
        *,
        event_id: str,
        scope: str,
        actor_ref: str | None = None,
        capability: Capability = Capability.CONVERSATION,
    ) -> SensorFrame:
        failure = 1.0 - self.success
        is_tool = self.kind is ActionKind.USE_AUTHORIZED_TOOL
        return SensorFrame(
            event_id=event_id,
            scope=scope,
            actor_ref=actor_ref,
            capability=capability,
            occurred_at=self.occurred_at,
            external=ExternalSenses(
                safety=0.35 + 0.35 * self.success + 0.20 * self.boundary_respected,
                connection=0.35 + 0.35 * self.repair,
                threat=0.0,
                boundary_pressure=failure * (1.0 - self.boundary_respected),
                uncertainty=failure,
                demand=self.cost,
                controllability=self.success,
                repair=self.repair,
            ),
            interoception=OperationalInteroception(
                error_pressure=max(failure, self.error_pressure),
                energy_reserve=1.0 - self.cost,
            ),
            proprioception=ActionProprioception(
                commitment=1.0,
                tool_activity=1.0 if is_tool else 0.0,
                action_cost=self.cost,
                uncertainty=failure,
                controllability=self.success,
                reversibility=self.reversibility,
            ),
        )


__all__ = ["ActionKind", "ActuatorOutcome"]
