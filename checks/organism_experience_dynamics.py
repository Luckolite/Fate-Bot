"""Focused regressions for bounded, short-lived experience dynamics."""

# ruff: noqa: E402 - direct script execution adds the repository root first.

from __future__ import annotations

import asyncio
import json
import math
import random
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from organism import AutonomicMode, Capability, Cues, OrganismicState
from organism.body import ExternalSenses, OperationalInteroception, SensorFrame
from organism.experience import (
    DYNAMIC_FIELDS,
    AttentionCandidate,
    ChangePattern,
    ExperienceDomain,
    ExperienceDynamics,
    FocusTrace,
    TemporalContinuity,
    compete_attention,
    cortex_developer_message,
    cortex_payload,
    derive_candidates,
    derive_dynamics,
    initial_expectation,
    integrate_experience,
    prediction_error,
)
from organism.fate_service import OrganismService
from organism.models import (
    GUIDANCE_FIELDS,
    FeelingPacket,
    ProtectionSummary,
    ProtectivePosture,
)
from organism.sectors import FeelingWithCues, MemorySectorRegistry


NOW = datetime(2026, 8, 30, 12, tzinfo=timezone.utc)
EXPECTED_DYNAMIC_FIELDS = (
    "activation",
    "valence",
    "interaction_safety",
    "agency",
    "uncertainty",
    "load",
    "connection_readiness",
    "surprise",
)


def _zero_axes() -> dict[str, float]:
    return dict.fromkeys(DYNAMIC_FIELDS, 0.0)


def _packet(
    generated_at: datetime,
    *,
    activation: float = 0.25,
    valence: float = 0.2,
    interaction_safety: float = 0.8,
    agency: float = 0.8,
    uncertainty: float = 0.2,
    load: float = 0.2,
) -> FeelingPacket:
    state = OrganismicState(
        activation=activation,
        pleasantness=valence,
        interaction_safety=interaction_safety,
        agency=agency,
        uncertainty=uncertainty,
        load=load,
        updated_at=generated_at,
    )
    return FeelingPacket(
        generated_at=generated_at,
        expires_at=generated_at + timedelta(minutes=5),
        state=state,
        modes={
            AutonomicMode.SOCIAL_ENGAGEMENT: 0.7,
            AutonomicMode.MOBILIZATION: 0.2,
            AutonomicMode.CONSERVATION: 0.1,
        },
        dominant_mode=AutonomicMode.SOCIAL_ENGAGEMENT,
        guidance={name: 0.5 for name in GUIDANCE_FIELDS},
        protection=ProtectionSummary(
            posture=ProtectivePosture.OPEN,
            caution=0.1,
            confidence=0.8,
            uncertainty=0.2,
        ),
        recruitments=(),
    )


def _model(**updates: float):
    base = integrate_experience(
        cues=Cues(safety=0.8, controllability=0.8),
        feeling=_packet(NOW),
    ).self_model
    return replace(base, **updates)


def _moment(
    generated_at: datetime,
    *,
    model=None,
    surprise: float = 0.2,
    dynamics: ExperienceDynamics | None = None,
):
    base = integrate_experience(
        cues=Cues(safety=0.8, controllability=0.8),
        feeling=_packet(generated_at),
    )
    selected_model = model or base.self_model
    prediction = replace(base.prediction, surprise=surprise)
    broadcast = replace(base.broadcast, surprise=surprise)
    changes = {
        "generated_at": generated_at,
        "self_model": selected_model,
        "prediction": prediction,
        "broadcast": broadcast,
    }
    if dynamics is not None:
        changes["dynamics"] = dynamics
    return replace(base, **changes)


def _point(model, surprise: float) -> dict[str, float]:
    return {
        "activation": model.activation,
        "valence": (model.valence + 1.0) / 2.0,
        "interaction_safety": model.interaction_safety,
        "agency": model.agency,
        "uncertainty": model.uncertainty,
        "load": model.load,
        "connection_readiness": model.connection_readiness,
        "surprise": surprise,
    }


def _experience_payload(messages: list[dict[str, str]]) -> dict[str, object]:
    message = next(
        item for item in messages if "<organism_experience>" in item["content"]
    )
    encoded = (
        message["content"]
        .split("<organism_experience>", 1)[1]
        .split("</organism_experience>", 1)[0]
    )
    return json.loads(encoded)


def _dynamic_values(dynamics: ExperienceDynamics) -> tuple[float, ...]:
    return (
        *dynamics.impulse.values(),
        *dynamics.momentum.values(),
        dynamics.flux,
        dynamics.volatility,
        dynamics.escalation,
        dynamics.settling,
        dynamics.retention,
    )


class DynamicsContractChecks(unittest.TestCase):
    def test_contract_is_exact_immutable_and_bounded(self) -> None:
        self.assertEqual(EXPECTED_DYNAMIC_FIELDS, DYNAMIC_FIELDS)
        source = _zero_axes()
        dynamics = ExperienceDynamics(
            impulse=source,
            momentum=_zero_axes(),
            flux=0.0,
            volatility=0.0,
            escalation=0.0,
            settling=0.0,
            retention=0.0,
            pattern=ChangePattern.INITIAL,
            revision=0,
        )
        source["activation"] = 1.0

        self.assertEqual(_zero_axes(), dict(dynamics.impulse))
        self.assertEqual(_zero_axes(), dict(dynamics.momentum))
        self.assertIs(ChangePattern.INITIAL, dynamics.pattern)
        with self.assertRaises(FrozenInstanceError):
            dynamics.flux = 0.5  # type: ignore[misc]

        missing = _zero_axes()
        missing.pop("surprise")
        extra = {**_zero_axes(), "private_axis": 0.0}
        for invalid in (missing, extra):
            with self.subTest(keys=tuple(invalid)), self.assertRaises(ValueError):
                replace(dynamics, impulse=invalid)

        for invalid in (True, math.nan, math.inf, -1.01, 1.01):
            with self.subTest(impulse=invalid), self.assertRaises(ValueError):
                replace(dynamics, impulse={**_zero_axes(), "activation": invalid})
        for field_name in (
            "flux",
            "volatility",
            "escalation",
            "settling",
            "retention",
        ):
            for invalid in (True, math.nan, math.inf, -0.01, 1.01):
                with (
                    self.subTest(field=field_name, value=invalid),
                    self.assertRaises(ValueError),
                ):
                    replace(dynamics, **{field_name: invalid})
        for invalid in (True, -1, 65536):
            with self.subTest(revision=invalid), self.assertRaises(ValueError):
                replace(dynamics, revision=invalid)

    def test_initial_and_invalid_time_gaps_reset_neutrally(self) -> None:
        model = _model()
        initial = derive_dynamics(None, model, 0.2, NOW)
        self.assertEqual(_zero_axes(), dict(initial.impulse))
        self.assertEqual(_zero_axes(), dict(initial.momentum))
        self.assertEqual(ChangePattern.INITIAL, initial.pattern)
        self.assertEqual(0, initial.revision)
        self.assertTrue(all(value == 0.0 for value in _dynamic_values(initial)))

        previous = _moment(NOW, model=model, surprise=0.2)
        for seconds in (0, -1, 120, 600):
            with self.subTest(seconds=seconds):
                reset = derive_dynamics(
                    previous,
                    model,
                    0.2,
                    NOW + timedelta(seconds=seconds),
                )
                self.assertEqual(initial, reset)
        with self.assertRaises(ValueError):
            derive_dynamics(None, model, 0.2, datetime(2026, 8, 30, 12))

    def test_exact_change_acceleration_and_reversal_math(self) -> None:
        safe = _model(
            activation=0.2,
            valence=0.6,
            interaction_safety=0.9,
            agency=0.9,
            uncertainty=0.1,
            load=0.1,
            connection_readiness=0.8,
        )
        strained = _model(
            activation=0.8,
            valence=-0.4,
            interaction_safety=0.2,
            agency=0.2,
            uncertainty=0.8,
            load=0.8,
            connection_readiness=0.2,
        )
        previous = _moment(NOW, model=safe, surprise=0.1)
        current_at = NOW + timedelta(seconds=30)
        rising = derive_dynamics(previous, strained, 0.8, current_at)
        expected_impulse = {
            name: _point(strained, 0.8)[name] - _point(safe, 0.1)[name]
            for name in DYNAMIC_FIELDS
        }

        self.assertAlmostEqual(0.5, rising.retention)
        self.assertEqual(1, rising.revision)
        for name, expected in expected_impulse.items():
            self.assertAlmostEqual(expected, rising.impulse[name])
            self.assertAlmostEqual(expected, rising.momentum[name])
        magnitudes = [abs(value) for value in expected_impulse.values()]
        expected_flux = 0.65 * (sum(magnitudes) / len(magnitudes)) + 0.35 * max(
            magnitudes
        )
        self.assertAlmostEqual(expected_flux, rising.flux)
        innovations = [value / 2.0 for value in magnitudes]
        expected_volatility = 0.65 * (sum(innovations) / len(innovations)) + 0.35 * max(
            innovations
        )
        self.assertAlmostEqual(expected_volatility, rising.volatility)
        self.assertGreater(rising.escalation, rising.settling)
        self.assertIs(ChangePattern.INCREASING_STRAIN, rising.pattern)

        middle = _moment(
            current_at,
            model=strained,
            surprise=0.8,
            dynamics=rising,
        )
        falling = derive_dynamics(
            middle,
            safe,
            0.1,
            current_at + timedelta(seconds=30),
        )
        self.assertGreater(falling.settling, falling.escalation)
        self.assertGreater(falling.volatility, 0.0)
        self.assertIs(ChangePattern.DECREASING_STRAIN, falling.pattern)

    def test_neutral_repetition_decays_momentum_to_steady(self) -> None:
        safe = _model(
            activation=0.2,
            valence=0.6,
            interaction_safety=0.9,
            agency=0.9,
            uncertainty=0.1,
            load=0.1,
            connection_readiness=0.8,
        )
        strained = _model(
            activation=0.8,
            valence=-0.4,
            interaction_safety=0.2,
            agency=0.2,
            uncertainty=0.8,
            load=0.8,
            connection_readiness=0.2,
        )
        at = NOW + timedelta(seconds=30)
        dynamics = derive_dynamics(
            _moment(NOW, model=safe, surprise=0.1),
            strained,
            0.8,
            at,
        )
        prior_peak = max(abs(value) for value in dynamics.momentum.values())
        prior_volatility = dynamics.volatility

        for _ in range(6):
            previous = _moment(
                at,
                model=strained,
                surprise=0.8,
                dynamics=dynamics,
            )
            at += timedelta(seconds=30)
            dynamics = derive_dynamics(previous, strained, 0.8, at)
            peak = max(abs(value) for value in dynamics.momentum.values())
            self.assertLess(peak, prior_peak)
            self.assertLessEqual(dynamics.volatility, prior_volatility)
            self.assertEqual(_zero_axes(), dict(dynamics.impulse))
            self.assertEqual(0.0, dynamics.flux)
            prior_peak = peak
            prior_volatility = dynamics.volatility

        self.assertIs(ChangePattern.STEADY, dynamics.pattern)

    def test_revision_saturates_and_seeded_sequences_are_deterministic(self) -> None:
        model = _model()
        initial = derive_dynamics(None, model, 0.2, NOW)
        saturated = replace(initial, revision=65535)
        previous = _moment(NOW, model=model, surprise=0.2, dynamics=saturated)
        successor = derive_dynamics(
            previous,
            model,
            0.2,
            NOW + timedelta(seconds=1),
        )
        self.assertEqual(65535, successor.revision)

        def run(seed: int) -> tuple[ExperienceDynamics, ...]:
            generator = random.Random(seed)
            at = NOW
            current_model = _model()
            surprise = 0.2
            moment = _moment(at, model=current_model, surprise=surprise)
            results = []
            for _ in range(250):
                at += timedelta(seconds=generator.randint(1, 119))
                current_model = _model(
                    activation=generator.random(),
                    valence=generator.uniform(-1.0, 1.0),
                    interaction_safety=generator.random(),
                    agency=generator.random(),
                    uncertainty=generator.random(),
                    load=generator.random(),
                    connection_readiness=generator.random(),
                )
                surprise = generator.random()
                dynamics = derive_dynamics(moment, current_model, surprise, at)
                self.assertEqual(set(DYNAMIC_FIELDS), set(dynamics.impulse))
                self.assertEqual(set(DYNAMIC_FIELDS), set(dynamics.momentum))
                self.assertTrue(
                    all(math.isfinite(value) for value in _dynamic_values(dynamics))
                )
                self.assertTrue(
                    all(-1.0 <= value <= 1.0 for value in dynamics.impulse.values())
                )
                self.assertTrue(
                    all(-1.0 <= value <= 1.0 for value in dynamics.momentum.values())
                )
                self.assertTrue(
                    all(0.0 <= value <= 1.0 for value in _dynamic_values(dynamics)[16:])
                )
                results.append(dynamics)
                moment = _moment(
                    at,
                    model=current_model,
                    surprise=surprise,
                    dynamics=dynamics,
                )
            return tuple(results)

        self.assertEqual(run(9137), run(9137))


class DynamicAttentionChecks(unittest.TestCase):
    def test_temporal_pull_is_bounded_typed_and_never_applied_to_person(self) -> None:
        safe = _model(
            activation=0.2,
            valence=0.6,
            interaction_safety=0.9,
            agency=0.9,
            uncertainty=0.1,
            load=0.1,
            connection_readiness=0.8,
        )
        strained = _model(
            activation=0.9,
            valence=-0.6,
            interaction_safety=0.1,
            agency=0.1,
            uncertainty=0.9,
            load=0.9,
            connection_readiness=0.1,
        )
        dynamics = derive_dynamics(
            _moment(NOW, model=safe, surprise=0.1),
            strained,
            0.9,
            NOW + timedelta(seconds=10),
        )
        cues = Cues(threat=0.9, uncertainty=0.9, controllability=0.1)
        error = prediction_error(initial_expectation(), cues)
        candidates = derive_candidates(cues, strained, error, dynamics=dynamics)
        by_domain = {candidate.domain: candidate for candidate in candidates}

        self.assertEqual(0.0, by_domain[ExperienceDomain.PERSON].temporal_pull)
        self.assertGreater(
            by_domain[ExperienceDomain.PROTECTION].temporal_pull,
            0.0,
        )
        self.assertTrue(
            all(0.0 <= candidate.temporal_pull <= 1.0 for candidate in candidates)
        )
        for invalid in (True, math.nan, math.inf, -0.01, 1.01):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                AttentionCandidate(
                    ExperienceDomain.REPAIR,
                    intensity=0.2,
                    temporal_pull=invalid,
                )

    def test_current_evidence_dominates_history_and_protection_interrupts(self) -> None:
        stale = AttentionCandidate(
            ExperienceDomain.CONNECTION,
            intensity=0.0,
            urgency=0.0,
            novelty=0.0,
            relevance=0.0,
            prediction_error_magnitude=0.0,
            temporal_pull=1.0,
        )
        current = AttentionCandidate(
            ExperienceDomain.REPAIR,
            intensity=0.25,
            relevance=0.2,
        )
        selected = compete_attention(
            (stale, current),
            continuity=TemporalContinuity(
                traces=(FocusTrace(ExperienceDomain.CONNECTION, 1.0),)
            ),
            capacity=1,
        )
        self.assertIs(ExperienceDomain.REPAIR, selected[0].domain)

        connection = AttentionCandidate(
            ExperienceDomain.CONNECTION,
            intensity=0.65,
            relevance=0.8,
            temporal_pull=1.0,
        )
        protection = AttentionCandidate(
            ExperienceDomain.PROTECTION,
            intensity=1.0,
            urgency=1.0,
            novelty=1.0,
            relevance=1.0,
            prediction_error_magnitude=1.0,
        )
        interrupted = compete_attention(
            (connection, protection),
            continuity=TemporalContinuity(
                traces=(FocusTrace(ExperienceDomain.CONNECTION, 1.0),)
            ),
            capacity=1,
        )
        self.assertIs(ExperienceDomain.PROTECTION, interrupted[0].domain)
        self.assertGreater(interrupted[0].salience, 0.9)


class DynamicCortexChecks(unittest.TestCase):
    def test_v4_payload_exposes_only_coarse_current_response_dynamics(self) -> None:
        first = integrate_experience(
            cues=Cues(safety=0.9, controllability=0.9),
            feeling=_packet(
                NOW,
                activation=0.2,
                valence=0.5,
                interaction_safety=0.9,
                agency=0.9,
                uncertainty=0.1,
                load=0.1,
            ),
        )
        second = integrate_experience(
            cues=Cues(
                threat=0.9,
                uncertainty=0.9,
                novelty=0.8,
                controllability=0.1,
                demand=0.8,
            ),
            feeling=_packet(
                NOW + timedelta(seconds=10),
                activation=0.9,
                valence=-0.6,
                interaction_safety=0.1,
                agency=0.1,
                uncertainty=0.9,
                load=0.9,
            ),
            previous=first,
        )
        payload = cortex_payload(second)
        message = cortex_developer_message(second)

        self.assertEqual("organism.experience.v4", payload["schema_version"])
        self.assertEqual(
            "current_response_short_lived_history",
            payload["temporal_scope"],
        )
        self.assertEqual(
            {
                "schema_version",
                "kind",
                "interpretation_scope",
                "authority",
                "temporal_scope",
                "self_model",
                "prediction",
                "regulatory_motifs",
                "dynamics",
                "workspace",
            },
            set(payload),
        )
        self.assertEqual(
            {"pattern", "flux", "volatility", "escalation", "settling"},
            set(payload["dynamics"]),
        )
        self.assertEqual("none", payload["authority"])
        for item in payload["workspace"]["focus"]:
            self.assertEqual(
                {
                    "domain",
                    "salience",
                    "share",
                    "intensity",
                    "urgency",
                    "prediction_error_magnitude",
                    "continuity",
                    "temporal_pull",
                },
                set(item),
            )
            self.assertGreaterEqual(item["temporal_pull"], 0.0)
            self.assertLessEqual(item["temporal_pull"], 1.0)

        encoded = json.dumps(payload, sort_keys=True)
        for forbidden in (
            "impulse",
            "momentum",
            "retention",
            "revision",
            "generated_at",
            "delta_seconds",
            "event_id",
            "actor_ref",
            "scope_ref",
            NOW.isoformat(),
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, encoded)
                self.assertNotIn(forbidden, message["content"])
        self.assertEqual("developer", message["role"])
        self.assertIn("current response", message["content"])
        self.assertIn("no permission", message["content"])


class FakeClock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: float) -> None:
        self.value += timedelta(**kwargs)


class DynamicServiceChecks(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.clock = FakeClock()

    def service(self, *, ttl_seconds: float = 120.0) -> OrganismService:
        registry = MemorySectorRegistry(
            self.root / f"state-{ttl_seconds}",
            identity_key=b"dynamic-identity-key-material-000001",
            learning_key=b"dynamic-learning-key-material-000002",
            owner_key=b"dynamic-owner-key-material-00000003",
            clock=self.clock,
        )
        return OrganismService(
            registry,
            operational_sensor=OperationalInteroception,
            experience_cache_ttl_seconds=ttl_seconds,
        )

    @staticmethod
    def frame(
        event_id: str,
        *,
        actor_ref: str | None = "member-dynamic",
        capability: Capability = Capability.CONVERSATION,
    ) -> SensorFrame:
        return SensorFrame(
            event_id=event_id,
            scope="ignored-by-service",
            actor_ref=actor_ref,
            capability=capability,
            external=ExternalSenses(
                safety=0.8,
                connection=0.7,
                uncertainty=0.2,
                novelty=0.2,
                controllability=0.8,
            ),
        )

    def controlled_result(self, frame: SensorFrame) -> FeelingWithCues:
        return FeelingWithCues(
            cues=frame.cues(),
            packet=_packet(self.clock()),
        )

    @staticmethod
    def cache_moment(service: OrganismService):
        entry = next(iter(service._experience_cache.values()))
        return entry.moment

    def test_same_event_is_idempotent_even_when_time_advances(self) -> None:
        service = self.service()
        frame = self.frame("same-event-private-sentinel")

        def controlled(_guild, selected_frame, _references):
            return self.controlled_result(selected_frame)

        with patch.object(
            service,
            "_feel_with_cues_operation",
            side_effect=controlled,
        ):
            _, first_messages = asyncio.run(
                service.cortex_input("guild-private-sentinel", frame, "first")
            )
            first = self.cache_moment(service)
            self.clock.advance(seconds=1)
            _, second_messages = asyncio.run(
                service.cortex_input("guild-private-sentinel", frame, "retry")
            )
            second = self.cache_moment(service)

        self.assertEqual(first, second)
        self.assertEqual(
            _experience_payload(first_messages),
            _experience_payload(second_messages),
        )
        cache_text = repr(service._experience_cache)
        self.assertNotIn("same-event-private-sentinel", cache_text)
        self.assertNotIn("guild-private-sentinel", cache_text)

    def test_actorless_calls_are_stateless(self) -> None:
        service = self.service()

        def controlled(_guild, selected_frame, _references):
            return self.controlled_result(selected_frame)

        with patch.object(
            service,
            "_feel_with_cues_operation",
            side_effect=controlled,
        ):
            _, first_messages = asyncio.run(
                service.cortex_input(
                    "guild-a",
                    self.frame("actorless-1", actor_ref=None),
                    "first actorless",
                )
            )
            self.clock.advance(seconds=10)
            _, second_messages = asyncio.run(
                service.cortex_input(
                    "guild-a",
                    self.frame("actorless-2", actor_ref=None),
                    "second actorless",
                )
            )

        self.assertEqual(0, service.experience_cache_size)
        payloads = (
            _experience_payload(first_messages),
            _experience_payload(second_messages),
        )
        self.assertEqual(payloads[0], payloads[1])
        for payload in payloads:
            self.assertEqual("initial", payload["dynamics"]["pattern"])

    def test_exact_expiry_resets_every_dynamic_accumulator(self) -> None:
        service = self.service(ttl_seconds=120.0)

        def controlled(_guild, selected_frame, _references):
            return self.controlled_result(selected_frame)

        with patch.object(
            service,
            "_feel_with_cues_operation",
            side_effect=controlled,
        ):
            _, first_messages = asyncio.run(
                service.cortex_input("guild-a", self.frame("expiry-1"), "first")
            )
            self.clock.advance(seconds=120)
            _, messages = asyncio.run(
                service.cortex_input("guild-a", self.frame("expiry-2"), "expired")
            )

        moment = self.cache_moment(service)
        self.assertEqual(ChangePattern.INITIAL, moment.dynamics.pattern)
        self.assertEqual(0, moment.dynamics.revision)
        self.assertEqual(_zero_axes(), dict(moment.dynamics.impulse))
        self.assertEqual(_zero_axes(), dict(moment.dynamics.momentum))
        payload = _experience_payload(messages)
        self.assertEqual("initial", payload["dynamics"]["pattern"])
        self.assertEqual(_experience_payload(first_messages), payload)

    def test_out_of_order_response_does_not_evict_newer_history(self) -> None:
        service = self.service()

        def controlled(_guild, selected_frame, _references):
            return self.controlled_result(selected_frame)

        with patch.object(
            service,
            "_feel_with_cues_operation",
            side_effect=controlled,
        ):
            asyncio.run(service.cortex_input("guild-a", self.frame("order-1"), "one"))
            self.clock.advance(seconds=10)
            asyncio.run(service.cortex_input("guild-a", self.frame("order-2"), "two"))
            newer = self.cache_moment(service)
            self.assertEqual(1, newer.dynamics.revision)

            self.clock.value = NOW + timedelta(seconds=5)
            _, stale_messages = asyncio.run(
                service.cortex_input("guild-a", self.frame("order-stale"), "stale")
            )
            self.assertEqual(newer, self.cache_moment(service))
            self.assertEqual(
                "initial",
                _experience_payload(stale_messages)["dynamics"]["pattern"],
            )

            self.clock.value = NOW + timedelta(seconds=20)
            asyncio.run(service.cortex_input("guild-a", self.frame("order-3"), "three"))

        latest = self.cache_moment(service)
        self.assertEqual(2, latest.dynamics.revision)
        self.assertGreater(latest.generated_at, newer.generated_at)

    def test_dynamic_history_stays_out_of_persistent_state(self) -> None:
        service = self.service()
        asyncio.run(
            service.cortex_input(
                "persistent-guild-sentinel",
                self.frame("persistent-event-sentinel"),
                "current user text",
            )
        )
        state_text = "".join(
            path.read_text(encoding="utf-8") for path in self.root.rglob("*.json")
        )
        for forbidden in (
            "impulse",
            "momentum",
            "volatility",
            "temporal_pull",
            "persistent-guild-sentinel",
            "persistent-event-sentinel",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, state_text)


if __name__ == "__main__":
    unittest.main()
