from __future__ import annotations

import json
import math
import random
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path

from organism import (
    AutonomicMode,
    Cues,
    Observation,
    Organism,
    OrganismicState,
    ProtectivePosture,
    RegulatoryMotifs,
    StrategyName,
    derive_regulatory_motifs,
)
from organism.autonomic.coordinator import synthesize_autonomic_response
from organism.config import OrganismConfig
from organism.experience import cortex_payload, integrate_experience
from organism.models import GUIDANCE_FIELDS, ProtectionSummary

NOW = datetime(2026, 8, 29, 12, tzinfo=timezone.utc)


def state(**changes: float) -> OrganismicState:
    values = {
        "activation": 0.2,
        "pleasantness": 0.2,
        "interaction_safety": 0.8,
        "agency": 0.8,
        "uncertainty": 0.2,
        "load": 0.2,
    }
    values.update(changes)
    return OrganismicState(**values, updated_at=NOW)


class RegulatoryMotifTests(unittest.TestCase):
    def test_contract_is_immutable_finite_and_bounded(self) -> None:
        motifs = RegulatoryMotifs(pain=0.2, panic=0.3, riddle=0.4)

        self.assertEqual(
            {"pain": 0.2, "panic": 0.3, "riddle": 0.4},
            motifs.to_dict(),
        )
        with self.assertRaises(FrozenInstanceError):
            motifs.pain = 1.0
        for invalid in (True, -0.1, 1.1, math.nan, math.inf):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                RegulatoryMotifs(pain=invalid)
        with self.assertRaises(ValueError):
            derive_regulatory_motifs(Cues(), state(), math.nan)

    def test_canonical_conditions_separate_pain_panic_and_riddle(self) -> None:
        calm = derive_regulatory_motifs(
            Cues(
                safety=0.95,
                connection=0.8,
                uncertainty=0.05,
                controllability=0.95,
                communication_clarity=0.95,
            ),
            state(
                activation=0.1,
                pleasantness=0.5,
                interaction_safety=0.9,
                agency=0.9,
                uncertainty=0.05,
                load=0.1,
            ),
            0.03,
        )
        resource_strain = derive_regulatory_motifs(
            Cues(
                safety=0.8,
                uncertainty=0.2,
                demand=1.0,
                controllability=0.65,
                energy_cost=1.0,
                communication_clarity=0.8,
            ),
            state(
                activation=0.65,
                pleasantness=-0.6,
                agency=0.5,
                uncertainty=0.35,
                load=0.95,
            ),
            0.25,
        )
        acute_alarm = derive_regulatory_motifs(
            Cues(
                safety=0.0,
                connection=0.1,
                threat=1.0,
                boundary_pressure=0.8,
                uncertainty=0.9,
                novelty=0.6,
                demand=0.8,
                controllability=0.0,
                energy_cost=0.5,
                communication_clarity=0.3,
            ),
            state(
                activation=0.95,
                pleasantness=-0.7,
                interaction_safety=0.1,
                agency=0.1,
                uncertainty=0.9,
                load=0.8,
            ),
            0.9,
        )
        safe_unknown = derive_regulatory_motifs(
            Cues(
                safety=0.95,
                connection=0.7,
                uncertainty=0.9,
                novelty=0.95,
                demand=0.2,
                controllability=0.95,
                energy_cost=0.1,
                communication_clarity=0.25,
                engagement=0.85,
            ),
            state(
                activation=0.25,
                pleasantness=0.3,
                interaction_safety=0.9,
                agency=0.9,
                uncertainty=0.5,
            ),
            0.1,
        )

        self.assertEqual(RegulatoryMotifs(), calm)
        self.assertGreater(resource_strain.pain, 0.6)
        self.assertLess(resource_strain.panic, 0.1)
        self.assertGreater(acute_alarm.panic, 0.8)
        self.assertEqual(0.0, acute_alarm.riddle)
        self.assertGreater(safe_unknown.riddle, 0.75)
        self.assertLess(safe_unknown.panic, 0.1)

    def test_panic_and_riddle_require_their_documented_gates(self) -> None:
        fully_controlled_alarm = derive_regulatory_motifs(
            Cues(
                safety=0.0,
                threat=1.0,
                uncertainty=0.0,
                novelty=0.0,
                controllability=1.0,
                communication_clarity=1.0,
            ),
            state(
                activation=0.0,
                interaction_safety=0.2,
                agency=1.0,
                uncertainty=0.0,
            ),
            0.9,
        )
        unsafe_powerless_unknown = derive_regulatory_motifs(
            Cues(
                safety=0.0,
                uncertainty=1.0,
                novelty=1.0,
                controllability=0.0,
                communication_clarity=0.0,
            ),
            state(
                activation=0.1,
                interaction_safety=0.0,
                agency=0.0,
                uncertainty=1.0,
            ),
            0.1,
        )

        self.assertEqual(0.0, fully_controlled_alarm.panic)
        self.assertEqual(0.0, unsafe_powerless_unknown.riddle)

    def test_motifs_shape_style_without_changing_modes_or_authority(self) -> None:
        config = OrganismConfig.load()
        cues = Cues(safety=0.7, uncertainty=0.45, controllability=0.7)
        current = state(activation=0.4, uncertainty=0.45, load=0.35)
        protection = ProtectionSummary(
            posture=ProtectivePosture.OPEN,
            caution=0.1,
            confidence=0.8,
            uncertainty=0.2,
        )
        affinities = {name.value: 0.5 for name in StrategyName}

        def synthesis(motifs: RegulatoryMotifs):
            return synthesize_autonomic_response(
                state=current,
                cues=cues,
                protection=protection,
                predicted_risk=0.2,
                strategy_affinities=affinities,
                previous_mode=None,
                config=config,
                motifs=motifs,
            )

        base_modes, base_dominant, base, base_recruitments = synthesis(
            RegulatoryMotifs()
        )
        riddle_modes, riddle_dominant, riddle, riddle_recruitments = synthesis(
            RegulatoryMotifs(riddle=1.0)
        )
        alarm_modes, alarm_dominant, alarm, alarm_recruitments = synthesis(
            RegulatoryMotifs(pain=0.8, panic=0.9)
        )

        self.assertEqual(base_modes, riddle_modes)
        self.assertEqual(base_modes, alarm_modes)
        self.assertIs(base_dominant, riddle_dominant)
        self.assertIs(base_dominant, alarm_dominant)
        self.assertEqual(base_recruitments, riddle_recruitments)
        self.assertEqual(base_recruitments, alarm_recruitments)
        self.assertGreater(riddle["curiosity"], base["curiosity"])
        self.assertGreater(riddle["verification"], base["verification"])
        self.assertEqual(riddle["firmness"], base["firmness"])
        self.assertEqual(riddle["human_review_hint"], base["human_review_hint"])
        self.assertGreater(alarm["brevity"], base["brevity"])
        self.assertGreater(alarm["pacing_restraint"], base["pacing_restraint"])
        self.assertGreater(alarm["verification"], base["verification"])
        self.assertLess(alarm["tool_autonomy_scale"], base["tool_autonomy_scale"])
        self.assertEqual(alarm["human_review_hint"], base["human_review_hint"])
        self.assertEqual(GUIDANCE_FIELDS, set(alarm))

    def test_randomized_motifs_are_deterministic_and_bounded(self) -> None:
        rng = random.Random(20260829)
        cue_names = tuple(Cues().__dict__)
        for _ in range(500):
            cues = Cues(**{name: rng.random() for name in cue_names})
            current = OrganismicState(
                activation=rng.random(),
                pleasantness=2.0 * rng.random() - 1.0,
                interaction_safety=rng.random(),
                agency=rng.random(),
                uncertainty=rng.random(),
                load=rng.random(),
                updated_at=NOW,
            )
            risk = rng.random()

            first = derive_regulatory_motifs(cues, current, risk)
            second = derive_regulatory_motifs(cues, current, risk)

            self.assertEqual(first, second)
            self.assertTrue(
                all(
                    math.isfinite(value) and 0.0 <= value <= 1.0
                    for value in first.to_dict().values()
                )
            )

    def test_packet_and_workspace_are_versioned_without_persistence(self) -> None:
        cues = Cues(
            safety=0.95,
            connection=0.7,
            uncertainty=0.9,
            novelty=0.95,
            controllability=0.95,
            communication_clarity=0.25,
            engagement=0.85,
        )
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            organism = Organism(state_path, clock=lambda: NOW)
            observation = Observation(event_id="riddle-1", scope="local", cues=cues)
            first = organism.feel(observation)
            duplicate = organism.feel(observation)
            prompt = first.prompt_payload()
            experience = cortex_payload(
                integrate_experience(cues=cues, feeling=first, capacity=3)
            )
            persisted = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(first.state, duplicate.state)
        self.assertEqual(first.motifs, duplicate.motifs)
        self.assertEqual("organism.feeling.v3", prompt["schema_version"])
        self.assertEqual("organism.experience.v4", experience["schema_version"])
        self.assertEqual(
            prompt["regulatory_motifs"],
            experience["regulatory_motifs"],
        )
        self.assertGreater(prompt["regulatory_motifs"]["riddle"], 0.65)
        self.assertEqual(
            {"pain", "panic", "riddle"},
            set(prompt["regulatory_motifs"]),
        )
        self.assertEqual(
            {
                "schema_version",
                "kind",
                "generated_at",
                "expires_at",
                "mode_weights",
                "dominant_mode",
                "regulatory_motifs",
                "response_guidance",
                "recruited_strategies",
                "constraints",
            },
            set(prompt),
        )
        self.assertTrue(
            all(
                set(recruitment) == {"name", "strength"}
                for recruitment in prompt["recruited_strategies"]
            )
        )
        self.assertEqual(
            {mode.value for mode in AutonomicMode},
            set(prompt["mode_weights"]),
        )
        self.assertEqual(GUIDANCE_FIELDS, set(prompt["response_guidance"]))
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
            set(experience),
        )
        self.assertEqual(
            {
                "dominant_need",
                "posture",
                "resource_pressure",
                "connection_readiness",
                "profile_presence",
                "self_expression",
                "profile_change",
                "familiarity",
                "reciprocity",
                "personalization_consent",
                "social_exposure",
                "interaction_continuity",
                "communication_clarity",
                "engagement",
                "support_availability",
            },
            set(experience["self_model"]),
        )
        self.assertEqual(
            {"surprise", "confidence"},
            set(experience["prediction"]),
        )
        self.assertEqual(
            {"pattern", "flux", "volatility", "escalation", "settling"},
            set(experience["dynamics"]),
        )
        self.assertEqual(
            {"capacity", "overall_salience", "focus"},
            set(experience["workspace"]),
        )
        self.assertTrue(
            all(
                set(item)
                == {
                    "domain",
                    "salience",
                    "share",
                    "intensity",
                    "urgency",
                    "prediction_error_magnitude",
                    "continuity",
                    "temporal_pull",
                }
                for item in experience["workspace"]["focus"]
            )
        )

        def persisted_keys(value: object) -> set[str]:
            if isinstance(value, dict):
                return set(value) | {
                    key for child in value.values() for key in persisted_keys(child)
                }
            if isinstance(value, list):
                return {key for child in value for key in persisted_keys(child)}
            return set()

        self.assertFalse(
            {
                "regulatory_motifs",
                "motifs",
                "pain",
                "panic",
                "riddle",
                "dynamics",
                "momentum",
                "impulse",
            }
            & persisted_keys(persisted)
        )
        self.assertTrue(
            all(mode.value in prompt["mode_weights"] for mode in AutonomicMode)
        )


if __name__ == "__main__":
    unittest.main()
