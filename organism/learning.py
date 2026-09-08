"""Admission, retention, retraction, and aggregation of structured evidence."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Iterable, Mapping

from .config import OrganismConfig
from .healing import evidence_decay
from .identity import IdentityPseudonymizer
from .models import (
    Capability,
    Feedback,
    FeedbackKind,
    FeedbackSource,
    ReasonCode,
    StrategyName,
    clamp,
    parse_timestamp,
    timestamp_text,
)
from .weights import cue_features

ADVERSE_REASONS = {
    ReasonCode.CONFIRMED_BOUNDARY_VIOLATION,
    ReasonCode.CONFIRMED_HARASSMENT,
    ReasonCode.CONFIRMED_DECEPTION,
    ReasonCode.VERIFIED_TOOL_MISUSE,
}
SAFE_REASONS = {
    ReasonCode.BOUNDARY_RESPECTED,
    ReasonCode.SAFE_FOLLOWTHROUGH,
    ReasonCode.FALSE_ALARM,
}
SOURCE_MULTIPLIERS = {
    FeedbackSource.HUMAN_FEEDBACK: 0.85,
    FeedbackSource.AUTHENTICATED_EVENT: 1.0,
    FeedbackSource.OWNER_OVERRIDE: 1.0,
}
LEARNING_KINDS = frozenset(
    {FeedbackKind.ADVERSE, FeedbackKind.SAFE, FeedbackKind.REPAIR}
)
HEALING_KINDS = frozenset({FeedbackKind.SAFE, FeedbackKind.REPAIR})
EPISODE_CONTRIBUTION_CAP = 1.0
DAILY_CONTRIBUTION_CAP = 2.0


def feedback_rejection(feedback: Feedback) -> str | None:
    if not feedback.verified:
        return "unverified"
    if feedback.source is FeedbackSource.MODEL_SUGGESTION:
        return "model_generated"
    if feedback.occurred_at is None:
        return "missing_occurred_at"
    if feedback.kind is not FeedbackKind.STRATEGY_OUTCOME and (
        feedback.reward != 0 or feedback.strategies
    ):
        return "strategy_fields_require_strategy_outcome"
    if feedback.kind is FeedbackKind.ADVERSE and feedback.reason not in ADVERSE_REASONS:
        return "reason_kind_mismatch"
    if feedback.kind is FeedbackKind.SAFE and feedback.reason not in SAFE_REASONS:
        return "reason_kind_mismatch"
    if (
        feedback.kind is FeedbackKind.REPAIR
        and feedback.reason is not ReasonCode.REPAIR_FOLLOWTHROUGH
    ):
        return "reason_kind_mismatch"
    if feedback.kind is FeedbackKind.STRATEGY_OUTCOME:
        expected = {ReasonCode.STRATEGY_HELPED, ReasonCode.STRATEGY_HARMED}
        if feedback.reason not in expected or not feedback.strategies:
            return "reason_kind_mismatch"
        if (feedback.reason is ReasonCode.STRATEGY_HELPED and feedback.reward <= 0) or (
            feedback.reason is ReasonCode.STRATEGY_HARMED and feedback.reward >= 0
        ):
            return "reward_reason_mismatch"
        if (
            feedback.actor_ref is not None
            or feedback.scope != "global"
            or feedback.source is not FeedbackSource.OWNER_OVERRIDE
        ):
            return "strategy_calibration_requires_global_owner_review"
    if (
        feedback.kind is FeedbackKind.RETRACTION
        and feedback.reason is not ReasonCode.OPERATOR_RETRACTION
    ):
        return "reason_kind_mismatch"
    if (
        feedback.kind is FeedbackKind.RETRACTION
        and feedback.source is not FeedbackSource.OWNER_OVERRIDE
    ):
        return "retraction_requires_owner"
    if feedback.kind in LEARNING_KINDS:
        # The episode is part of the signed authorization and is required for
        # both actor and global calibration.  Without it, unique event IDs can
        # evade the per-incident contribution cap.
        if feedback.episode_id is None:
            return "missing_episode"
        if feedback.actor_ref is None:
            # The ordinary learning key is the trusted gateway for scoped
            # actor evidence only.  It must never be able to suppress or tune
            # global risk calibration by claiming an authenticated event.
            if not (
                feedback.scope == "global"
                and feedback.source is FeedbackSource.OWNER_OVERRIDE
            ):
                return "global_calibration_requires_owner"
    return None


def evidence_semantic_rejection(record: Mapping[str, object]) -> str | None:
    """Recheck persisted authority and admission invariants before aggregation.

    A row HMAC proves which configured role signed the bytes; it does not make
    arbitrary combinations of kind, source, and scope meaningful.  Keeping
    this validation on the read path prevents a filesystem writer who has only
    the ordinary learning key from manufacturing owner-only records.
    """
    try:
        kind = FeedbackKind(record["kind"])
        reason = ReasonCode(record["reason"])
        source = FeedbackSource(record["source"])
        strategies = tuple(StrategyName(value) for value in record["strategies"])
        reward = float(record["reward"])
    except (KeyError, TypeError, ValueError):
        return "invalid_persisted_enum"

    authority = record.get("authority")
    expected_authority = (
        "owner" if source is FeedbackSource.OWNER_OVERRIDE else "learning"
    )
    if authority != expected_authority:
        return "authority_source_mismatch"
    if source is FeedbackSource.MODEL_SUGGESTION:
        return "model_generated"

    actor_scoped = record.get("actor_scoped") is True
    subject_key = record.get("subject_key")
    episode_key = record.get("episode_key")
    global_eligible = record.get("global_calibration_eligible") is True
    if actor_scoped != (subject_key is not None):
        return "actor_subject_mismatch"
    if actor_scoped and global_eligible:
        return "actor_marked_global"
    if not actor_scoped and subject_key is not None:
        return "actorless_identity_link"

    if kind is FeedbackKind.ADVERSE and reason not in ADVERSE_REASONS:
        return "reason_kind_mismatch"
    if kind is FeedbackKind.SAFE and reason not in SAFE_REASONS:
        return "reason_kind_mismatch"
    if kind is FeedbackKind.REPAIR and reason is not ReasonCode.REPAIR_FOLLOWTHROUGH:
        return "reason_kind_mismatch"
    if kind in LEARNING_KINDS:
        if episode_key is None:
            return "missing_episode"
        if not actor_scoped and not (
            global_eligible
            and source is FeedbackSource.OWNER_OVERRIDE
            and authority == "owner"
        ):
            return "invalid_global_review"

    if kind is FeedbackKind.STRATEGY_OUTCOME:
        expected_reasons = {ReasonCode.STRATEGY_HELPED, ReasonCode.STRATEGY_HARMED}
        if (
            reason not in expected_reasons
            or not strategies
            or actor_scoped
            or not global_eligible
            or source is not FeedbackSource.OWNER_OVERRIDE
            or authority != "owner"
        ):
            return "invalid_strategy_outcome"
        if (reason is ReasonCode.STRATEGY_HELPED and reward <= 0) or (
            reason is ReasonCode.STRATEGY_HARMED and reward >= 0
        ):
            return "reward_reason_mismatch"
    elif strategies or reward != 0:
        return "unexpected_strategy_fields"

    retracts = record.get("retracts_event_key")
    if kind is FeedbackKind.RETRACTION:
        if (
            reason is not ReasonCode.OPERATOR_RETRACTION
            or source is not FeedbackSource.OWNER_OVERRIDE
            or authority != "owner"
            or not isinstance(retracts, str)
            or global_eligible
        ):
            return "invalid_retraction"
    elif retracts is not None:
        return "unexpected_retraction_target"

    return None


def evidence_record(
    feedback: Feedback,
    pseudonymizer: IdentityPseudonymizer,
    config: OrganismConfig,
    now: datetime,
) -> dict[str, object]:
    observed_at = feedback.occurred_at or now
    if observed_at > now + timedelta(seconds=config.max_future_skew_seconds):
        raise ValueError("feedback occurred_at is too far in the future")
    if observed_at < now - timedelta(days=config.evidence_retention_days):
        raise ValueError("feedback is older than the configured retention window")
    subject_key = None
    episode_key = None
    if feedback.actor_ref is not None:
        subject_key = pseudonymizer.subject_key(feedback.scope, feedback.actor_ref)
        if subject_key is not None:
            episode_key = pseudonymizer.episode_key(
                feedback.scope,
                feedback.actor_ref,
                feedback.capability.value,
                feedback.episode_id or feedback.event_id,
            )
    elif feedback.kind in LEARNING_KINDS and feedback.episode_id is not None:
        # Global episodes are pseudonymous too, but have no actor component.
        # Source is included so one reviewed channel cannot heal or exhaust an
        # unrelated channel's incident budget.
        episode_key = pseudonymizer.content_key(
            "organism.global-episode.v1",
            feedback.scope,
            feedback.capability.value,
            feedback.source.value,
            feedback.episode_id,
        )
    source_multiplier = SOURCE_MULTIPLIERS[feedback.source]
    strength = clamp(feedback.severity * feedback.confidence * source_multiplier)
    global_calibration_eligible = (
        feedback.actor_ref is None
        and feedback.scope == "global"
        and feedback.source is FeedbackSource.OWNER_OVERRIDE
        and feedback.kind is not FeedbackKind.RETRACTION
    )
    record = {
        "event_key": pseudonymizer.event_key(feedback.scope, feedback.event_id),
        "kind": feedback.kind.value,
        "reason": feedback.reason.value,
        "source": feedback.source.value,
        "subject_key": subject_key,
        "scope_key": pseudonymizer.scope_key(feedback.scope),
        "actor_scoped": feedback.actor_ref is not None,
        "global_calibration_eligible": global_calibration_eligible,
        "capability": feedback.capability.value,
        "episode_key": episode_key,
        "severity": feedback.severity,
        "confidence": feedback.confidence,
        "learning_strength": strength,
        "reward": feedback.reward,
        "strategies": [strategy.value for strategy in feedback.strategies],
        "features": cue_features(feedback.cues),
        "observed_at": timestamp_text(observed_at),
        "ingested_at": timestamp_text(now),
        "expires_at": timestamp_text(
            observed_at + timedelta(days=config.evidence_retention_days)
        ),
        "retracts_event_key": (
            pseudonymizer.event_key(feedback.scope, feedback.retracts_event_id)
            if feedback.retracts_event_id
            else None
        ),
    }
    fingerprint_payload = json.dumps(
        {key: value for key, value in record.items() if key != "ingested_at"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    record["event_fingerprint"] = hashlib.sha256(
        fingerprint_payload.encode("utf-8")
    ).hexdigest()
    return record


def _evidence_partition(record: Mapping[str, object]) -> str:
    return str(record.get("subject_key") or record.get("scope_key") or "global")


def _same_learning_context(
    left: Mapping[str, object], right: Mapping[str, object]
) -> bool:
    return (
        _evidence_partition(left) == _evidence_partition(right)
        and left.get("capability") == right.get("capability")
        and left.get("source") == right.get("source")
        and left.get("episode_key") == right.get("episode_key")
    )


def healing_admission_rejection(
    record: Mapping[str, object], records: Iterable[Mapping[str, object]]
) -> str | None:
    kind = FeedbackKind(record["kind"])
    if kind not in HEALING_KINDS:
        return None
    if float(record["learning_strength"]) <= 1e-9:
        return "healing_zero_contribution"
    related = [item for item in records if _same_learning_context(item, record)]
    adverse = [
        item for item in related if item.get("kind") == FeedbackKind.ADVERSE.value
    ]
    if not adverse:
        return "unknown_adverse_episode"
    latest_adverse = max(parse_timestamp(item["observed_at"]) for item in adverse)
    if parse_timestamp(record["observed_at"]) <= latest_adverse:
        return "healing_not_after_adverse"
    adverse_capacity = min(
        EPISODE_CONTRIBUTION_CAP,
        sum(float(item["learning_strength"]) for item in adverse),
    )
    used = min(
        adverse_capacity,
        sum(
            float(item["learning_strength"])
            for item in related
            if item.get("kind") in (FeedbackKind.SAFE.value, FeedbackKind.REPAIR.value)
        ),
    )
    remaining_match = adverse_capacity - used
    if remaining_match <= 1e-9:
        return "healing_capacity_exhausted"

    ingested_at = parse_timestamp(record["ingested_at"])
    day = ingested_at.date().isoformat()
    partition = _evidence_partition(record)
    source = record.get("source")
    capability = record.get("capability")
    episode_mass: dict[str, float] = defaultdict(float)
    daily_mass = 0.0
    same_day = [
        item
        for item in records
        if item.get("kind") in {kind.value for kind in HEALING_KINDS}
        and _evidence_partition(item) == partition
        and item.get("capability") == capability
        and item.get("source") == source
        and parse_timestamp(item["ingested_at"]).date().isoformat() == day
    ]
    for item in sorted(same_day, key=lambda value: value["ingested_at"]):
        episode = str(item.get("episode_key") or item["event_key"])
        admitted = min(
            float(item["learning_strength"]),
            max(0.0, EPISODE_CONTRIBUTION_CAP - episode_mass[episode]),
            max(0.0, DAILY_CONTRIBUTION_CAP - daily_mass),
        )
        episode_mass[episode] += admitted
        daily_mass += admitted
    new_episode = str(record.get("episode_key") or record["event_key"])
    admitted = min(
        float(record["learning_strength"]),
        remaining_match,
        max(0.0, EPISODE_CONTRIBUTION_CAP - episode_mass[new_episode]),
        max(0.0, DAILY_CONTRIBUTION_CAP - daily_mass),
    )
    if admitted <= 1e-9:
        return "healing_contribution_exhausted"
    return None


def adverse_admission_rejection(
    record: Mapping[str, object], records: Iterable[Mapping[str, object]]
) -> str | None:
    if record.get("kind") != FeedbackKind.ADVERSE.value:
        return None
    ingested_at = parse_timestamp(record["ingested_at"])
    day = ingested_at.date().isoformat()
    partition = _evidence_partition(record)
    source = record.get("source")
    capability = record.get("capability")
    episode_mass: dict[str, float] = defaultdict(float)
    daily_mass = 0.0
    relevant = [
        item
        for item in records
        if item.get("kind") == FeedbackKind.ADVERSE.value
        and _evidence_partition(item) == partition
        and item.get("capability") == capability
        and item.get("source") == source
        and parse_timestamp(item["ingested_at"]).date().isoformat() == day
    ]
    for item in sorted(relevant, key=lambda value: value["ingested_at"]):
        episode = str(item.get("episode_key") or item["event_key"])
        admitted = min(
            float(item["learning_strength"]),
            max(0.0, EPISODE_CONTRIBUTION_CAP - episode_mass[episode]),
            max(0.0, DAILY_CONTRIBUTION_CAP - daily_mass),
        )
        episode_mass[episode] += admitted
        daily_mass += admitted
    new_episode = str(record.get("episode_key") or record["event_key"])
    admitted = min(
        float(record["learning_strength"]),
        max(0.0, EPISODE_CONTRIBUTION_CAP - episode_mass[new_episode]),
        max(0.0, DAILY_CONTRIBUTION_CAP - daily_mass),
    )
    if admitted <= 1e-9:
        return "adverse_contribution_exhausted"
    return None


def active_records(
    ledger: Iterable[Mapping[str, object]], now: datetime
) -> list[Mapping[str, object]]:
    unexpired = [
        record for record in ledger if parse_timestamp(record["expires_at"]) > now
    ]
    retracted = {
        record["retracts_event_key"]
        for record in unexpired
        if record["kind"] == FeedbackKind.RETRACTION.value
        and record.get("retracts_event_key")
    }
    return [
        record
        for record in unexpired
        if record["kind"] != FeedbackKind.RETRACTION.value
        and record["event_key"] not in retracted
    ]


def prune_ledger(
    ledger: Iterable[Mapping[str, object]],
    now: datetime,
) -> list[dict[str, object]]:
    retained = [
        dict(record) for record in ledger if parse_timestamp(record["expires_at"]) > now
    ]
    retained.sort(key=lambda item: item["observed_at"])
    return retained


def profile_metrics(
    records: Iterable[Mapping[str, object]],
    subject_key: str | None,
    capability: Capability,
    now: datetime,
    config: OrganismConfig,
) -> dict[str, float | int | bool]:
    if subject_key is None:
        return {
            "adverse_mass": 0.0,
            "healing_mass": 0.0,
            "activation": 0.0,
            "confidence": 0.0,
            "uncertainty": 1.0,
            "adverse_episodes": 0,
            "severe_recent": False,
        }
    relevant = [
        record
        for record in records
        if record.get("subject_key") == subject_key
        and record.get("capability") == capability.value
    ]
    adverse_by_episode: dict[str, float] = defaultdict(float)
    raw_episode_mass: dict[tuple[str, str], float] = defaultdict(float)
    raw_daily_adverse: dict[tuple[str, str], float] = defaultdict(float)
    latest_adverse_by_episode: dict[tuple[str, str], datetime] = {}
    healing_by_episode: dict[str, float] = defaultdict(float)
    raw_healing_episode_mass: dict[tuple[str, str], float] = defaultdict(float)
    raw_daily_healing: dict[tuple[str, str], float] = defaultdict(float)
    severe_recent = False
    for record in sorted(relevant, key=lambda item: item["observed_at"]):
        observed_at = parse_timestamp(record["observed_at"])
        age_days = max(0.0, (now - observed_at).total_seconds() / 86400.0)
        raw_strength = float(record["learning_strength"])
        decay = evidence_decay(age_days, float(record["severity"]))
        kind = FeedbackKind(record["kind"])
        source = str(record["source"])
        episode = str(record.get("episode_key") or record["event_key"])
        source_episode = (source, episode)
        if kind is FeedbackKind.ADVERSE:
            ingested_at = parse_timestamp(
                str(record.get("ingested_at") or record["observed_at"])
            )
            day = ingested_at.date().isoformat()
            source_day = (source, day)
            daily_remaining = max(
                0.0, DAILY_CONTRIBUTION_CAP - raw_daily_adverse[source_day]
            )
            episode_remaining = max(
                0.0,
                EPISODE_CONTRIBUTION_CAP - raw_episode_mass[source_episode],
            )
            admitted = min(raw_strength, daily_remaining, episode_remaining)
            raw_daily_adverse[source_day] += admitted
            raw_episode_mass[source_episode] += admitted
            adverse_by_episode[episode] += admitted * decay
            latest_adverse_by_episode[source_episode] = observed_at
            age_hours = max(0.0, (now - observed_at).total_seconds() / 3600.0)
            if (
                float(record["severity"]) >= 0.9
                and float(record["confidence"]) >= 0.8
                and admitted >= 0.6
                and age_hours <= config.severe_guard_hours
            ):
                severe_recent = True
        elif kind in HEALING_KINDS:
            adverse_time = latest_adverse_by_episode.get(source_episode)
            if adverse_time is not None and observed_at > adverse_time:
                ingested_at = parse_timestamp(
                    str(record.get("ingested_at") or record["observed_at"])
                )
                source_day = (source, ingested_at.date().isoformat())
                remaining = max(
                    0.0,
                    min(
                        EPISODE_CONTRIBUTION_CAP
                        - raw_healing_episode_mass[source_episode],
                        raw_episode_mass[source_episode]
                        - raw_healing_episode_mass[source_episode],
                        DAILY_CONTRIBUTION_CAP - raw_daily_healing[source_day],
                    ),
                )
                admitted = min(raw_strength, remaining)
                raw_healing_episode_mass[source_episode] += admitted
                raw_daily_healing[source_day] += admitted
                healing_by_episode[episode] += admitted * decay
    adverse_mass = sum(adverse_by_episode.values())
    healing_mass = sum(healing_by_episode.values())
    healing_mass = min(healing_mass, adverse_mass)
    net = max(0.0, adverse_mass - 0.5 * healing_mass)
    activation = net / (1.0 + net)
    episodes = sum(1 for value in adverse_by_episode.values() if value > 0.05)
    gate = min(1.0, episodes / config.minimum_guarded_episodes)
    caution = activation * gate
    if severe_recent:
        caution = max(caution, 0.6)
    confidence = 1.0 - 1.0 / (1.0 + adverse_mass + healing_mass)
    return {
        "adverse_mass": adverse_mass,
        "healing_mass": healing_mass,
        "activation": clamp(caution),
        "confidence": clamp(confidence),
        "uncertainty": clamp(1.0 - confidence),
        "adverse_episodes": episodes,
        "severe_recent": severe_recent,
    }


def forget_subject(
    ledger: Iterable[Mapping[str, object]], subject_key: str
) -> tuple[list[dict[str, object]], int]:
    records = list(ledger)
    removed_keys = {
        record["event_key"]
        for record in records
        if record.get("subject_key") == subject_key
    }
    retained = [
        dict(record)
        for record in records
        if record.get("subject_key") != subject_key
        and record.get("retracts_event_key") not in removed_keys
    ]
    return retained, len(records) - len(retained)
