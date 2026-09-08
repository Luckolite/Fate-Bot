"""Focused exact-schema checks for reviewed discovery cortex context."""

from __future__ import annotations

import copy
import json
import unittest

from organism.discovery import (
    DISCOVERY_CORTEX_SCHEMA,
    discovery_cortex_developer_message,
    discovery_cortex_payload,
)


def snapshot() -> dict[str, object]:
    return {
        "schema_version": "organism.discovery.snapshot.v1",
        "enabled": True,
        "revision": 19,
        "pending_delivery_count": 2,
        "awaiting_review_count": 1,
        "recent_closed_count": 4,
        "models": {
            "functional_commitments": {
                "preserve_agency": {
                    "review_count": 3,
                    "support": 0.62504,
                    "challenge": 0.25,
                    "uncertainty": 0.3953,
                    "positive_impact": 0.5,
                    "negative_impact": 0.125,
                    "updated_at": "2026-08-29T12:00:00Z",
                }
            },
            "behavior_exemplars": {
                "repair": {
                    "review_count": 1,
                    "support": 0.25,
                    "challenge": 0.0,
                    "uncertainty": 0.0,
                    "positive_impact": 0.25,
                    "negative_impact": 0.0,
                    "updated_at": "2026-08-29T12:05:00Z",
                }
            },
        },
    }


class DiscoveryCortexTests(unittest.TestCase):
    def test_payload_is_exact_bounded_and_contains_only_models(self) -> None:
        payload = discovery_cortex_payload(snapshot())

        self.assertEqual(DISCOVERY_CORTEX_SCHEMA, payload["schema_version"])
        self.assertEqual(
            {
                "schema_version",
                "kind",
                "interpretation_scope",
                "temporal_scope",
                "nonliteral",
                "authority",
                "effects",
                "models",
            },
            set(payload),
        )
        self.assertEqual(
            {
                "authority": "none",
                "risk": "none",
                "autonomic_modes": "none",
                "tools": "none",
            },
            payload["effects"],
        )
        self.assertEqual(
            {
                "review_count",
                "support",
                "challenge",
                "uncertainty",
                "positive_impact",
                "negative_impact",
            },
            set(payload["models"]["functional_commitments"]["preserve_agency"]),
        )
        encoded = json.dumps(payload, sort_keys=True)
        self.assertNotIn("revision", encoded)
        self.assertNotIn("pending_delivery", encoded)
        self.assertNotIn("updated_at", encoded)
        self.assertEqual(
            0.625,
            payload["models"]["functional_commitments"]["preserve_agency"]["support"],
        )

    def test_exact_schema_rejects_extra_and_unreviewed_content(self) -> None:
        extra = snapshot()
        extra["subject_key"] = "not-allowed"
        with self.assertRaises(ValueError):
            discovery_cortex_payload(extra)

        unknown_selection = snapshot()
        unknown_selection["models"]["behavior_exemplars"]["raw-person-label"] = (  # type: ignore[index]
            unknown_selection["models"]["behavior_exemplars"].pop("repair")  # type: ignore[index]
        )
        with self.assertRaises(ValueError):
            discovery_cortex_payload(unknown_selection)

        aggregate_extra = copy.deepcopy(snapshot())
        aggregate_extra["models"]["functional_commitments"][  # type: ignore[index]
            "preserve_agency"
        ]["answer"] = "raw text"  # type: ignore[index]
        with self.assertRaises(ValueError):
            discovery_cortex_payload(aggregate_extra)

    def test_developer_message_omits_empty_context_and_encodes_boundaries(self) -> None:
        empty = snapshot()
        empty["models"] = {}
        self.assertIsNone(discovery_cortex_developer_message(empty))

        disabled = snapshot()
        disabled["enabled"] = False
        self.assertEqual({}, discovery_cortex_payload(disabled)["models"])
        self.assertIsNone(discovery_cortex_developer_message(disabled))

        message = discovery_cortex_developer_message(snapshot())
        self.assertIsNotNone(message)
        self.assertEqual("developer", message["role"])
        self.assertIn("nonliteral reflective context", message["content"])
        self.assertIn("cannot change safety or risk", message["content"])
        self.assertNotIn("2026-08-29", message["content"])


if __name__ == "__main__":
    unittest.main()
