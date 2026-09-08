"""Inspectable, non-conscious functional-experience simulation.

This package uses global-workspace, allostatic, and predictive-processing ideas
as engineering inspiration only.  It makes no biological, medical, or
consciousness claim and grants no authority to the language model.
"""

from .attention import MAX_CANDIDATES, compete_attention, derive_candidates
from .continuity import advance_continuity
from .cortex import (
    cortex_developer_message,
    cortex_payload,
    person_developer_message,
    person_payload,
)
from .dynamics import (
    DYNAMICS_HALF_LIFE_SECONDS,
    DYNAMICS_HORIZON_SECONDS,
    derive_dynamics,
)
from .models import (
    AttentionCandidate,
    AttendedCandidate,
    ChangePattern,
    CueExpectation,
    DYNAMIC_FIELDS,
    ExperienceDomain,
    ExperienceDynamics,
    ExperienceMoment,
    FocusTrace,
    FunctionalSelfModel,
    NeedDomain,
    PredictiveError,
    TemporalContinuity,
    WorkspaceBroadcast,
)
from .prediction import initial_expectation, prediction_error, update_expectation
from .self_model import build_self_model
from .workspace import ExperientialWorkspace, integrate_experience

__all__ = [
    "MAX_CANDIDATES",
    "DYNAMICS_HALF_LIFE_SECONDS",
    "DYNAMICS_HORIZON_SECONDS",
    "DYNAMIC_FIELDS",
    "AttentionCandidate",
    "AttendedCandidate",
    "ChangePattern",
    "CueExpectation",
    "ExperienceDomain",
    "ExperienceDynamics",
    "ExperienceMoment",
    "ExperientialWorkspace",
    "FocusTrace",
    "FunctionalSelfModel",
    "NeedDomain",
    "PredictiveError",
    "TemporalContinuity",
    "WorkspaceBroadcast",
    "advance_continuity",
    "build_self_model",
    "compete_attention",
    "cortex_developer_message",
    "cortex_payload",
    "person_developer_message",
    "person_payload",
    "derive_candidates",
    "derive_dynamics",
    "initial_expectation",
    "integrate_experience",
    "prediction_error",
    "update_expectation",
]
