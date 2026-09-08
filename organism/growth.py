"""Replay trusted outcomes into bounded, reversible adaptive weights."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Iterable, Mapping

from .config import OrganismConfig
from .healing import evidence_decay
from .learning import (
    DAILY_CONTRIBUTION_CAP,
    EPISODE_CONTRIBUTION_CAP,
    HEALING_KINDS,
    LEARNING_KINDS,
)
from .models import FeedbackKind, ReasonCode, StrategyName, parse_timestamp
from .weights import baseline_cue_weights, update_cue_weights


def _risk_target(record: Mapping[str, object]) -> float | None:
    kind = FeedbackKind(record["kind"])
    reason = ReasonCode(record["reason"])
    if kind is FeedbackKind.ADVERSE:
        return 1.0
    if kind is FeedbackKind.REPAIR:
        return 0.15
    if kind is FeedbackKind.SAFE or reason is ReasonCode.FALSE_ALARM:
        return 0.0
    return None


def rebuild_growth(
    records: Iterable[Mapping[str, object]],
    config: OrganismConfig,
    now: datetime,
) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    cue_weights = baseline_cue_weights()
    strategy_weights = {
        strategy.value: value for strategy, value in config.strategy_baselines.items()
    }
    capacities = dict(config.capacity_baselines)
    # Person-specific events shape only that local profile. Global calibration is
    # a separate, explicitly owner-reviewed channel using scope="global".
    eligible = [
        record
        for record in records
        if record.get("global_calibration_eligible") is True
    ]
    ordered = sorted(eligible, key=lambda item: item["observed_at"])
    adverse_episode_mass: dict[tuple[str, str, str, str], float] = defaultdict(float)
    healing_episode_mass: dict[tuple[str, str, str, str], float] = defaultdict(float)
    daily_mass: dict[tuple[str, str, str, str, str], float] = defaultdict(float)
    for record in ordered:
        observed_at = parse_timestamp(record["observed_at"])
        age_days = max(0.0, (now - observed_at).total_seconds() / 86400.0)
        memory = evidence_decay(age_days, float(record["severity"]))
        raw_strength = float(record["learning_strength"])
        kind = FeedbackKind(record["kind"])
        partition = str(
            record.get("subject_key") or record.get("scope_key") or "global"
        )
        capability = str(record.get("capability"))
        source = str(record.get("source"))
        episode = str(record.get("episode_key") or record["event_key"])
        source_episode = (partition, capability, source, episode)
        admitted = raw_strength
        if kind in LEARNING_KINDS:
            ingested_at = parse_timestamp(
                str(record.get("ingested_at") or record["observed_at"])
            )
            polarity = "adverse" if kind is FeedbackKind.ADVERSE else "healing"
            day_key = (
                partition,
                capability,
                source,
                ingested_at.date().isoformat(),
                polarity,
            )
            daily_remaining = max(0.0, DAILY_CONTRIBUTION_CAP - daily_mass[day_key])
            if kind is FeedbackKind.ADVERSE:
                episode_remaining = max(
                    0.0,
                    EPISODE_CONTRIBUTION_CAP - adverse_episode_mass[source_episode],
                )
                admitted = min(raw_strength, episode_remaining, daily_remaining)
                adverse_episode_mass[source_episode] += admitted
            elif kind in HEALING_KINDS:
                episode_remaining = max(
                    0.0,
                    min(
                        EPISODE_CONTRIBUTION_CAP - healing_episode_mass[source_episode],
                        adverse_episode_mass[source_episode]
                        - healing_episode_mass[source_episode],
                    ),
                )
                admitted = min(raw_strength, episode_remaining, daily_remaining)
                healing_episode_mass[source_episode] += admitted
            daily_mass[day_key] += admitted
        strength = admitted * memory
        target = _risk_target(record)
        features = record.get("features")
        if target is not None and isinstance(features, Mapping):
            cue_weights = update_cue_weights(
                cue_weights,
                features,
                target=target,
                strength=strength,
                config=config,
            )

        reward = float(record.get("reward", 0.0))
        for strategy_name in record.get("strategies", ()):
            strategy = StrategyName(strategy_name)
            baseline = config.strategy_baselines[strategy]
            minimum = baseline - config.strategy_adaptive_span
            maximum = baseline + config.strategy_adaptive_span
            delta = config.strategy_learning_rate * reward * strength
            strategy_weights[strategy.value] = max(
                minimum,
                min(maximum, strategy_weights[strategy.value] + delta),
            )

        positive = max(0.0, reward) * strength * 0.02
        if StrategyName.HOLD_BOUNDARY.value in record.get("strategies", ()):
            capacities["boundary"] = min(0.9, capacities["boundary"] + positive)
        if kind is FeedbackKind.REPAIR:
            capacities["connection"] = min(
                0.9, capacities["connection"] + 0.02 * strength
            )
            capacities["recovery"] = min(0.9, capacities["recovery"] + 0.01 * strength)
        if StrategyName.ORIENT_AND_VERIFY.value in record.get("strategies", ()):
            capacities["uncertainty_tolerance"] = min(
                0.9, capacities["uncertainty_tolerance"] + positive
            )
        if reward > 0:
            capacities["regulation"] = min(
                0.9, capacities["regulation"] + 0.005 * strength
            )
    return cue_weights, strategy_weights, capacities
