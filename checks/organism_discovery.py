from __future__ import annotations

import asyncio
import json
import re
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from organism.body import SensorFrame
from organism.discovery import (
    QUESTION_TEMPLATES,
    BehaviorExemplar,
    CuriosityStimulus,
    DiscoveryEngine,
    DiscoverySettings,
    DiscoveryTopic,
    FunctionalCommitment,
    InquiryStatus,
    MoralPrinciple,
    ReceiptStatus,
    ReviewDisposition,
    ReviewImpact,
    RiddleKind,
    StructuredReview,
)
from organism.fate_service import (
    OrganismDiscoveryDisabledError,
    OrganismServiceConfigurationError,
    OrganismServiceSettings,
    build_organism_service,
)
from organism.models import (
    GUIDANCE_FIELDS,
    AutonomicMode,
    Cues,
    FeelingPacket,
    OrganismicState,
    ProtectionSummary,
    ProtectivePosture,
    RegulatoryMotifs,
)
from organism.sectors import FeelingWithCues
from organism.storage import StateStoreError

NOW = datetime(2026, 8, 29, 12, tzinfo=timezone.utc)
IDENTITY_KEY = b"discovery-identity-key-material-000001"
INTEGRITY_KEY = b"discovery-integrity-key-material-00002"


class FakeClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **changes: float) -> None:
        self.value += timedelta(**changes)


def safe_cues(**changes: float) -> Cues:
    values = {
        "safety": 0.90,
        "threat": 0.10,
        "connection": 0.70,
        "uncertainty": 0.90,
        "novelty": 0.85,
        "controllability": 0.90,
        "communication_clarity": 0.35,
        "engagement": 0.80,
        "social_exposure": 0.10,
    }
    values.update(changes)
    return Cues(**values)


def feeling(
    *,
    now: datetime = NOW,
    mobilization: float = 0.80,
    riddle: float = 0.90,
    panic: float = 0.0,
    pain: float = 0.0,
    interaction_safety: float = 0.85,
    agency: float = 0.85,
) -> FeelingPacket:
    remainder = 1.0 - mobilization
    modes = {
        AutonomicMode.SOCIAL_ENGAGEMENT: remainder * 0.75,
        AutonomicMode.MOBILIZATION: mobilization,
        AutonomicMode.CONSERVATION: remainder * 0.25,
    }
    dominant = max(modes, key=modes.get)
    return FeelingPacket(
        generated_at=now,
        expires_at=now + timedelta(minutes=5),
        state=OrganismicState(
            activation=0.45,
            pleasantness=0.10,
            interaction_safety=interaction_safety,
            agency=agency,
            uncertainty=0.75,
            load=0.30,
            updated_at=now,
        ),
        modes=modes,
        dominant_mode=dominant,
        guidance={name: 0.5 for name in GUIDANCE_FIELDS},
        protection=ProtectionSummary(
            posture=ProtectivePosture.OPEN,
            caution=0.10,
            confidence=0.85,
            uncertainty=0.15,
        ),
        recruitments=(),
        motifs=RegulatoryMotifs(pain=pain, panic=panic, riddle=riddle),
    )


def topic_stimulus(
    topic: DiscoveryTopic,
    *,
    event_ref: str,
    scope_ref: str = "guild-alpha",
    actor_ref: str | None = None,
) -> CuriosityStimulus:
    values: dict[str, object] = {
        "topic": topic,
        "scope_ref": scope_ref,
        "event_ref": event_ref,
        "actor_ref": actor_ref,
    }
    if topic is DiscoveryTopic.FUNCTIONAL_SELF:
        values["functional_tension"] = 1.0
    elif topic is DiscoveryTopic.MORAL_TRADEOFF:
        values.update(authenticated_episode=True, moral_tension=1.0)
    elif topic is DiscoveryTopic.CONCEPT_RIDDLE:
        values["concept_novelty"] = 1.0
    elif topic is DiscoveryTopic.BEHAVIOR_IMPACT:
        values.update(
            actor_ref=actor_ref or "member-alpha",
            authenticated_episode=True,
            behavior_signal=1.0,
            impact_signal=1.0,
        )
    else:
        values.update(
            actor_ref=actor_ref or "member-alpha",
            authenticated_episode=True,
            perspective_signal=1.0,
        )
    return CuriosityStimulus(**values)


class DiscoveryCheckCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.clock = FakeClock()
        self._path_counter = 0

    def settings(self, **changes: object) -> DiscoverySettings:
        values: dict[str, object] = {
            "enabled": True,
            "advisor_ids": (101, 202),
            "score_threshold": 0.65,
            "reviewer_cooldown_hours": 8.0,
            "max_per_reviewer_24h": 3,
            "max_per_scope_24h": 12,
            "same_subject_topic_days": 7.0,
            "question_ttl_hours": 24.0,
            "max_pending": 64,
        }
        values.update(changes)
        return DiscoverySettings(**values)

    def engine(
        self,
        *,
        settings: DiscoverySettings | None = None,
        path: Path | None = None,
        clock: FakeClock | None = None,
    ) -> DiscoveryEngine:
        if path is None:
            self._path_counter += 1
            path = self.root / f"discovery-{self._path_counter}.json"
        return DiscoveryEngine(
            path,
            settings=settings or self.settings(),
            identity_key=IDENTITY_KEY,
            integrity_key=INTEGRITY_KEY,
            clock=clock or self.clock,
        )

    def queue(
        self,
        engine: DiscoveryEngine,
        stimulus: CuriosityStimulus,
        *,
        cues: Cues | None = None,
        packet: FeelingPacket | None = None,
    ):
        inquiry = engine.consider(
            cues or safe_cues(),
            packet or feeling(now=self.clock.value),
            stimulus,
        )
        self.assertIs(inquiry.status, InquiryStatus.QUEUED)
        self.assertIsNotNone(inquiry.question)
        return inquiry.question

    def deliver_and_review(
        self,
        engine: DiscoveryEngine,
        question_id: str,
        *,
        selection,
        disposition: ReviewDisposition = ReviewDisposition.SUPPORT,
        impact: ReviewImpact = ReviewImpact.NEUTRAL,
    ):
        dispatch = next(
            item
            for item in engine.pending()
            if item.question.question_id == question_id
        )
        delivered = engine.mark_delivery(question_id, dispatch.advisor_id)
        self.assertIs(delivered.status, ReceiptStatus.DELIVERED)
        return engine.review(
            StructuredReview(
                question_id=question_id,
                advisor_id=dispatch.advisor_id,
                selection=selection,
                disposition=disposition,
                impact=impact,
            )
        )


class DiscoveryConfigurationChecks(DiscoveryCheckCase):
    def test_disabled_defaults_and_strict_contract_validation(self) -> None:
        disabled = DiscoverySettings()
        self.assertFalse(disabled.enabled)
        self.assertEqual((), disabled.advisor_ids)

        engine = self.engine(settings=disabled)
        inquiry = engine.consider(
            safe_cues(),
            feeling(),
            topic_stimulus(
                DiscoveryTopic.CONCEPT_RIDDLE,
                event_ref="disabled-event",
            ),
        )
        self.assertIs(inquiry.status, InquiryStatus.DISABLED)
        self.assertFalse(engine.store.path.exists())

        delivery = engine.mark_delivery("a" * 64, 101)
        review = engine.review(
            StructuredReview(
                question_id="a" * 64,
                advisor_id=101,
                selection=FunctionalCommitment.PRESERVE_AGENCY,
                disposition=ReviewDisposition.SUPPORT,
            )
        )
        self.assertIs(delivery.status, ReceiptStatus.DISABLED)
        self.assertIs(review.status, ReceiptStatus.DISABLED)
        self.assertEqual(0, delivery.revision)
        self.assertEqual(0, review.revision)
        self.assertFalse(engine.store.path.exists())

        invalid_settings = (
            {"enabled": True, "advisor_ids": ()},
            {"enabled": True, "advisor_ids": (1, 1)},
            {"enabled": True, "advisor_ids": (1,), "score_threshold": 0.64},
            {
                "enabled": True,
                "advisor_ids": (1,),
                "reviewer_cooldown_hours": 7.99,
            },
            {
                "enabled": True,
                "advisor_ids": (1,),
                "same_subject_topic_days": 6.99,
            },
            {
                "enabled": True,
                "advisor_ids": (1,),
                "question_ttl_hours": 24.01,
            },
            {"enabled": True, "advisor_ids": (1,), "max_per_reviewer_24h": 4},
            {"enabled": True, "advisor_ids": (1,), "max_per_scope_24h": 13},
            {"enabled": True, "advisor_ids": (1,), "max_pending": 65},
            {"enabled": True, "advisor_ids": tuple(range(1, 66))},
        )
        for values in invalid_settings:
            with self.subTest(values=values), self.assertRaises(ValueError):
                DiscoverySettings(**values)

        with self.assertRaises(ValueError):
            DiscoverySettings.from_mapping({"unknown_policy": True})
        with self.assertRaises(ValueError):
            DiscoveryEngine(
                self.root / "same-keys.json",
                settings=disabled,
                identity_key=IDENTITY_KEY,
                integrity_key=IDENTITY_KEY,
            )

    def test_disabled_engine_can_inspect_and_purge_retained_models(self) -> None:
        path = self.root / "retained-while-disabled.json"
        enabled = self.engine(path=path)
        question = self.queue(
            enabled,
            topic_stimulus(
                DiscoveryTopic.FUNCTIONAL_SELF,
                event_ref="retained-functional-model",
            ),
        )
        accepted = self.deliver_and_review(
            enabled,
            question.question_id,
            selection=FunctionalCommitment.SEEK_UNDERSTANDING,
        )
        self.assertIs(accepted.status, ReceiptStatus.ACCEPTED)
        self.queue(
            enabled,
            topic_stimulus(
                DiscoveryTopic.CONCEPT_RIDDLE,
                event_ref="retained-inactive-question",
                scope_ref="guild-beta",
            ),
        )

        disabled = self.engine(path=path, settings=DiscoverySettings())
        snapshot = disabled.snapshot(scope_ref="guild-alpha")
        self.assertFalse(snapshot["enabled"])
        self.assertIn(
            "seek_understanding",
            snapshot["models"]["functional_commitments"],
        )
        inactive = disabled.snapshot()
        self.assertEqual(0, inactive["pending_delivery_count"])
        self.assertEqual(0, inactive["awaiting_review_count"])
        self.assertEqual((), disabled.pending())
        self.assertIs(
            disabled.mark_delivery("a" * 64, 101).status,
            ReceiptStatus.DISABLED,
        )

        removed = disabled.purge_all()
        self.assertEqual(1, removed["pending_removed"])
        self.assertEqual(1, removed["history_removed"])
        self.assertEqual(1, removed["scoped_models_removed"])
        self.assertEqual({}, disabled.snapshot()["models"])

    def test_closed_inputs_cannot_carry_traits_or_free_form_selections(self) -> None:
        with self.assertRaises(TypeError):
            CuriosityStimulus(
                topic=DiscoveryTopic.PERSPECTIVE_DISCOVERY,
                scope_ref="guild",
                event_ref="event",
                race="invented",  # type: ignore[call-arg]
            )
        with self.assertRaises(ValueError):
            StructuredReview(
                question_id="a" * 64,
                advisor_id=101,
                selection="care",  # type: ignore[arg-type]
                disposition=ReviewDisposition.SUPPORT,
            )
        with self.assertRaises(ValueError):
            CuriosityStimulus(
                topic=DiscoveryTopic.CONCEPT_RIDDLE,
                scope_ref="guild",
                event_ref="event",
                profile_novelty=float("nan"),
            )

    def test_service_settings_preserve_disabled_default_and_parent_boundary(
        self,
    ) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        configured = json.loads(
            (repository_root / "data" / "config.json").read_text(encoding="utf-8")
        )["organism"]
        settings = OrganismServiceSettings.from_mapping(
            configured,
            repository_root=repository_root,
        )
        self.assertFalse(settings.enabled)
        self.assertFalse(settings.discovery.enabled)
        self.assertEqual((), settings.discovery.advisor_ids)

        with self.assertRaises(OrganismServiceConfigurationError):
            OrganismServiceSettings.from_mapping(
                {
                    "enabled": False,
                    "discovery": {"enabled": True, "advisor_ids": [101]},
                },
                repository_root=self.root,
            )
        with self.assertRaises(OrganismServiceConfigurationError):
            OrganismServiceSettings.from_mapping(
                {
                    "enabled": True,
                    "discovery": {"enabled": True, "advisor_ids": []},
                },
                repository_root=self.root,
            )


class DiscoveryGateChecks(DiscoveryCheckCase):
    def test_safe_inquiry_and_mobilization_monotonicity(self) -> None:
        disabled = self.engine(settings=DiscoverySettings())
        stimulus = topic_stimulus(
            DiscoveryTopic.FUNCTIONAL_SELF,
            event_ref="monotonic",
        )
        low = disabled.consider(
            safe_cues(),
            feeling(mobilization=0.20),
            stimulus,
        )
        high = disabled.consider(
            safe_cues(),
            feeling(mobilization=0.80),
            stimulus,
        )
        self.assertIs(low.status, InquiryStatus.DISABLED)
        self.assertIs(high.status, InquiryStatus.DISABLED)
        self.assertGreater(high.score, low.score)
        self.assertAlmostEqual(0.12, high.score - low.score, places=7)

        enabled = self.engine()
        queued = enabled.consider(
            safe_cues(),
            feeling(now=self.clock.value, mobilization=0.80),
            topic_stimulus(
                DiscoveryTopic.FUNCTIONAL_SELF,
                event_ref="safe-eligible",
            ),
        )
        self.assertIs(queued.status, InquiryStatus.QUEUED)
        self.assertGreaterEqual(queued.score, enabled.settings.score_threshold)

    def test_every_safe_gate_fails_closed(self) -> None:
        engine = self.engine()
        cases = {
            "riddle": (safe_cues(), feeling(riddle=0.59)),
            "uncertainty": (safe_cues(uncertainty=0.49), feeling()),
            "cue_safety": (safe_cues(safety=0.59), feeling()),
            "state_safety": (
                safe_cues(),
                feeling(interaction_safety=0.59),
            ),
            "controllability": (safe_cues(controllability=0.49), feeling()),
            "agency": (safe_cues(), feeling(agency=0.49)),
            "threat": (safe_cues(threat=0.36), feeling()),
            "panic": (safe_cues(), feeling(panic=0.46)),
            "social_exposure": (safe_cues(social_exposure=0.51), feeling()),
        }
        for name, (cues, packet) in cases.items():
            with self.subTest(gate=name):
                inquiry = engine.consider(
                    cues,
                    packet,
                    topic_stimulus(
                        DiscoveryTopic.FUNCTIONAL_SELF,
                        event_ref=f"gate-{name}",
                    ),
                )
                self.assertIs(inquiry.status, InquiryStatus.GATE_REJECTED)
        self.assertEqual({}, engine.snapshot()["models"])
        self.assertFalse(engine.store.path.exists())

    def test_panic_and_threat_cannot_turn_mobilization_into_questioning(self) -> None:
        engine = self.engine()
        high_mobilization = feeling(mobilization=0.95)
        threatened = engine.consider(
            safe_cues(threat=0.36),
            high_mobilization,
            topic_stimulus(
                DiscoveryTopic.CONCEPT_RIDDLE,
                event_ref="threatened",
            ),
        )
        panicked = engine.consider(
            safe_cues(),
            feeling(mobilization=0.95, panic=0.46),
            topic_stimulus(
                DiscoveryTopic.CONCEPT_RIDDLE,
                event_ref="panicked",
            ),
        )
        bounded_panic = engine.consider(
            safe_cues(),
            feeling(mobilization=0.80, panic=0.45),
            topic_stimulus(
                DiscoveryTopic.CONCEPT_RIDDLE,
                event_ref="panic-penalty",
            ),
        )
        self.assertIs(threatened.status, InquiryStatus.GATE_REJECTED)
        self.assertIs(panicked.status, InquiryStatus.GATE_REJECTED)
        self.assertIs(bounded_panic.status, InquiryStatus.BELOW_THRESHOLD)

    def test_topic_evidence_authentication_and_person_gates(self) -> None:
        engine = self.engine()

        missing_cases = (
            (
                CuriosityStimulus(
                    DiscoveryTopic.FUNCTIONAL_SELF,
                    "guild-alpha",
                    "self-empty",
                ),
                InquiryStatus.TOPIC_EVIDENCE_REQUIRED,
            ),
            (
                CuriosityStimulus(
                    DiscoveryTopic.MORAL_TRADEOFF,
                    "guild-alpha",
                    "moral-unauthenticated",
                    moral_tension=1.0,
                ),
                InquiryStatus.TOPIC_EVIDENCE_REQUIRED,
            ),
            (
                CuriosityStimulus(
                    DiscoveryTopic.CONCEPT_RIDDLE,
                    "guild-alpha",
                    "concept-empty",
                ),
                InquiryStatus.TOPIC_EVIDENCE_REQUIRED,
            ),
            (
                CuriosityStimulus(
                    DiscoveryTopic.BEHAVIOR_IMPACT,
                    "guild-alpha",
                    "behavior-no-person",
                    authenticated_episode=True,
                    behavior_signal=1.0,
                ),
                InquiryStatus.PERSON_EVIDENCE_REQUIRED,
            ),
            (
                CuriosityStimulus(
                    DiscoveryTopic.BEHAVIOR_IMPACT,
                    "guild-alpha",
                    "behavior-unauthenticated",
                    actor_ref="member-alpha",
                    behavior_signal=1.0,
                ),
                InquiryStatus.PERSON_EVIDENCE_REQUIRED,
            ),
            (
                CuriosityStimulus(
                    DiscoveryTopic.PERSPECTIVE_DISCOVERY,
                    "guild-alpha",
                    "profile-only",
                    actor_ref="member-alpha",
                    authenticated_episode=True,
                    profile_novelty=1.0,
                ),
                InquiryStatus.PERSON_EVIDENCE_REQUIRED,
            ),
        )
        for stimulus, expected in missing_cases:
            with self.subTest(event=stimulus.event_ref):
                inquiry = engine.consider(safe_cues(), feeling(), stimulus)
                self.assertIs(inquiry.status, expected)

        valid = engine.consider(
            safe_cues(),
            feeling(),
            topic_stimulus(
                DiscoveryTopic.BEHAVIOR_IMPACT,
                event_ref="behavior-reviewed-episode",
            ),
        )
        self.assertIs(valid.status, InquiryStatus.QUEUED)
        self.assertTrue(valid.question.subject_present)

        limited = self.engine(
            settings=self.settings(allowed_topics=(DiscoveryTopic.FUNCTIONAL_SELF,))
        )
        disabled_topic = limited.consider(
            safe_cues(),
            feeling(),
            topic_stimulus(
                DiscoveryTopic.CONCEPT_RIDDLE,
                event_ref="topic-disabled",
            ),
        )
        self.assertIs(disabled_topic.status, InquiryStatus.TOPIC_DISABLED)


class DiscoveryReviewChecks(DiscoveryCheckCase):
    def test_observation_and_delivery_do_not_learn_before_review(self) -> None:
        engine = self.engine()
        question = self.queue(
            engine,
            topic_stimulus(
                DiscoveryTopic.MORAL_TRADEOFF,
                event_ref="observed-tradeoff",
            ),
        )
        before_delivery = engine.snapshot()
        self.assertEqual({}, before_delivery["models"])
        self.assertEqual(1, before_delivery["pending_delivery_count"])

        dispatch = engine.pending()[0]
        premature = engine.review(
            StructuredReview(
                question_id=question.question_id,
                advisor_id=dispatch.advisor_id,
                selection=MoralPrinciple.CARE,
                disposition=ReviewDisposition.SUPPORT,
            )
        )
        self.assertIs(premature.status, ReceiptStatus.NOT_DELIVERED)
        self.assertEqual({}, engine.snapshot()["models"])

        delivered = engine.mark_delivery(question.question_id, dispatch.advisor_id)
        self.assertIs(delivered.status, ReceiptStatus.DELIVERED)
        after_delivery = engine.snapshot()
        self.assertEqual({}, after_delivery["models"])
        self.assertEqual(1, after_delivery["awaiting_review_count"])

    def test_delivery_reviewer_and_selection_are_bound_to_question(self) -> None:
        engine = self.engine()
        question = self.queue(
            engine,
            topic_stimulus(
                DiscoveryTopic.CONCEPT_RIDDLE,
                event_ref="bound-question",
            ),
        )
        dispatch = engine.pending()[0]
        self.assertIn(dispatch.advisor_id, engine.settings.advisor_ids)
        self.assertEqual(QUESTION_TEMPLATES[question.topic], dispatch.prompt)
        self.assertEqual(
            tuple(option.value for option in RiddleKind),
            dispatch.selection_options,
        )
        self.assertEqual(
            tuple(option.value for option in ReviewDisposition),
            dispatch.disposition_options,
        )
        self.assertEqual(
            tuple(option.value for option in ReviewImpact),
            dispatch.impact_options,
        )
        from_strings = dispatch.make_review(
            selection=dispatch.selection_options[0],
            disposition=dispatch.disposition_options[0],
            impact=dispatch.impact_options[0],
        )
        self.assertEqual(dispatch.question.question_id, from_strings.question_id)
        self.assertEqual(dispatch.advisor_id, from_strings.advisor_id)
        self.assertIs(RiddleKind.AMBIGUITY, from_strings.selection)
        self.assertIs(ReviewDisposition.SUPPORT, from_strings.disposition)
        self.assertIs(ReviewImpact.POSITIVE, from_strings.impact)
        from_enums = dispatch.make_review(
            selection=RiddleKind.PATTERN,
            disposition=ReviewDisposition.MIXED,
            impact=ReviewImpact.NEUTRAL,
        )
        self.assertIs(RiddleKind.PATTERN, from_enums.selection)
        with self.assertRaisesRegex(ValueError, "dispatch.selection_options"):
            dispatch.make_review(
                selection="not-a-riddle-kind",
                disposition=ReviewDisposition.SUPPORT,
            )
        with self.assertRaisesRegex(ValueError, "dispatch.selection_options"):
            dispatch.make_review(
                selection=MoralPrinciple.CARE,
                disposition=ReviewDisposition.SUPPORT,
            )
        other = next(
            advisor
            for advisor in engine.settings.advisor_ids
            if advisor != dispatch.advisor_id
        )

        with self.assertRaises(PermissionError):
            engine.mark_delivery(question.question_id, 999)
        wrong = engine.mark_delivery(question.question_id, other)
        self.assertIs(wrong.status, ReceiptStatus.WRONG_ADVISOR)
        delivered = engine.mark_delivery(question.question_id, dispatch.advisor_id)
        self.assertIs(delivered.status, ReceiptStatus.DELIVERED)
        repeated_delivery = engine.mark_delivery(
            question.question_id, dispatch.advisor_id
        )
        self.assertIs(repeated_delivery.status, ReceiptStatus.ALREADY_DELIVERED)

        with self.assertRaises(ValueError):
            engine.review(
                StructuredReview(
                    question_id=question.question_id,
                    advisor_id=dispatch.advisor_id,
                    selection=MoralPrinciple.CARE,
                    disposition=ReviewDisposition.SUPPORT,
                )
            )
        with self.assertRaises(PermissionError):
            engine.review(
                StructuredReview(
                    question_id=question.question_id,
                    advisor_id=999,
                    selection=RiddleKind.AMBIGUITY,
                    disposition=ReviewDisposition.SUPPORT,
                )
            )

        accepted = engine.review(
            StructuredReview(
                question_id=question.question_id,
                advisor_id=dispatch.advisor_id,
                selection=RiddleKind.AMBIGUITY,
                disposition=ReviewDisposition.SUPPORT,
            )
        )
        self.assertIs(accepted.status, ReceiptStatus.ACCEPTED)
        self.assertTrue(accepted.model_updated)
        repeated = engine.review(
            StructuredReview(
                question_id=question.question_id,
                advisor_id=dispatch.advisor_id,
                selection=RiddleKind.AMBIGUITY,
                disposition=ReviewDisposition.SUPPORT,
            )
        )
        self.assertIs(repeated.status, ReceiptStatus.ALREADY_REVIEWED)

    def test_abstention_and_expiry_have_no_learning_effect(self) -> None:
        engine = self.engine()
        abstained_question = self.queue(
            engine,
            topic_stimulus(
                DiscoveryTopic.FUNCTIONAL_SELF,
                event_ref="abstain-self",
            ),
        )
        abstained = self.deliver_and_review(
            engine,
            abstained_question.question_id,
            selection=FunctionalCommitment.PRESERVE_AGENCY,
            disposition=ReviewDisposition.ABSTAIN,
        )
        self.assertIs(abstained.status, ReceiptStatus.ABSTAINED)
        self.assertFalse(abstained.model_updated)
        self.assertEqual({}, engine.snapshot()["models"])

        expiry_engine = self.engine(settings=self.settings(advisor_ids=(303,)))
        expired_question = self.queue(
            expiry_engine,
            topic_stimulus(
                DiscoveryTopic.CONCEPT_RIDDLE,
                event_ref="expires",
            ),
        )
        self.clock.advance(hours=24)
        expired = expiry_engine.mark_delivery(expired_question.question_id, 303)
        self.assertIs(expired.status, ReceiptStatus.EXPIRED)
        self.assertEqual(2, expired.revision)
        self.assertEqual({}, expiry_engine.snapshot()["models"])

    def test_conflicting_moral_reviews_preserve_both_sides_and_uncertainty(
        self,
    ) -> None:
        engine = self.engine(settings=self.settings(advisor_ids=(401, 402)))
        questions = [
            self.queue(
                engine,
                topic_stimulus(
                    DiscoveryTopic.MORAL_TRADEOFF,
                    event_ref=f"moral-conflict-{index}",
                ),
            )
            for index in range(2)
        ]
        dispatches = {
            dispatch.question.question_id: dispatch for dispatch in engine.pending()
        }
        self.assertEqual(2, len({item.advisor_id for item in dispatches.values()}))

        dispositions = (
            (ReviewDisposition.SUPPORT, ReviewImpact.POSITIVE),
            (ReviewDisposition.CHALLENGE, ReviewImpact.NEGATIVE),
        )
        for question, (disposition, impact) in zip(questions, dispositions):
            dispatch = dispatches[question.question_id]
            engine.mark_delivery(question.question_id, dispatch.advisor_id)
            receipt = engine.review(
                StructuredReview(
                    question_id=question.question_id,
                    advisor_id=dispatch.advisor_id,
                    selection=MoralPrinciple.FAIRNESS,
                    disposition=disposition,
                    impact=impact,
                )
            )
            self.assertIs(receipt.status, ReceiptStatus.ACCEPTED)

        aggregate = engine.snapshot()["models"]["moral_principles"]["fairness"]
        self.assertEqual(2, aggregate["review_count"])
        self.assertGreater(aggregate["support"], 0.0)
        self.assertGreater(aggregate["challenge"], 0.0)
        self.assertGreater(aggregate["uncertainty"], 0.0)
        self.assertGreater(aggregate["positive_impact"], 0.0)
        self.assertGreater(aggregate["negative_impact"], 0.0)


class DiscoveryPrivacyAndRetentionChecks(DiscoveryCheckCase):
    def test_adverse_exemplar_describes_episode_not_person_worth(self) -> None:
        engine = self.engine(settings=self.settings(advisor_ids=(919,)))
        question = self.queue(
            engine,
            topic_stimulus(
                DiscoveryTopic.BEHAVIOR_IMPACT,
                event_ref="reviewed-adverse-episode",
                actor_ref="ephemeral-subject",
            ),
        )
        receipt = self.deliver_and_review(
            engine,
            question.question_id,
            selection=BehaviorExemplar.ACCOUNTABILITY_NOT_DEMONSTRATED,
            disposition=ReviewDisposition.SUPPORT,
            impact=ReviewImpact.NEGATIVE,
        )

        self.assertIs(receipt.status, ReceiptStatus.ACCEPTED)
        models = engine.snapshot(scope_ref="guild-alpha")["models"]
        aggregate = models["behavior_exemplars"]["accountability_not_demonstrated"]
        self.assertEqual(0.0, aggregate["positive_impact"])
        self.assertGreater(aggregate["negative_impact"], 0.0)
        serialized = json.dumps(models, sort_keys=True)
        self.assertNotIn("ephemeral-subject", serialized)
        self.assertNotIn("person_value", serialized)
        self.assertNotIn("moral_worth", serialized)

    def test_person_model_is_deidentified_and_persistence_contains_no_raw_text(
        self,
    ) -> None:
        engine = self.engine(settings=self.settings(advisor_ids=(987654321012345,)))
        scope = "Guild Privacy Sentinel"
        event = "Event Privacy Sentinel"
        actor = "Actor Privacy Sentinel"
        question = self.queue(
            engine,
            topic_stimulus(
                DiscoveryTopic.BEHAVIOR_IMPACT,
                scope_ref=scope,
                event_ref=event,
                actor_ref=actor,
            ),
        )
        accepted = self.deliver_and_review(
            engine,
            question.question_id,
            selection=BehaviorExemplar.REPAIR,
            impact=ReviewImpact.POSITIVE,
        )
        self.assertIs(accepted.status, ReceiptStatus.ACCEPTED)

        snapshot = engine.snapshot(scope_ref=scope)
        self.assertEqual(
            {"repair"},
            set(snapshot["models"]["behavior_exemplars"]),
        )
        aggregate = snapshot["models"]["behavior_exemplars"]["repair"]
        self.assertEqual(
            {
                "review_count",
                "support",
                "challenge",
                "uncertainty",
                "positive_impact",
                "negative_impact",
                "updated_at",
            },
            set(aggregate),
        )
        self.assertGreater(aggregate["positive_impact"], 0.0)
        self.assertEqual(0.0, aggregate["negative_impact"])
        snapshot_text = json.dumps(snapshot, sort_keys=True)
        self.assertIsNone(re.search(r"\b[0-9a-f]{64}\b", snapshot_text))

        state = json.loads(engine.store.path.read_text(encoding="utf-8"))
        state_text = json.dumps(state, sort_keys=True)
        for sentinel in (
            scope,
            event,
            actor,
            str(987654321012345),
            QUESTION_TEMPLATES[DiscoveryTopic.BEHAVIOR_IMPACT],
            "race",
            "religion",
            "sexuality",
            "diagnosis",
            "personality",
            "moral_worth",
        ):
            with self.subTest(sentinel=sentinel):
                self.assertNotIn(sentinel, state_text)

        scoped_models = next(iter(state["models"].values()))
        stored_aggregate = scoped_models["behavior_exemplars"]["repair"]
        self.assertEqual(set(aggregate), set(stored_aggregate))
        self.assertNotIn("subject_key", stored_aggregate)
        self.assertNotIn("event_key", stored_aggregate)

    def test_state_authentication_detects_persisted_mutation(self) -> None:
        engine = self.engine()
        self.queue(
            engine,
            topic_stimulus(
                DiscoveryTopic.CONCEPT_RIDDLE,
                event_ref="tamper-target",
            ),
        )
        state = json.loads(engine.store.path.read_text(encoding="utf-8"))
        record = next(iter(state["pending"].values()))
        record["score"] = max(0.0, float(record["score"]) - 0.01)
        engine.store.path.write_text(json.dumps(state), encoding="utf-8")

        with self.assertRaises(StateStoreError):
            engine.snapshot()

    def test_authenticated_lifecycle_timestamp_regression_is_rejected(self) -> None:
        engine = self.engine(settings=self.settings(advisor_ids=(410,)))
        question = self.queue(
            engine,
            topic_stimulus(
                DiscoveryTopic.CONCEPT_RIDDLE,
                event_ref="invalid-lifecycle-time",
            ),
        )
        receipt = self.deliver_and_review(
            engine,
            question.question_id,
            selection=RiddleKind.PATTERN,
        )
        self.assertIs(receipt.status, ReceiptStatus.ACCEPTED)

        state = json.loads(engine.store.path.read_text(encoding="utf-8"))
        closed = state["history"][0]
        created_at = datetime.fromisoformat(
            closed["created_at"].replace("Z", "+00:00")
        )
        closed["delivered_at"] = (
            created_at - timedelta(minutes=1)
        ).isoformat().replace("+00:00", "Z")
        engine._seal(state)
        engine.store.path.write_text(json.dumps(state), encoding="utf-8")

        with self.assertRaisesRegex(StateStoreError, "history entry"):
            engine.snapshot()

    def test_pending_and_snapshot_are_authenticated_read_only_views(self) -> None:
        engine = self.engine(settings=self.settings(advisor_ids=(411,)))
        self.queue(
            engine,
            topic_stimulus(
                DiscoveryTopic.CONCEPT_RIDDLE,
                event_ref="read-only-views",
            ),
        )
        before = engine.store.path.read_bytes()
        self.clock.advance(minutes=1)

        self.assertEqual(1, len(engine.pending()))
        self.assertEqual(1, engine.snapshot()["pending_delivery_count"])

        self.assertEqual(before, engine.store.path.read_bytes())

    def test_read_views_hide_expired_pending_without_writing(self) -> None:
        engine = self.engine(settings=self.settings(advisor_ids=(412,)))
        self.queue(
            engine,
            topic_stimulus(
                DiscoveryTopic.CONCEPT_RIDDLE,
                event_ref="read-only-expiry",
            ),
        )
        before = engine.store.path.read_bytes()
        self.clock.advance(hours=24)

        self.assertEqual((), engine.pending())
        view = engine.snapshot()
        self.assertEqual(0, view["pending_delivery_count"])
        self.assertEqual(1, view["recent_closed_count"])
        self.assertEqual(before, engine.store.path.read_bytes())

    def test_small_clock_rollback_is_tolerated_without_timestamp_regression(
        self,
    ) -> None:
        engine = self.engine(settings=self.settings(advisor_ids=(413,)))
        question = self.queue(
            engine,
            topic_stimulus(
                DiscoveryTopic.CONCEPT_RIDDLE,
                event_ref="small-clock-rollback",
            ),
        )
        original = json.loads(
            engine.store.path.read_text(encoding="utf-8")
        )["updated_at"]
        self.clock.advance(minutes=-4)

        dispatch = engine.pending()[0]
        delivered = engine.mark_delivery(question.question_id, dispatch.advisor_id)
        reviewed = engine.review(
            StructuredReview(
                question_id=question.question_id,
                advisor_id=dispatch.advisor_id,
                selection=RiddleKind.PATTERN,
                disposition=ReviewDisposition.SUPPORT,
            )
        )

        self.assertIs(delivered.status, ReceiptStatus.DELIVERED)
        self.assertIs(reviewed.status, ReceiptStatus.ACCEPTED)
        persisted = json.loads(engine.store.path.read_text(encoding="utf-8"))
        self.assertEqual(original, persisted["updated_at"])
        closed = persisted["history"][0]
        self.assertEqual(closed["created_at"], closed["delivered_at"])
        self.assertEqual(closed["created_at"], closed["closed_at"])
        aggregate = next(iter(persisted["models"].values()))["riddle_kinds"][
            RiddleKind.PATTERN.value
        ]
        self.assertEqual(closed["created_at"], aggregate["updated_at"])

        self.clock.advance(minutes=-2)
        with self.assertRaises(StateStoreError):
            engine.snapshot()

    def test_deduplication_rate_limits_and_restart_are_durable(self) -> None:
        settings = self.settings(
            advisor_ids=(501,),
            reviewer_cooldown_hours=8.0,
            max_per_reviewer_24h=2,
            max_per_scope_24h=2,
        )
        path = self.root / "durable-rate.json"
        engine = self.engine(settings=settings, path=path)
        first_stimulus = topic_stimulus(
            DiscoveryTopic.CONCEPT_RIDDLE,
            event_ref="rate-first",
        )
        self.queue(engine, first_stimulus)
        duplicate = engine.consider(safe_cues(), feeling(), first_stimulus)
        self.assertIs(duplicate.status, InquiryStatus.DEDUPLICATED)

        immediate = engine.consider(
            safe_cues(),
            feeling(),
            topic_stimulus(
                DiscoveryTopic.CONCEPT_RIDDLE,
                event_ref="rate-second",
            ),
        )
        self.assertIs(immediate.status, InquiryStatus.RATE_LIMITED)

        self.clock.advance(hours=8)
        self.queue(
            engine,
            topic_stimulus(
                DiscoveryTopic.CONCEPT_RIDDLE,
                event_ref="rate-second",
            ),
            packet=feeling(now=self.clock.value),
        )

        restarted = self.engine(settings=settings, path=path)
        duplicate_after_restart = restarted.consider(
            safe_cues(), feeling(now=self.clock.value), first_stimulus
        )
        limited_after_restart = restarted.consider(
            safe_cues(),
            feeling(now=self.clock.value),
            topic_stimulus(
                DiscoveryTopic.CONCEPT_RIDDLE,
                event_ref="rate-third",
            ),
        )
        self.assertIs(duplicate_after_restart.status, InquiryStatus.DEDUPLICATED)
        self.assertIs(limited_after_restart.status, InquiryStatus.RATE_LIMITED)

        self.clock.advance(hours=16, seconds=1)
        after_window = restarted.consider(
            safe_cues(),
            feeling(now=self.clock.value),
            topic_stimulus(
                DiscoveryTopic.CONCEPT_RIDDLE,
                event_ref="rate-third",
            ),
        )
        self.assertIs(after_window.status, InquiryStatus.QUEUED)

    def test_subject_topic_limit_and_concurrent_dedupe(self) -> None:
        settings = self.settings(advisor_ids=(601, 602, 603))
        path = self.root / "subject-rate.json"
        engine = self.engine(settings=settings, path=path)
        self.queue(
            engine,
            topic_stimulus(
                DiscoveryTopic.BEHAVIOR_IMPACT,
                event_ref="subject-first",
                actor_ref="subject-one",
            ),
        )
        self.clock.advance(hours=8)
        subject_limited = engine.consider(
            safe_cues(),
            feeling(now=self.clock.value),
            topic_stimulus(
                DiscoveryTopic.BEHAVIOR_IMPACT,
                event_ref="subject-second",
                actor_ref="subject-one",
            ),
        )
        self.assertIs(subject_limited.status, InquiryStatus.RATE_LIMITED)

        restarted = self.engine(settings=settings, path=path)
        still_limited = restarted.consider(
            safe_cues(),
            feeling(now=self.clock.value),
            topic_stimulus(
                DiscoveryTopic.BEHAVIOR_IMPACT,
                event_ref="subject-second",
                actor_ref="subject-one",
            ),
        )
        self.assertIs(still_limited.status, InquiryStatus.RATE_LIMITED)
        self.clock.advance(days=6, hours=16, seconds=1)
        released = restarted.consider(
            safe_cues(),
            feeling(now=self.clock.value),
            topic_stimulus(
                DiscoveryTopic.BEHAVIOR_IMPACT,
                event_ref="subject-second",
                actor_ref="subject-one",
            ),
        )
        self.assertIs(released.status, InquiryStatus.QUEUED)

        concurrent_engine = self.engine(
            settings=self.settings(advisor_ids=(701, 702, 703))
        )
        concurrent_stimulus = topic_stimulus(
            DiscoveryTopic.FUNCTIONAL_SELF,
            event_ref="concurrent-dedupe",
        )
        packet = feeling(now=self.clock.value)
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = tuple(
                executor.map(
                    lambda _: concurrent_engine.consider(
                        safe_cues(), packet, concurrent_stimulus
                    ),
                    range(8),
                )
            )
        statuses = [result.status for result in results]
        self.assertEqual(1, statuses.count(InquiryStatus.QUEUED))
        self.assertEqual(7, statuses.count(InquiryStatus.DEDUPLICATED))

    def test_forget_subject_retains_only_deidentified_model_then_purge_erases_it(
        self,
    ) -> None:
        engine = self.engine(settings=self.settings(advisor_ids=(801, 802, 803)))
        actor = "forget-me-subject"
        reviewed = self.queue(
            engine,
            topic_stimulus(
                DiscoveryTopic.BEHAVIOR_IMPACT,
                event_ref="forget-reviewed",
                actor_ref=actor,
            ),
        )
        self.deliver_and_review(
            engine,
            reviewed.question_id,
            selection=BehaviorExemplar.ACCOUNTABILITY,
            impact=ReviewImpact.MIXED,
        )
        self.queue(
            engine,
            topic_stimulus(
                DiscoveryTopic.PERSPECTIVE_DISCOVERY,
                event_ref="forget-pending",
                actor_ref=actor,
            ),
        )

        removed = engine.forget_subject(
            scope_ref="guild-alpha",
            actor_ref=actor,
        )
        self.assertEqual(
            {"pending_removed": 1, "history_removed": 1},
            removed,
        )
        snapshot = engine.snapshot(scope_ref="guild-alpha")
        self.assertEqual(0, snapshot["pending_delivery_count"])
        self.assertEqual(0, snapshot["recent_closed_count"])
        self.assertIn("behavior_exemplars", snapshot["models"])

        scope_removed = engine.purge_scope(scope_ref="guild-alpha")
        self.assertEqual(1, scope_removed["scoped_models_removed"])
        self.assertEqual({}, engine.snapshot(scope_ref="guild-alpha")["models"])

        self.queue(
            engine,
            topic_stimulus(
                DiscoveryTopic.CONCEPT_RIDDLE,
                scope_ref="guild-beta",
                event_ref="purge-all-pending",
            ),
        )
        all_removed = engine.purge_all()
        self.assertEqual(1, all_removed["pending_removed"])
        fresh = engine.snapshot()
        self.assertEqual({}, fresh["models"])
        self.assertEqual(0, fresh["pending_delivery_count"])
        self.assertEqual(0, fresh["recent_closed_count"])


class DiscoveryServiceIntegrationChecks(DiscoveryCheckCase):
    def service_config(self, *, discovery_enabled: bool) -> dict[str, object]:
        return {
            "enabled": True,
            "state_directory": "./service-state",
            "cross_guild_associations": False,
            "max_cached_sectors": 4,
            "discovery": {
                "enabled": discovery_enabled,
                "advisor_ids": [901] if discovery_enabled else [],
                "allowed_topics": [topic.value for topic in DiscoveryTopic],
                "score_threshold": 0.65,
                "reviewer_cooldown_hours": 8.0,
                "max_per_reviewer_24h": 3,
                "max_per_scope_24h": 12,
                "same_subject_topic_days": 7.0,
                "question_ttl_hours": 24.0,
                "max_pending": 64,
            },
        }

    @staticmethod
    def service_keys() -> dict[str, str]:
        return {
            "FATE_ORGANISM_IDENTITY_KEY": "identity-service-key-material-0000001",
            "FATE_ORGANISM_LEARNING_KEY": "learning-service-key-material-0000002",
            "FATE_ORGANISM_OWNER_KEY": "owner-service-key-material-0000000003",
        }

    def test_build_and_async_review_boundary(self) -> None:
        service = build_organism_service(
            self.service_config(discovery_enabled=True),
            repository_root=self.root,
            environment=self.service_keys(),
        )
        assert service is not None and service.discovery is not None
        self.assertTrue(service.discovery_enabled)

        inquiry = service.discovery.consider(
            safe_cues(),
            feeling(now=service.registry.clock()),
            topic_stimulus(
                DiscoveryTopic.FUNCTIONAL_SELF,
                scope_ref="service-guild",
                event_ref="service-question",
            ),
        )
        self.assertIs(inquiry.status, InquiryStatus.QUEUED)
        dispatches = asyncio.run(service.pending_discovery())
        self.assertEqual(1, len(dispatches))
        dispatch = dispatches[0]
        delivered = asyncio.run(
            service.mark_discovery_delivery(
                dispatch.question.question_id,
                dispatch.advisor_id,
            )
        )
        self.assertIs(delivered.status, ReceiptStatus.DELIVERED)
        reviewed = asyncio.run(
            service.review_discovery(
                StructuredReview(
                    question_id=dispatch.question.question_id,
                    advisor_id=dispatch.advisor_id,
                    selection=FunctionalCommitment.SEEK_UNDERSTANDING,
                    disposition=ReviewDisposition.SUPPORT,
                )
            )
        )
        self.assertIs(reviewed.status, ReceiptStatus.ACCEPTED)
        snapshot = asyncio.run(service.inspect_discovery(guild_ref="service-guild"))
        self.assertIn(
            "seek_understanding", snapshot["models"]["functional_commitments"]
        )

        cortex_frame = SensorFrame(
            event_id="reviewed-cortex-context",
            scope="ignored-by-service",
        )
        cortex_cues = safe_cues()
        cortex_packet = feeling(now=service.registry.clock())
        with patch.object(
            service,
            "_feel_with_cues_operation",
            return_value=FeelingWithCues(cues=cortex_cues, packet=cortex_packet),
        ):
            _, messages = asyncio.run(
                service.cortex_input(
                    "service-guild",
                    cortex_frame,
                    "Current reviewed-model response",
                )
            )
        discovery_messages = [
            message
            for message in messages
            if "<organism_discovery>" in message["content"]
        ]
        self.assertEqual(1, len(discovery_messages))
        discovery_content = discovery_messages[0]["content"]
        self.assertIn("seek_understanding", discovery_content)
        self.assertNotIn("service-guild", discovery_content)
        self.assertNotIn(dispatch.question.question_id, discovery_content)

    def test_disabled_service_boundary_is_inert(self) -> None:
        service = build_organism_service(
            self.service_config(discovery_enabled=False),
            repository_root=self.root,
            environment=self.service_keys(),
        )
        assert service is not None
        self.assertFalse(service.discovery_enabled)
        self.assertTrue(service.discovery_control_available)
        self.assertEqual((), asyncio.run(service.pending_discovery()))
        self.assertFalse(asyncio.run(service.inspect_discovery())["enabled"])
        self.assertEqual(
            {
                "pending_removed": 0,
                "history_removed": 0,
                "scoped_models_removed": 0,
            },
            asyncio.run(service.purge_discovery()),
        )
        delivery = asyncio.run(service.mark_discovery_delivery("a" * 64, 901))
        review = asyncio.run(
            service.review_discovery(
                StructuredReview(
                    question_id="a" * 64,
                    advisor_id=901,
                    selection=FunctionalCommitment.PRESERVE_AGENCY,
                    disposition=ReviewDisposition.SUPPORT,
                )
            )
        )
        self.assertIs(delivery.status, ReceiptStatus.DISABLED)
        self.assertIs(review.status, ReceiptStatus.DISABLED)
        with self.assertRaisesRegex(
            OrganismDiscoveryDisabledError,
            "enable it before considering inquiry",
        ):
            asyncio.run(
                service.consider_discovery(
                    "disabled-guild",
                    SensorFrame(event_id="disabled-consider"),
                    topic_stimulus(
                        DiscoveryTopic.FUNCTIONAL_SELF,
                        event_ref="disabled-consider",
                        scope_ref="disabled-guild",
                    ),
                )
            )
        self.assertEqual(
            [],
            list((service.registry.state_directory / "sectors").glob("*.json")),
        )

    def test_disabled_service_can_inspect_and_purge_retained_discovery(self) -> None:
        enabled = build_organism_service(
            self.service_config(discovery_enabled=True),
            repository_root=self.root,
            environment=self.service_keys(),
        )
        assert enabled is not None and enabled.discovery is not None
        inquiry = enabled.discovery.consider(
            safe_cues(),
            feeling(now=enabled.registry.clock()),
            topic_stimulus(
                DiscoveryTopic.FUNCTIONAL_SELF,
                scope_ref="retained-service-guild",
                event_ref="retained-service-model",
            ),
        )
        self.assertIs(inquiry.status, InquiryStatus.QUEUED)
        dispatch = asyncio.run(enabled.pending_discovery())[0]
        self.assertIs(
            asyncio.run(
                enabled.mark_discovery_delivery(
                    dispatch.question.question_id,
                    dispatch.advisor_id,
                )
            ).status,
            ReceiptStatus.DELIVERED,
        )
        self.assertIs(
            asyncio.run(
                enabled.review_discovery(
                    dispatch.make_review(
                        selection=FunctionalCommitment.SEEK_UNDERSTANDING,
                        disposition=ReviewDisposition.SUPPORT,
                    )
                )
            ).status,
            ReceiptStatus.ACCEPTED,
        )

        disabled = build_organism_service(
            self.service_config(discovery_enabled=False),
            repository_root=self.root,
            environment=self.service_keys(),
        )
        assert disabled is not None
        self.assertFalse(disabled.discovery_enabled)
        retained = asyncio.run(
            disabled.inspect_discovery(guild_ref="retained-service-guild")
        )
        self.assertFalse(retained["enabled"])
        self.assertIn(
            "seek_understanding",
            retained["models"]["functional_commitments"],
        )
        removed = asyncio.run(disabled.purge_discovery())
        self.assertEqual(1, removed["history_removed"])
        self.assertEqual(1, removed["scoped_models_removed"])
        self.assertEqual({}, asyncio.run(disabled.inspect_discovery())["models"])

    def test_service_binds_stimulus_to_guild_event_and_actor(self) -> None:
        service = build_organism_service(
            self.service_config(discovery_enabled=True),
            repository_root=self.root,
            environment=self.service_keys(),
        )
        assert service is not None
        frame = SensorFrame(
            event_id="bound-event",
            scope="ignored-by-service",
            actor_ref="bound-actor",
        )
        invalid = (
            CuriosityStimulus(
                DiscoveryTopic.BEHAVIOR_IMPACT,
                "wrong-guild",
                "bound-event",
                actor_ref="bound-actor",
                authenticated_episode=True,
                behavior_signal=1.0,
            ),
            CuriosityStimulus(
                DiscoveryTopic.BEHAVIOR_IMPACT,
                "guild-one",
                "wrong-event",
                actor_ref="bound-actor",
                authenticated_episode=True,
                behavior_signal=1.0,
            ),
            CuriosityStimulus(
                DiscoveryTopic.BEHAVIOR_IMPACT,
                "guild-one",
                "bound-event",
                actor_ref="wrong-actor",
                authenticated_episode=True,
                behavior_signal=1.0,
            ),
        )
        for stimulus in invalid:
            with self.subTest(stimulus=stimulus), self.assertRaises(ValueError):
                asyncio.run(service.consider_discovery("guild-one", frame, stimulus))

    def test_cortex_path_observes_but_does_not_learn(self) -> None:
        service = build_organism_service(
            self.service_config(discovery_enabled=True),
            repository_root=self.root,
            environment=self.service_keys(),
        )
        assert service is not None and service.discovery is not None
        now = service.registry.clock()
        cues = safe_cues(novelty=0.95)
        packet = feeling(now=now)
        frame = SensorFrame(event_id="cortex-discovery", scope="ignored")

        with patch.object(
            service,
            "_feel_with_cues_operation",
            return_value=FeelingWithCues(cues=cues, packet=packet),
        ):
            returned, messages = asyncio.run(
                service.cortex_input("cortex-guild", frame, "Current user text")
            )

        self.assertIs(returned, packet)
        self.assertEqual("Current user text", messages[-1]["content"])
        pending = asyncio.run(service.pending_discovery())
        self.assertEqual(1, len(pending))
        snapshot = asyncio.run(service.inspect_discovery(guild_ref="cortex-guild"))
        self.assertEqual({}, snapshot["models"])
        self.assertEqual(1, snapshot["pending_delivery_count"])


if __name__ == "__main__":
    unittest.main()
