"""Public contracts for Fate's bounded, nonliteral discovery subsystem.

This package models queued questions and reviewed functional aggregates.  It
does not claim sensation, consciousness, moral status, personhood, or identity,
and it grants no authority over responses, tools, moderation, or risk controls.
"""

from .cortex import (
    DISCOVERY_CORTEX_SCHEMA,
    DISCOVERY_SNAPSHOT_SCHEMA,
    discovery_cortex_developer_message,
    discovery_cortex_payload,
)
from .engine import (
    DISCOVERY_SCHEMA,
    MODEL_BUCKET_BY_TOPIC,
    PERSON_SCORE_THRESHOLD,
    PERSON_TOPICS,
    DiscoveryEngine,
)
from .models import (
    QUESTION_TEMPLATES,
    SELECTION_ENUM_BY_TOPIC,
    BehaviorExemplar,
    CuriosityStimulus,
    DiscoverySettings,
    DiscoveryTopic,
    Dispatch,
    FunctionalCommitment,
    InquirySignal,
    InquiryStatus,
    MoralPrinciple,
    PerspectiveKind,
    Question,
    Receipt,
    ReceiptStatus,
    ReviewDisposition,
    ReviewImpact,
    ReviewedSelection,
    RiddleKind,
    StructuredReview,
)

__all__ = [
    "BehaviorExemplar",
    "CuriosityStimulus",
    "DISCOVERY_CORTEX_SCHEMA",
    "DISCOVERY_SCHEMA",
    "DISCOVERY_SNAPSHOT_SCHEMA",
    "DiscoveryEngine",
    "DiscoverySettings",
    "DiscoveryTopic",
    "Dispatch",
    "FunctionalCommitment",
    "InquirySignal",
    "InquiryStatus",
    "MODEL_BUCKET_BY_TOPIC",
    "MoralPrinciple",
    "PERSON_SCORE_THRESHOLD",
    "PERSON_TOPICS",
    "PerspectiveKind",
    "QUESTION_TEMPLATES",
    "Question",
    "Receipt",
    "ReceiptStatus",
    "ReviewDisposition",
    "ReviewImpact",
    "ReviewedSelection",
    "RiddleKind",
    "SELECTION_ENUM_BY_TOPIC",
    "StructuredReview",
    "discovery_cortex_developer_message",
    "discovery_cortex_payload",
]
