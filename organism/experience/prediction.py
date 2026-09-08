"""Bounded predictive-cue expectations and numeric prediction error."""

from __future__ import annotations

from .models import CUE_FIELDS, CueExpectation, PredictiveError
from ..models import Cues, clamp, unit


def initial_expectation(
    cues: Cues | None = None, *, confidence: float = 0.5
) -> CueExpectation:
    return CueExpectation(expected=cues or Cues(), confidence=confidence)


def prediction_error(
    expectation: CueExpectation,
    observed: Cues,
) -> PredictiveError:
    if not isinstance(expectation, CueExpectation):
        raise ValueError("expectation must be a CueExpectation")
    if not isinstance(observed, Cues):
        raise ValueError("observed must be a Cues instance")
    expected_values = expectation.expected.to_dict()
    observed_values = observed.to_dict()
    signed = {
        name: observed_values[name] - expected_values[name] for name in CUE_FIELDS
    }
    absolute = {name: abs(value) for name, value in signed.items()}
    mean_error = sum(absolute.values()) / len(CUE_FIELDS)
    peak_error = max(absolute.values(), default=0.0)
    surprise = clamp(
        (0.65 * mean_error + 0.35 * peak_error) * (0.5 + 0.5 * expectation.confidence)
    )
    return PredictiveError(
        signed=signed,
        absolute=absolute,
        surprise=surprise,
        confidence=expectation.confidence,
    )


def update_expectation(
    expectation: CueExpectation,
    observed: Cues,
    *,
    learning_rate: float = 0.25,
) -> CueExpectation:
    rate = unit(learning_rate, name="expectation.learning_rate")
    error = prediction_error(expectation, observed)
    previous = expectation.expected.to_dict()
    current = observed.to_dict()
    updated = {
        name: clamp(previous[name] + rate * (current[name] - previous[name]))
        for name in CUE_FIELDS
    }
    confidence = clamp(0.9 * expectation.confidence + 0.1 * (1.0 - error.surprise))
    return CueExpectation(expected=Cues(**updated), confidence=confidence)


__all__ = ["initial_expectation", "prediction_error", "update_expectation"]
