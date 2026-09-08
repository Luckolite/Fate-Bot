"""Deterministic offline evaluation for the autonomic composition layer.

The bundled suite contains hand-authored, non-personal numeric contexts.  This
module calibrates a few explicit composition parameters; it does not train a
neural model and must not be described as learning from real people.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .autonomic.base import AutonomicContext
from .autonomic.coordinator import (
    DEFAULT_AUTONOMIC_CALIBRATION,
    AutonomicCalibration,
    branch_tendencies,
    synthesize_autonomic_response,
)
from .config import OrganismConfig
from .models import (
    GUIDANCE_FIELDS,
    AutonomicMode,
    Cues,
    OrganismicState,
    ProtectionSummary,
    ProtectivePosture,
    Recruitment,
    StrategyName,
    unit,
)

EVALUATION_SCHEMA = "organism.evaluation.v1"
SYNTHETIC_CLASSIFICATION = "non_personal_synthetic"
_EVALUATION_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class StabilityExpectation:
    expected_retained: bool
    require_unconditioned_challenger: bool = True

    def __post_init__(self) -> None:
        if type(self.expected_retained) is not bool:
            raise ValueError("expected_retained must be a boolean")
        if type(self.require_unconditioned_challenger) is not bool:
            raise ValueError("require_unconditioned_challenger must be a boolean")


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    state: OrganismicState
    cues: Cues
    protection: ProtectionSummary
    predicted_risk: float
    strategy_affinities: Mapping[str, float]
    previous_mode: AutonomicMode | None
    expected_dominant_mode: AutonomicMode
    expected_strategy_order: tuple[StrategyName, ...]
    stability: StabilityExpectation | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not self.case_id.strip():
            raise ValueError("evaluation case_id must be a non-empty string")
        if not isinstance(self.state, OrganismicState):
            raise ValueError("evaluation state must be an OrganismicState")
        if not isinstance(self.cues, Cues):
            raise ValueError("evaluation cues must be Cues")
        if not isinstance(self.protection, ProtectionSummary):
            raise ValueError("evaluation protection must be a ProtectionSummary")
        object.__setattr__(
            self,
            "predicted_risk",
            unit(self.predicted_risk, name="evaluation predicted_risk"),
        )
        affinities = {
            strategy.value: unit(
                self.strategy_affinities.get(strategy.value, 0.5),
                name=f"strategy affinity {strategy.value}",
            )
            for strategy in StrategyName
        }
        unknown = set(self.strategy_affinities) - set(affinities)
        if unknown:
            raise ValueError(
                "unknown strategy affinities: " + ", ".join(sorted(unknown))
            )
        object.__setattr__(self, "strategy_affinities", MappingProxyType(affinities))
        if self.previous_mode is not None and not isinstance(
            self.previous_mode, AutonomicMode
        ):
            raise ValueError("previous_mode must be an AutonomicMode or None")
        if not isinstance(self.expected_dominant_mode, AutonomicMode):
            raise ValueError("expected_dominant_mode must be an AutonomicMode")
        order = tuple(self.expected_strategy_order)
        if len(order) != len(set(order)):
            raise ValueError("expected strategy order must not contain duplicates")
        if any(not isinstance(strategy, StrategyName) for strategy in order):
            raise ValueError("expected strategy order contains an unsupported value")
        object.__setattr__(self, "expected_strategy_order", order)
        if self.stability is not None:
            if not isinstance(self.stability, StabilityExpectation):
                raise ValueError("stability must be a StabilityExpectation")
            if self.previous_mode is None:
                raise ValueError("stability cases require a previous mode")


@dataclass(frozen=True)
class RecoveryExpectation:
    pair_id: str
    before_case: str
    after_case: str
    minimum_social_gain: float
    minimum_mobilization_drop: float
    minimum_warmth_gain: float
    required_strategy: StrategyName | None = None

    def __post_init__(self) -> None:
        for name in ("pair_id", "before_case", "after_case"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        for name in (
            "minimum_social_gain",
            "minimum_mobilization_drop",
            "minimum_warmth_gain",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0.0 <= float(value) <= 1.0
            ):
                raise ValueError(f"{name} must be finite and between 0 and 1")
            object.__setattr__(self, name, float(value))
        if self.required_strategy is not None and not isinstance(
            self.required_strategy, StrategyName
        ):
            raise ValueError("required_strategy must be a StrategyName or None")


@dataclass(frozen=True)
class TemporalExpectation:
    sequence_id: str
    case_order: tuple[str, ...]
    recovery_start_index: int
    minimum_activation_drop: float
    minimum_load_drop: float
    minimum_mobilization_drop: float

    def __post_init__(self) -> None:
        if not isinstance(self.sequence_id, str) or not self.sequence_id.strip():
            raise ValueError("sequence_id must be a non-empty string")
        order = tuple(self.case_order)
        if len(order) < 4 or len(order) != len(set(order)):
            raise ValueError(
                "temporal case_order must contain at least four unique cases"
            )
        object.__setattr__(self, "case_order", order)
        if (
            type(self.recovery_start_index) is not int
            or not 1 <= self.recovery_start_index < len(order) - 1
        ):
            raise ValueError("recovery_start_index is outside the temporal sequence")
        for name in (
            "minimum_activation_drop",
            "minimum_load_drop",
            "minimum_mobilization_drop",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0.0 <= float(value) <= 1.0
            ):
                raise ValueError(f"{name} must be finite and between 0 and 1")
            object.__setattr__(self, name, float(value))


@dataclass(frozen=True)
class EvaluationSuite:
    provenance: str
    cases: tuple[EvaluationCase, ...]
    recovery_pairs: tuple[RecoveryExpectation, ...]
    temporal_sequences: tuple[TemporalExpectation, ...]
    classification: str = SYNTHETIC_CLASSIFICATION

    def __post_init__(self) -> None:
        if self.classification != SYNTHETIC_CLASSIFICATION:
            raise ValueError(
                "evaluation data must be explicitly non-personal synthetic"
            )
        if not isinstance(self.provenance, str) or not self.provenance.strip():
            raise ValueError("evaluation provenance must be documented")
        cases = tuple(self.cases)
        if not cases:
            raise ValueError("evaluation suite must contain at least one case")
        case_ids = [case.case_id for case in cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("evaluation case IDs must be unique")
        if any(not isinstance(case, EvaluationCase) for case in cases):
            raise ValueError("evaluation suite contains an invalid case")
        pairs = tuple(self.recovery_pairs)
        if any(not isinstance(pair, RecoveryExpectation) for pair in pairs):
            raise ValueError("evaluation suite contains an invalid recovery pair")
        known = set(case_ids)
        for pair in pairs:
            if pair.before_case not in known or pair.after_case not in known:
                raise ValueError(
                    f"recovery pair {pair.pair_id!r} references an unknown case"
                )
            if pair.before_case == pair.after_case:
                raise ValueError("recovery pairs require distinct before/after cases")
        sequences = tuple(self.temporal_sequences)
        if any(not isinstance(item, TemporalExpectation) for item in sequences):
            raise ValueError("evaluation suite contains an invalid temporal sequence")
        sequence_ids = [item.sequence_id for item in sequences]
        if len(sequence_ids) != len(set(sequence_ids)):
            raise ValueError("temporal sequence IDs must be unique")
        for sequence in sequences:
            unknown = set(sequence.case_order) - known
            if unknown:
                raise ValueError(
                    f"temporal sequence {sequence.sequence_id!r} references unknown cases"
                )
        object.__setattr__(self, "cases", cases)
        object.__setattr__(self, "recovery_pairs", pairs)
        object.__setattr__(self, "temporal_sequences", sequences)


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    expected_dominant_mode: AutonomicMode
    dominant_mode: AutonomicMode
    modes: Mapping[AutonomicMode, float]
    guidance: Mapping[str, float]
    recruitments: tuple[Recruitment, ...]
    bounded: bool
    strategy_ranking_score: float
    stability_passed: bool | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "modes", MappingProxyType(dict(self.modes)))
        object.__setattr__(self, "guidance", MappingProxyType(dict(self.guidance)))
        object.__setattr__(self, "recruitments", tuple(self.recruitments))


@dataclass(frozen=True)
class RecoveryResult:
    pair_id: str
    social_gain: float
    mobilization_drop: float
    warmth_gain: float
    required_strategy_present: bool
    score: float
    passed: bool


@dataclass(frozen=True)
class TemporalResult:
    sequence_id: str
    rapid_protection_passed: bool
    immediate_safety_memory_passed: bool
    baseline_attractor_passed: bool
    activation_drop: float
    load_drop: float
    mobilization_drop: float
    monotonic_recovery_passed: bool
    branch_rebalancing_passed: bool
    score: float
    passed: bool


@dataclass(frozen=True)
class EvaluationMetrics:
    case_count: int
    dominant_mode_accuracy: float
    bounded_output_rate: float
    strategy_ranking_score: float
    stability_hysteresis_accuracy: float
    recovery_score: float
    temporal_regulation_score: float
    overall_score: float


@dataclass(frozen=True)
class EvaluationReport:
    metrics: EvaluationMetrics
    cases: tuple[CaseResult, ...]
    recovery: tuple[RecoveryResult, ...]
    temporal: tuple[TemporalResult, ...]
    calibration: AutonomicCalibration
    mode_temperature: float
    mode_hysteresis: float
    data_classification: str = SYNTHETIC_CLASSIFICATION

    def to_dict(self) -> dict[str, object]:
        return {
            "data_classification": self.data_classification,
            "model_kind": "deterministic_rule_based_composition",
            "trained_neural_model": False,
            "calibration": {
                "mode_tendency_gain": self.calibration.mode_tendency_gain,
                "restoration_mode_gain": self.calibration.restoration_mode_gain,
                "recruitment_tendency_floor": (
                    self.calibration.recruitment_tendency_floor
                ),
                "mode_temperature": self.mode_temperature,
                "mode_hysteresis": self.mode_hysteresis,
            },
            "metrics": {
                "case_count": self.metrics.case_count,
                "dominant_mode_accuracy": self.metrics.dominant_mode_accuracy,
                "bounded_output_rate": self.metrics.bounded_output_rate,
                "strategy_ranking_score": self.metrics.strategy_ranking_score,
                "stability_hysteresis_accuracy": (
                    self.metrics.stability_hysteresis_accuracy
                ),
                "recovery_score": self.metrics.recovery_score,
                "temporal_regulation_score": (self.metrics.temporal_regulation_score),
                "overall_score": self.metrics.overall_score,
            },
        }


@dataclass(frozen=True)
class CalibrationSearchSpace:
    mode_tendency_gains: tuple[float, ...] = (0.6, 0.9, 1.2)
    restoration_mode_gains: tuple[float, ...] = (0.15, 0.25, 0.4)
    recruitment_tendency_floors: tuple[float, ...] = (0.45, 0.55, 0.65)
    mode_temperatures: tuple[float, ...] = (0.65, 0.75, 0.9)
    mode_hysteresis_values: tuple[float, ...] = (0.05, 0.08, 0.12)

    def __post_init__(self) -> None:
        for name in (
            "mode_tendency_gains",
            "restoration_mode_gains",
            "recruitment_tendency_floors",
            "mode_temperatures",
            "mode_hysteresis_values",
        ):
            values = tuple(getattr(self, name))
            if not values:
                raise ValueError(f"{name} cannot be empty")
            if len(values) != len(set(values)):
                raise ValueError(f"{name} cannot contain duplicate values")
            object.__setattr__(self, name, values)
        for value in self.mode_temperatures:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0.0
            ):
                raise ValueError("mode temperatures must be finite and positive")
        for value in self.mode_hysteresis_values:
            unit(value, name="mode hysteresis candidate")
        for value in self.mode_tendency_gains:
            AutonomicCalibration(mode_tendency_gain=value)
        for value in self.restoration_mode_gains:
            AutonomicCalibration(restoration_mode_gain=value)
        for value in self.recruitment_tendency_floors:
            unit(value, name="recruitment tendency floor candidate")


@dataclass(frozen=True)
class CalibrationResult:
    baseline: EvaluationReport
    selected: EvaluationReport
    evaluated_candidates: int

    @property
    def improved(self) -> bool:
        return (
            self.selected.metrics.overall_score
            > self.baseline.metrics.overall_score + 1e-12
        )

    def configured(self, config: OrganismConfig) -> OrganismConfig:
        """Return a copy carrying the selected temperature and hysteresis."""
        return replace(
            config,
            mode_temperature=self.selected.mode_temperature,
            mode_hysteresis=self.selected.mode_hysteresis,
        )


def _mapping(value: object, *, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _state(values: object) -> OrganismicState:
    payload = _mapping(values, name="evaluation state")
    expected = {
        "activation",
        "pleasantness",
        "interaction_safety",
        "agency",
        "uncertainty",
        "load",
    }
    if set(payload) != expected:
        raise ValueError("evaluation state fields do not match the state schema")
    return OrganismicState(
        activation=payload["activation"],
        pleasantness=payload["pleasantness"],
        interaction_safety=payload["interaction_safety"],
        agency=payload["agency"],
        uncertainty=payload["uncertainty"],
        load=payload["load"],
        updated_at=_EVALUATION_TIME,
    )


def _protection(values: object) -> ProtectionSummary:
    payload = _mapping(values, name="evaluation protection")
    allowed = {"posture", "caution", "confidence", "uncertainty", "reason_codes"}
    if set(payload) - allowed:
        raise ValueError("evaluation protection contains unsupported fields")
    return ProtectionSummary(
        posture=ProtectivePosture(payload["posture"]),
        caution=payload["caution"],
        confidence=payload["confidence"],
        uncertainty=payload["uncertainty"],
        reason_codes=tuple(payload.get("reason_codes", ())),
    )


def _stability(values: object | None) -> StabilityExpectation | None:
    if values is None:
        return None
    payload = _mapping(values, name="stability expectation")
    allowed = {"expected_retained", "require_unconditioned_challenger"}
    if set(payload) - allowed or "expected_retained" not in payload:
        raise ValueError("stability expectation fields are invalid")
    return StabilityExpectation(
        expected_retained=payload["expected_retained"],
        require_unconditioned_challenger=payload.get(
            "require_unconditioned_challenger", True
        ),
    )


def _case(values: object) -> EvaluationCase:
    payload = _mapping(values, name="evaluation case")
    required = {
        "case_id",
        "state",
        "cues",
        "protection",
        "predicted_risk",
        "previous_mode",
        "expected_dominant_mode",
        "expected_strategy_order",
    }
    allowed = required | {"strategy_affinities", "stability"}
    if set(payload) - allowed or not required <= set(payload):
        raise ValueError("evaluation case fields are invalid")
    previous = payload["previous_mode"]
    affinities = _mapping(
        payload.get("strategy_affinities", {}),
        name="strategy affinities",
    )
    return EvaluationCase(
        case_id=payload["case_id"],
        state=_state(payload["state"]),
        cues=Cues.from_dict(_mapping(payload["cues"], name="evaluation cues")),
        protection=_protection(payload["protection"]),
        predicted_risk=payload["predicted_risk"],
        strategy_affinities={str(name): value for name, value in affinities.items()},
        previous_mode=None if previous is None else AutonomicMode(previous),
        expected_dominant_mode=AutonomicMode(payload["expected_dominant_mode"]),
        expected_strategy_order=tuple(
            StrategyName(value) for value in payload["expected_strategy_order"]
        ),
        stability=_stability(payload.get("stability")),
    )


def _recovery(values: object) -> RecoveryExpectation:
    payload = _mapping(values, name="recovery expectation")
    required = {
        "pair_id",
        "before_case",
        "after_case",
        "minimum_social_gain",
        "minimum_mobilization_drop",
        "minimum_warmth_gain",
    }
    allowed = required | {"required_strategy"}
    if set(payload) - allowed or not required <= set(payload):
        raise ValueError("recovery expectation fields are invalid")
    strategy = payload.get("required_strategy")
    return RecoveryExpectation(
        pair_id=payload["pair_id"],
        before_case=payload["before_case"],
        after_case=payload["after_case"],
        minimum_social_gain=payload["minimum_social_gain"],
        minimum_mobilization_drop=payload["minimum_mobilization_drop"],
        minimum_warmth_gain=payload["minimum_warmth_gain"],
        required_strategy=None if strategy is None else StrategyName(strategy),
    )


def _temporal(values: object) -> TemporalExpectation:
    payload = _mapping(values, name="temporal expectation")
    required = {
        "sequence_id",
        "case_order",
        "recovery_start_index",
        "minimum_activation_drop",
        "minimum_load_drop",
        "minimum_mobilization_drop",
    }
    if set(payload) != required:
        raise ValueError("temporal expectation fields are invalid")
    return TemporalExpectation(
        sequence_id=payload["sequence_id"],
        case_order=tuple(payload["case_order"]),
        recovery_start_index=payload["recovery_start_index"],
        minimum_activation_drop=payload["minimum_activation_drop"],
        minimum_load_drop=payload["minimum_load_drop"],
        minimum_mobilization_drop=payload["minimum_mobilization_drop"],
    )


def load_evaluation_suite(path: str | Path | None = None) -> EvaluationSuite:
    """Load and strictly validate the curated synthetic evaluation suite."""
    source = (
        Path(path)
        if path is not None
        else Path(__file__).with_name("evaluation_data") / "cases.json"
    )
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(
            f"unable to load evaluation suite {source}: {error}"
        ) from error
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != EVALUATION_SCHEMA
    ):
        raise ValueError("unsupported organism evaluation schema")
    allowed = {
        "schema_version",
        "classification",
        "provenance",
        "cases",
        "recovery_pairs",
        "temporal_sequences",
    }
    if set(payload) != allowed:
        raise ValueError("evaluation suite fields do not match its schema")
    try:
        cases = tuple(_case(value) for value in payload["cases"])
        recovery = tuple(_recovery(value) for value in payload["recovery_pairs"])
        temporal = tuple(_temporal(value) for value in payload["temporal_sequences"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"invalid evaluation suite: {error}") from error
    return EvaluationSuite(
        classification=payload["classification"],
        provenance=payload["provenance"],
        cases=cases,
        recovery_pairs=recovery,
        temporal_sequences=temporal,
    )


def _synthesize(
    case: EvaluationCase,
    config: OrganismConfig,
    calibration: AutonomicCalibration,
    *,
    previous_mode: AutonomicMode | None,
) -> tuple[
    dict[AutonomicMode, float],
    AutonomicMode,
    dict[str, float],
    tuple[Recruitment, ...],
]:
    return synthesize_autonomic_response(
        state=case.state,
        cues=case.cues,
        protection=case.protection,
        predicted_risk=case.predicted_risk,
        strategy_affinities=case.strategy_affinities,
        previous_mode=previous_mode,
        config=config,
        calibration=calibration,
    )


def _bounded_output(
    modes: Mapping[AutonomicMode, float],
    guidance: Mapping[str, float],
    recruitments: tuple[Recruitment, ...],
) -> bool:
    mode_values = tuple(modes.values())
    guidance_values = tuple(guidance.values())
    recruitment_scores = tuple(item.score for item in recruitments)
    names = tuple(item.name for item in recruitments)
    return (
        set(modes) == set(AutonomicMode)
        and set(guidance) == GUIDANCE_FIELDS
        and all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in mode_values)
        and abs(sum(mode_values) - 1.0) <= 1e-9
        and all(
            math.isfinite(value) and 0.0 <= value <= 1.0 for value in guidance_values
        )
        and all(
            math.isfinite(value) and 0.0 <= value <= 1.0 for value in recruitment_scores
        )
        and len(names) == len(set(names))
    )


def _strategy_ranking_score(
    expected: tuple[StrategyName, ...],
    actual: tuple[Recruitment, ...],
) -> float:
    names = tuple(item.name for item in actual)
    if not expected:
        return 1.0 if not names else 0.0
    scores = []
    scale = max(len(expected), len(names), 1)
    for expected_index, name in enumerate(expected):
        if name not in names:
            scores.append(0.0)
            continue
        actual_index = names.index(name)
        scores.append(max(0.0, 1.0 - abs(actual_index - expected_index) / scale))
    return sum(scores) / len(scores)


def evaluate_autonomic(
    config: OrganismConfig,
    *,
    suite: EvaluationSuite | None = None,
    calibration: AutonomicCalibration = DEFAULT_AUTONOMIC_CALIBRATION,
) -> EvaluationReport:
    """Evaluate bounded behavior against explicit synthetic expectations."""
    if not isinstance(config, OrganismConfig):
        raise ValueError("config must be an OrganismConfig")
    selected_suite = suite or load_evaluation_suite()
    if selected_suite.classification != SYNTHETIC_CLASSIFICATION:
        raise ValueError("only non-personal synthetic evaluation suites are accepted")

    results: list[CaseResult] = []
    for case in selected_suite.cases:
        modes, dominant, guidance, recruitments = _synthesize(
            case,
            config,
            calibration,
            previous_mode=case.previous_mode,
        )
        stability_passed: bool | None = None
        if case.stability is not None:
            retained = dominant is case.previous_mode
            retention_matches = retained is case.stability.expected_retained
            challenger_matches = True
            if case.stability.require_unconditioned_challenger:
                _, unconditioned, _, _ = _synthesize(
                    case,
                    config,
                    calibration,
                    previous_mode=None,
                )
                challenger_matches = unconditioned is not case.previous_mode
            stability_passed = retention_matches and challenger_matches
        results.append(
            CaseResult(
                case_id=case.case_id,
                expected_dominant_mode=case.expected_dominant_mode,
                dominant_mode=dominant,
                modes=modes,
                guidance=guidance,
                recruitments=recruitments,
                bounded=_bounded_output(modes, guidance, recruitments),
                strategy_ranking_score=_strategy_ranking_score(
                    case.expected_strategy_order, recruitments
                ),
                stability_passed=stability_passed,
            )
        )

    by_id = {result.case_id: result for result in results}
    case_definitions = {case.case_id: case for case in selected_suite.cases}
    recovery_results: list[RecoveryResult] = []
    for expectation in selected_suite.recovery_pairs:
        before = by_id[expectation.before_case]
        after = by_id[expectation.after_case]
        social_gain = (
            after.modes[AutonomicMode.SOCIAL_ENGAGEMENT]
            - before.modes[AutonomicMode.SOCIAL_ENGAGEMENT]
        )
        mobilization_drop = (
            before.modes[AutonomicMode.MOBILIZATION]
            - after.modes[AutonomicMode.MOBILIZATION]
        )
        warmth_gain = after.guidance["warmth"] - before.guidance["warmth"]
        strategy_present = expectation.required_strategy is None or any(
            item.name is expectation.required_strategy for item in after.recruitments
        )
        checks = (
            social_gain >= expectation.minimum_social_gain,
            mobilization_drop >= expectation.minimum_mobilization_drop,
            warmth_gain >= expectation.minimum_warmth_gain,
            strategy_present,
        )
        score = sum(checks) / len(checks)
        recovery_results.append(
            RecoveryResult(
                pair_id=expectation.pair_id,
                social_gain=social_gain,
                mobilization_drop=mobilization_drop,
                warmth_gain=warmth_gain,
                required_strategy_present=strategy_present,
                score=score,
                passed=all(checks),
            )
        )

    temporal_results: list[TemporalResult] = []
    for expectation in selected_suite.temporal_sequences:
        ordered_results = [by_id[case_id] for case_id in expectation.case_order]
        ordered_cases = [
            case_definitions[case_id] for case_id in expectation.case_order
        ]
        recovery_results_slice = ordered_results[expectation.recovery_start_index :]
        recovery_cases_slice = ordered_cases[expectation.recovery_start_index :]
        activations = [case.state.activation for case in recovery_cases_slice]
        loads = [case.state.load for case in recovery_cases_slice]
        mobilizations = [
            result.modes[AutonomicMode.MOBILIZATION]
            for result in recovery_results_slice
        ]
        tendency_sequence = [
            branch_tendencies(
                AutonomicContext(
                    cues=case.cues,
                    state=case.state,
                    protection=case.protection,
                    predicted_risk=case.predicted_risk,
                )
            )
            for case in recovery_cases_slice
        ]
        ventral_tendencies = [
            tendencies["ventral_vagal"] for tendencies in tendency_sequence
        ]
        sympathetic_tendencies = [
            tendencies["sympathetic"] for tendencies in tendency_sequence
        ]
        restoration_tendencies = [
            tendencies["parasympathetic"] for tendencies in tendency_sequence
        ]

        def nonincreasing(values: list[float]) -> bool:
            return all(
                later <= earlier + 1e-12 for earlier, later in zip(values, values[1:])
            )

        def nondecreasing(values: list[float]) -> bool:
            return all(
                later + 1e-12 >= earlier for earlier, later in zip(values, values[1:])
            )

        activation_drop = activations[0] - activations[-1]
        load_drop = loads[0] - loads[-1]
        mobilization_drop = mobilizations[0] - mobilizations[-1]
        rapid_protection = (
            ordered_results[0].dominant_mode is AutonomicMode.MOBILIZATION
        )
        immediate_memory = (
            ordered_results[1].dominant_mode is AutonomicMode.MOBILIZATION
        )
        baseline_attractor = (
            ordered_results[-1].dominant_mode is AutonomicMode.SOCIAL_ENGAGEMENT
        )
        monotonic_recovery = (
            nonincreasing(activations)
            and nonincreasing(loads)
            and nonincreasing(mobilizations)
        )
        branch_rebalancing = (
            nondecreasing(ventral_tendencies)
            and nonincreasing(sympathetic_tendencies)
            and nondecreasing(restoration_tendencies)
        )
        checks = (
            rapid_protection,
            immediate_memory,
            baseline_attractor,
            monotonic_recovery,
            branch_rebalancing,
            activation_drop >= expectation.minimum_activation_drop,
            load_drop >= expectation.minimum_load_drop,
            mobilization_drop >= expectation.minimum_mobilization_drop,
        )
        temporal_results.append(
            TemporalResult(
                sequence_id=expectation.sequence_id,
                rapid_protection_passed=rapid_protection,
                immediate_safety_memory_passed=immediate_memory,
                baseline_attractor_passed=baseline_attractor,
                activation_drop=activation_drop,
                load_drop=load_drop,
                mobilization_drop=mobilization_drop,
                monotonic_recovery_passed=monotonic_recovery,
                branch_rebalancing_passed=branch_rebalancing,
                score=sum(checks) / len(checks),
                passed=all(checks),
            )
        )

    case_count = len(results)
    dominant_accuracy = (
        sum(result.dominant_mode is result.expected_dominant_mode for result in results)
        / case_count
    )
    bounded_rate = sum(result.bounded for result in results) / case_count
    ranking_score = (
        sum(result.strategy_ranking_score for result in results) / case_count
    )
    stability_values = [
        result.stability_passed
        for result in results
        if result.stability_passed is not None
    ]
    stability_accuracy = (
        sum(stability_values) / len(stability_values) if stability_values else 1.0
    )
    recovery_score = (
        sum(result.score for result in recovery_results) / len(recovery_results)
        if recovery_results
        else 1.0
    )
    temporal_score = (
        sum(result.score for result in temporal_results) / len(temporal_results)
        if temporal_results
        else 1.0
    )
    overall = (
        0.25 * dominant_accuracy
        + 0.2 * bounded_rate
        + 0.15 * ranking_score
        + 0.1 * stability_accuracy
        + 0.1 * recovery_score
        + 0.2 * temporal_score
    )
    metrics = EvaluationMetrics(
        case_count=case_count,
        dominant_mode_accuracy=dominant_accuracy,
        bounded_output_rate=bounded_rate,
        strategy_ranking_score=ranking_score,
        stability_hysteresis_accuracy=stability_accuracy,
        recovery_score=recovery_score,
        temporal_regulation_score=temporal_score,
        overall_score=overall,
    )
    return EvaluationReport(
        metrics=metrics,
        cases=tuple(results),
        recovery=tuple(recovery_results),
        temporal=tuple(temporal_results),
        calibration=calibration,
        mode_temperature=config.mode_temperature,
        mode_hysteresis=config.mode_hysteresis,
    )


def _distance_from_baseline(
    calibration: AutonomicCalibration,
    temperature: float,
    hysteresis: float,
    config: OrganismConfig,
) -> float:
    baseline = DEFAULT_AUTONOMIC_CALIBRATION
    return (
        abs(calibration.mode_tendency_gain - baseline.mode_tendency_gain)
        + abs(calibration.restoration_mode_gain - baseline.restoration_mode_gain)
        + abs(
            calibration.recruitment_tendency_floor - baseline.recruitment_tendency_floor
        )
        + abs(temperature - config.mode_temperature)
        + abs(hysteresis - config.mode_hysteresis)
    )


def _selection_key(
    report: EvaluationReport,
    distance: float,
) -> tuple[float, ...]:
    metrics = report.metrics
    # Boundedness is a hard first priority.  The final term deterministically
    # prefers the smallest departure when behavior scores tie.
    return (
        metrics.bounded_output_rate,
        metrics.overall_score,
        metrics.dominant_mode_accuracy,
        metrics.temporal_regulation_score,
        metrics.recovery_score,
        metrics.stability_hysteresis_accuracy,
        metrics.strategy_ranking_score,
        -distance,
    )


def calibrate_autonomic(
    config: OrganismConfig,
    *,
    suite: EvaluationSuite | None = None,
    search_space: CalibrationSearchSpace | None = None,
) -> CalibrationResult:
    """Grid-calibrate explicit parameters against the synthetic suite.

    The result is an offline recommendation.  It does not mutate configuration,
    persist weights, ingest user content, or perform neural-model training.
    """
    selected_suite = suite or load_evaluation_suite()
    space = search_space or CalibrationSearchSpace()
    baseline = evaluate_autonomic(config, suite=selected_suite)
    best = baseline
    best_key = _selection_key(best, 0.0)
    count = 0
    candidates = product(
        space.mode_tendency_gains,
        space.restoration_mode_gains,
        space.recruitment_tendency_floors,
        space.mode_temperatures,
        space.mode_hysteresis_values,
    )
    for mode_gain, restoration_gain, floor, temperature, hysteresis in candidates:
        calibration = AutonomicCalibration(
            mode_tendency_gain=mode_gain,
            restoration_mode_gain=restoration_gain,
            recruitment_tendency_floor=floor,
        )
        candidate_config = replace(
            config,
            mode_temperature=temperature,
            mode_hysteresis=hysteresis,
        )
        report = evaluate_autonomic(
            candidate_config,
            suite=selected_suite,
            calibration=calibration,
        )
        count += 1
        distance = _distance_from_baseline(
            calibration,
            temperature,
            hysteresis,
            config,
        )
        key = _selection_key(report, distance)
        if key > best_key:
            best = report
            best_key = key
    return CalibrationResult(
        baseline=baseline,
        selected=best,
        evaluated_candidates=count,
    )


def main() -> None:
    """Print a reproducible, machine-readable offline calibration summary."""
    result = calibrate_autonomic(OrganismConfig.load())
    payload = {
        "baseline": result.baseline.to_dict(),
        "selected": result.selected.to_dict(),
        "evaluated_candidates": result.evaluated_candidates,
        "improved": result.improved,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()


__all__ = [
    "CalibrationResult",
    "CalibrationSearchSpace",
    "CaseResult",
    "EvaluationCase",
    "EvaluationMetrics",
    "EvaluationReport",
    "EvaluationSuite",
    "RecoveryExpectation",
    "RecoveryResult",
    "StabilityExpectation",
    "TemporalExpectation",
    "TemporalResult",
    "calibrate_autonomic",
    "evaluate_autonomic",
    "load_evaluation_suite",
]
