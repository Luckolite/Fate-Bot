"""Orchestration for one bounded, simulated functional-experience moment."""

from __future__ import annotations

from .attention import MAX_CANDIDATES, compete_attention, derive_candidates
from .continuity import advance_continuity
from .dynamics import DYNAMICS_HORIZON_SECONDS, derive_dynamics
from .models import (
    AttentionCandidate,
    CueExpectation,
    ExperienceDomain,
    ExperienceMoment,
    TemporalContinuity,
    WorkspaceBroadcast,
)
from .prediction import initial_expectation, prediction_error, update_expectation
from .self_model import build_self_model
from ..body.homeostasis import homeostatic_needs
from ..models import Cues, FeelingPacket, unit


class ExperientialWorkspace:
    """A stateless coordinator; callers own expectation and continuity state."""

    def __init__(
        self,
        *,
        capacity: int = 3,
        minimum_salience: float = 0.12,
        continuity_gain: float = 0.18,
        expectation_learning_rate: float = 0.25,
    ) -> None:
        if type(capacity) is not int or not 1 <= capacity <= 4:
            raise ValueError("workspace capacity must be between one and four")
        self.capacity = capacity
        self.minimum_salience = unit(
            minimum_salience, name="workspace.minimum_salience"
        )
        self.continuity_gain = unit(continuity_gain, name="workspace.continuity_gain")
        self.expectation_learning_rate = unit(
            expectation_learning_rate,
            name="workspace.expectation_learning_rate",
        )

    def form_moment(
        self,
        *,
        cues: Cues,
        feeling: FeelingPacket,
        expectation: CueExpectation | None = None,
        continuity: TemporalContinuity | None = None,
        extra_candidates: tuple[AttentionCandidate, ...] = (),
        previous: ExperienceMoment | None = None,
    ) -> ExperienceMoment:
        """Form a moment from the exact cues used to produce ``feeling``.

        ``previous`` is the preferred consecutive-step contract.  The explicit
        expectation and continuity parameters remain for stateless legacy
        callers, but cannot be combined with a previous moment.
        """

        if not isinstance(cues, Cues):
            raise ValueError("cues must be a Cues instance")
        if not isinstance(feeling, FeelingPacket):
            raise ValueError("feeling must be a FeelingPacket")
        if previous is not None and not isinstance(previous, ExperienceMoment):
            raise ValueError("previous must be ExperienceMoment or None")
        if previous is not None and (expectation is not None or continuity is not None):
            raise ValueError(
                "previous cannot be combined with expectation or continuity"
            )
        if expectation is not None and not isinstance(expectation, CueExpectation):
            raise ValueError("expectation must be CueExpectation or None")
        if continuity is not None and not isinstance(continuity, TemporalContinuity):
            raise ValueError("continuity must be TemporalContinuity or None")
        if not isinstance(extra_candidates, tuple) or any(
            not isinstance(item, AttentionCandidate) for item in extra_candidates
        ):
            raise ValueError(
                "extra_candidates must be a tuple of AttentionCandidate instances"
            )
        if len(extra_candidates) > MAX_CANDIDATES - len(ExperienceDomain):
            raise ValueError("too many extra attention candidates")

        needs = homeostatic_needs(feeling.state)
        self_model = build_self_model(
            feeling.state,
            needs,
            feeling.protection,
            cues,
            feeling.motifs,
        )
        valid_previous = None
        if previous is not None:
            elapsed = (feeling.generated_at - previous.generated_at).total_seconds()
            if 0.0 < elapsed < DYNAMICS_HORIZON_SECONDS:
                valid_previous = previous
        prior_expectation = (
            valid_previous.next_expectation
            if valid_previous is not None
            else expectation or initial_expectation()
        )
        prior_continuity = (
            valid_previous.continuity if valid_previous is not None else continuity
        )
        error = prediction_error(prior_expectation, cues)
        dynamics = derive_dynamics(
            valid_previous,
            self_model,
            error.surprise,
            feeling.generated_at,
        )
        candidates = (
            derive_candidates(cues, self_model, error, dynamics) + extra_candidates
        )
        attended = compete_attention(
            candidates,
            continuity=prior_continuity,
            capacity=self.capacity,
            minimum_salience=self.minimum_salience,
            continuity_gain=self.continuity_gain,
        )
        broadcast = WorkspaceBroadcast(
            capacity=self.capacity,
            items=attended,
            overall_salience=max((item.salience for item in attended), default=0.0),
            surprise=error.surprise,
        )
        continuity_retention = (
            dynamics.retention
            if valid_previous is not None
            else 0.60
            if prior_continuity is not None
            else 0.0
        )
        next_continuity = advance_continuity(
            prior_continuity,
            attended,
            retention=continuity_retention,
        )
        posterior = update_expectation(
            prior_expectation,
            cues,
            learning_rate=self.expectation_learning_rate,
        )
        return ExperienceMoment(
            generated_at=feeling.generated_at,
            self_model=self_model,
            prediction=error,
            dynamics=dynamics,
            broadcast=broadcast,
            continuity=next_continuity,
            next_expectation=posterior,
        )


def integrate_experience(
    *,
    cues: Cues,
    feeling: FeelingPacket,
    previous: ExperienceMoment | None = None,
    capacity: int = 3,
) -> ExperienceMoment:
    """Advance one caller-isolated, consecutive step.

    ``cues`` must be the exact numeric cues used to produce ``feeling``.
    ``previous`` must be the immediately prior moment from that same caller-owned
    isolation scope; pass ``None`` after a gap or scope change.
    """

    if previous is not None and not isinstance(previous, ExperienceMoment):
        raise ValueError("previous must be ExperienceMoment or None")
    return ExperientialWorkspace(capacity=capacity).form_moment(
        cues=cues,
        feeling=feeling,
        previous=previous,
    )


__all__ = ["ExperientialWorkspace", "integrate_experience"]
