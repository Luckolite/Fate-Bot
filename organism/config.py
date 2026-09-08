"""Configuration loading and validation for Organism."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .models import StrategyName, signed_unit, unit

_MAX_RUNTIME_DURATION_SECONDS = 7 * 24 * 60 * 60
_MAX_RUNTIME_DURATION_HOURS = 180 * 24


@dataclass(frozen=True)
class OrganismConfig:
    baseline_state: Mapping[str, float]
    cue_coefficients: Mapping[str, float]
    cue_learning_rate: float
    cue_event_delta_cap: float
    cue_weight_min: float
    cue_weight_max: float
    strategy_learning_rate: float
    strategy_adaptive_span: float
    historical_influence_cap: float
    minimum_guarded_episodes: int
    evidence_retention_days: int
    max_evidence_events: int
    max_evidence_per_partition: int
    owner_reserved_evidence_events: int
    max_retraction_tombstones: int
    max_retraction_tombstones_per_partition: int
    authorization_ttl_seconds: int
    max_authorization_tokens: int
    severe_guard_hours: float
    recovery_half_life_hours: float
    event_gain: float
    defensive_release_ratio: float
    regulated_release_boost: float
    mode_temperature: float
    mode_hysteresis: float
    prompt_ttl_seconds: int
    max_future_skew_seconds: int
    max_observation_age_seconds: int
    recent_observation_limit: int
    max_transient_states: int
    transient_state_retention_hours: float
    strategy_baselines: Mapping[StrategyName, float]
    capacity_baselines: Mapping[str, float]

    def __post_init__(self) -> None:
        """Validate direct construction as strictly as file-backed loading.

        ``dataclasses.replace`` and dependency injection both call this path,
        so validation cannot live only in :meth:`load`.  Normalizing here also
        makes every accepted instance immutable and safe to share across
        sectors, regardless of how it was created.
        """

        def mapping(name: str) -> dict:
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise ValueError(f"{name} must be a mapping")
            return dict(value)

        def positive_number(name: str) -> float:
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value <= 0
            ):
                raise ValueError(f"{name} must be a positive number")
            return float(value)

        def positive_integer(name: str) -> int:
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
            return value

        baseline = mapping("baseline_state")
        expected_state = {
            "activation",
            "pleasantness",
            "interaction_safety",
            "agency",
            "uncertainty",
            "load",
        }
        if set(baseline) != expected_state:
            raise ValueError("baseline_state fields do not match the state schema")
        baseline = {
            name: (
                signed_unit(value, name=f"baseline_state.{name}")
                if name == "pleasantness"
                else unit(value, name=f"baseline_state.{name}")
            )
            for name, value in baseline.items()
        }

        coefficients = mapping("cue_coefficients")
        expected_features = {
            "threat",
            "boundary_pressure",
            "uncertainty",
            "novelty",
            "demand",
            "low_controllability",
            "energy_cost",
        }
        if set(coefficients) != expected_features:
            raise ValueError("cue_coefficients fields do not match the cue schema")
        coefficients = {
            name: unit(value, name=f"cue_coefficients.{name}")
            for name, value in coefficients.items()
        }
        if sum(coefficients.values()) <= 0:
            raise ValueError("cue coefficients must contain positive weight")

        raw_strategies = mapping("strategy_baselines")
        try:
            strategies = {
                StrategyName(name): unit(
                    value,
                    name=f"strategy_baselines.{getattr(name, 'value', name)}",
                )
                for name, value in raw_strategies.items()
            }
        except (TypeError, ValueError) as error:
            raise ValueError(f"strategy_baselines is invalid: {error}") from error
        if len(strategies) != len(raw_strategies) or set(strategies) != set(
            StrategyName
        ):
            raise ValueError("strategy_baselines must contain every strategy")

        capacities = mapping("capacity_baselines")
        expected_capacities = {
            "regulation",
            "boundary",
            "recovery",
            "connection",
            "uncertainty_tolerance",
        }
        if set(capacities) != expected_capacities:
            raise ValueError(
                "capacity_baselines fields do not match the capacity schema"
            )
        capacities = {
            name: unit(value, name=f"capacity_baselines.{name}")
            for name, value in capacities.items()
        }

        unit_fields = (
            "cue_learning_rate",
            "cue_event_delta_cap",
            "strategy_learning_rate",
            "strategy_adaptive_span",
            "historical_influence_cap",
            "event_gain",
            "defensive_release_ratio",
            "regulated_release_boost",
            "mode_hysteresis",
        )
        for name in unit_fields:
            object.__setattr__(self, name, unit(getattr(self, name), name=name))

        positive_number_fields = (
            "cue_weight_min",
            "cue_weight_max",
            "severe_guard_hours",
            "recovery_half_life_hours",
            "mode_temperature",
            "transient_state_retention_hours",
        )
        for name in positive_number_fields:
            object.__setattr__(self, name, positive_number(name))

        positive_integer_fields = (
            "minimum_guarded_episodes",
            "evidence_retention_days",
            "max_evidence_events",
            "max_evidence_per_partition",
            "owner_reserved_evidence_events",
            "max_retraction_tombstones",
            "max_retraction_tombstones_per_partition",
            "authorization_ttl_seconds",
            "max_authorization_tokens",
            "prompt_ttl_seconds",
            "max_future_skew_seconds",
            "max_observation_age_seconds",
            "recent_observation_limit",
            "max_transient_states",
        )
        for name in positive_integer_fields:
            object.__setattr__(self, name, positive_integer(name))

        if self.cue_weight_min >= self.cue_weight_max:
            raise ValueError("cue_weight_min must be less than cue_weight_max")
        if not self.cue_weight_min <= 1.0 <= self.cue_weight_max:
            raise ValueError("cue weight bounds must contain the 1.0 baseline")
        if self.evidence_retention_days > 180:
            raise ValueError("evidence_retention_days cannot exceed 180")
        if self.max_evidence_events > 4096:
            raise ValueError("max_evidence_events cannot exceed 4096")
        if self.max_evidence_per_partition > min(256, self.max_evidence_events):
            raise ValueError(
                "max_evidence_per_partition cannot exceed 256 or the global cap"
            )
        if self.owner_reserved_evidence_events >= self.max_evidence_events:
            raise ValueError(
                "owner_reserved_evidence_events must be less than max_evidence_events"
            )
        if self.max_retraction_tombstones > 4096:
            raise ValueError("max_retraction_tombstones cannot exceed 4096")
        if self.max_retraction_tombstones_per_partition > min(
            256, self.max_retraction_tombstones
        ):
            raise ValueError(
                "max_retraction_tombstones_per_partition cannot exceed 256 or "
                "the global tombstone cap"
            )
        if self.authorization_ttl_seconds > 7 * 24 * 60 * 60:
            raise ValueError("authorization_ttl_seconds cannot exceed seven days")
        if self.max_authorization_tokens > 16384:
            raise ValueError("max_authorization_tokens cannot exceed 16384")
        if self.recent_observation_limit > 256:
            raise ValueError("recent_observation_limit cannot exceed 256")
        if self.max_transient_states > 256:
            raise ValueError("max_transient_states cannot exceed 256")
        for name in (
            "prompt_ttl_seconds",
            "max_future_skew_seconds",
            "max_observation_age_seconds",
        ):
            if getattr(self, name) > _MAX_RUNTIME_DURATION_SECONDS:
                raise ValueError(f"{name} cannot exceed seven days")
        for name in (
            "severe_guard_hours",
            "recovery_half_life_hours",
            "transient_state_retention_hours",
        ):
            if getattr(self, name) > _MAX_RUNTIME_DURATION_HOURS:
                raise ValueError(f"{name} cannot exceed 180 days")
        if any(
            value - self.strategy_adaptive_span < 0
            or value + self.strategy_adaptive_span > 1
            for value in strategies.values()
        ):
            raise ValueError("strategy adaptive bounds must stay between 0 and 1")
        if self.event_gain <= 0.0:
            raise ValueError("event_gain must be greater than zero")
        if self.defensive_release_ratio + self.regulated_release_boost <= 0.0:
            raise ValueError(
                "at least one event-driven release parameter must be positive"
            )

        object.__setattr__(self, "baseline_state", MappingProxyType(baseline))
        object.__setattr__(self, "cue_coefficients", MappingProxyType(coefficients))
        object.__setattr__(self, "strategy_baselines", MappingProxyType(strategies))
        object.__setattr__(self, "capacity_baselines", MappingProxyType(capacities))

    @classmethod
    def load(cls, path: str | Path | None = None) -> "OrganismConfig":
        source = (
            Path(path)
            if path is not None
            else Path(__file__).with_name("defaults.json")
        )
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError(
                f"unable to load organism config {source}: {error}"
            ) from error
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != "organism.config.v1"
        ):
            raise ValueError("unsupported organism configuration schema")
        try:
            baseline = dict(payload["baseline_state"])
            coefficients = dict(payload["cue_coefficients"])
            learning = payload["learning"]
            state = payload["state"]
            strategy_values = {
                StrategyName(name): unit(value, name=f"strategy_baselines.{name}")
                for name, value in payload["strategy_baselines"].items()
            }
            capacity_values = {
                str(name): unit(value, name=f"capacity_baselines.{name}")
                for name, value in payload["capacity_baselines"].items()
            }
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"invalid organism config structure: {error}") from error

        expected_state = {
            "activation",
            "pleasantness",
            "interaction_safety",
            "agency",
            "uncertainty",
            "load",
        }
        if set(baseline) != expected_state:
            raise ValueError("baseline_state fields do not match the state schema")
        for name, value in baseline.items():
            baseline[name] = (
                signed_unit(value, name=f"baseline_state.{name}")
                if name == "pleasantness"
                else unit(value, name=f"baseline_state.{name}")
            )
        expected_features = {
            "threat",
            "boundary_pressure",
            "uncertainty",
            "novelty",
            "demand",
            "low_controllability",
            "energy_cost",
        }
        if set(coefficients) != expected_features:
            raise ValueError("cue_coefficients fields do not match the cue schema")
        for name, value in coefficients.items():
            coefficients[name] = unit(value, name=f"cue_coefficients.{name}")
        if sum(coefficients.values()) <= 0:
            raise ValueError("cue coefficients must contain positive weight")
        if set(strategy_values) != set(StrategyName):
            raise ValueError("strategy_baselines must contain every strategy")
        expected_capacities = {
            "regulation",
            "boundary",
            "recovery",
            "connection",
            "uncertainty_tolerance",
        }
        if set(capacity_values) != expected_capacities:
            raise ValueError(
                "capacity_baselines fields do not match the capacity schema"
            )

        def positive_number(section, name: str) -> float:
            value = section[name]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value <= 0
            ):
                raise ValueError(f"{name} must be a positive number")
            return float(value)

        def positive_integer(section, name: str) -> int:
            value = section[name]
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
            return value

        minimum = positive_number(learning, "cue_weight_min")
        maximum = positive_number(learning, "cue_weight_max")
        if minimum >= maximum:
            raise ValueError("cue_weight_min must be less than cue_weight_max")
        if not minimum <= 1.0 <= maximum:
            raise ValueError("cue weight bounds must contain the 1.0 baseline")
        retention_days = positive_integer(learning, "evidence_retention_days")
        maximum_events = positive_integer(learning, "max_evidence_events")
        maximum_partition_events = positive_integer(
            learning, "max_evidence_per_partition"
        )
        owner_reserved_events = positive_integer(
            learning, "owner_reserved_evidence_events"
        )
        maximum_tombstones = positive_integer(learning, "max_retraction_tombstones")
        maximum_partition_tombstones = positive_integer(
            learning, "max_retraction_tombstones_per_partition"
        )
        authorization_ttl_seconds = positive_integer(
            learning, "authorization_ttl_seconds"
        )
        maximum_authorization_tokens = positive_integer(
            learning, "max_authorization_tokens"
        )
        if retention_days > 180:
            raise ValueError("evidence_retention_days cannot exceed 180")
        if maximum_events > 4096:
            raise ValueError("max_evidence_events cannot exceed 4096")
        if maximum_partition_events > min(256, maximum_events):
            raise ValueError(
                "max_evidence_per_partition cannot exceed 256 or the global cap"
            )
        if owner_reserved_events >= maximum_events:
            raise ValueError(
                "owner_reserved_evidence_events must be less than max_evidence_events"
            )
        if maximum_tombstones > 4096:
            raise ValueError("max_retraction_tombstones cannot exceed 4096")
        if maximum_partition_tombstones > min(256, maximum_tombstones):
            raise ValueError(
                "max_retraction_tombstones_per_partition cannot exceed 256 or "
                "the global tombstone cap"
            )
        if authorization_ttl_seconds > 7 * 24 * 60 * 60:
            raise ValueError("authorization_ttl_seconds cannot exceed seven days")
        if maximum_authorization_tokens > 16384:
            raise ValueError("max_authorization_tokens cannot exceed 16384")
        adaptive_span = unit(
            learning["strategy_adaptive_span"], name="strategy_adaptive_span"
        )
        if any(
            baseline - adaptive_span < 0 or baseline + adaptive_span > 1
            for baseline in strategy_values.values()
        ):
            raise ValueError("strategy adaptive bounds must stay between 0 and 1")
        event_gain = unit(state["event_gain"], name="event_gain")
        if event_gain <= 0.0:
            raise ValueError("event_gain must be greater than zero")
        defensive_release_ratio = unit(
            state["defensive_release_ratio"],
            name="defensive_release_ratio",
        )
        regulated_release_boost = unit(
            state["regulated_release_boost"],
            name="regulated_release_boost",
        )
        if defensive_release_ratio + regulated_release_boost <= 0.0:
            raise ValueError(
                "at least one event-driven release parameter must be positive"
            )
        return cls(
            baseline_state=baseline,
            cue_coefficients=coefficients,
            cue_learning_rate=unit(
                learning["cue_learning_rate"], name="cue_learning_rate"
            ),
            cue_event_delta_cap=unit(
                learning["cue_event_delta_cap"], name="cue_event_delta_cap"
            ),
            cue_weight_min=minimum,
            cue_weight_max=maximum,
            strategy_learning_rate=unit(
                learning["strategy_learning_rate"], name="strategy_learning_rate"
            ),
            strategy_adaptive_span=adaptive_span,
            historical_influence_cap=unit(
                learning["historical_influence_cap"],
                name="historical_influence_cap",
            ),
            minimum_guarded_episodes=positive_integer(
                learning, "minimum_guarded_episodes"
            ),
            evidence_retention_days=retention_days,
            max_evidence_events=maximum_events,
            max_evidence_per_partition=maximum_partition_events,
            owner_reserved_evidence_events=owner_reserved_events,
            max_retraction_tombstones=maximum_tombstones,
            max_retraction_tombstones_per_partition=maximum_partition_tombstones,
            authorization_ttl_seconds=authorization_ttl_seconds,
            max_authorization_tokens=maximum_authorization_tokens,
            severe_guard_hours=positive_number(learning, "severe_guard_hours"),
            recovery_half_life_hours=positive_number(state, "recovery_half_life_hours"),
            event_gain=event_gain,
            defensive_release_ratio=defensive_release_ratio,
            regulated_release_boost=regulated_release_boost,
            mode_temperature=positive_number(state, "mode_temperature"),
            mode_hysteresis=unit(state["mode_hysteresis"], name="mode_hysteresis"),
            prompt_ttl_seconds=positive_integer(state, "prompt_ttl_seconds"),
            max_future_skew_seconds=positive_integer(state, "max_future_skew_seconds"),
            max_observation_age_seconds=positive_integer(
                state, "max_observation_age_seconds"
            ),
            recent_observation_limit=positive_integer(
                state, "recent_observation_limit"
            ),
            max_transient_states=positive_integer(state, "max_transient_states"),
            transient_state_retention_hours=positive_number(
                state, "transient_state_retention_hours"
            ),
            strategy_baselines=strategy_values,
            capacity_baselines=capacity_values,
        )
