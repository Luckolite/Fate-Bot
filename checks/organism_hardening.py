"""Cross-boundary regressions for Organism reliability and fast read paths."""

# ruff: noqa: E402 - direct script execution adds the repository root first.

from __future__ import annotations

import asyncio
import gc
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from organism.body import (
    ExternalSenses,
    OperationalInteroception,
    SensorFrame,
    memory_constraint_signal,
)
from organism.config import OrganismConfig
from organism.fate_service import (
    OrganismService,
    OrganismServiceStaleOperationError,
    sample_operational_interoception,
)
from organism.models import Capability, timestamp_text
from organism.sectors import MemorySectorRegistry
from organism.storage import JsonStateStore, StateStoreError, _PROCESS_LOCKS


NOW = datetime(2026, 8, 29, 16, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: float) -> None:
        self.value += timedelta(**kwargs)


def _experience_message(messages: list[dict[str, str]]) -> str:
    return next(
        message["content"]
        for message in messages
        if "<organism_experience>" in message["content"]
    )


class OrganismHardeningChecks(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.clock = FakeClock()

    def registry(
        self,
        *,
        state_name: str = "state",
        config_path: Path | None = None,
    ) -> MemorySectorRegistry:
        return MemorySectorRegistry(
            self.root / state_name,
            identity_key=b"hardening-identity-key-material-0001",
            learning_key=b"hardening-learning-key-material-0002",
            owner_key=b"hardening-owner-key-material-000003",
            config_path=config_path,
            clock=self.clock,
        )

    @staticmethod
    def frame(event_id: str, *, uncertainty: float = 0.2) -> SensorFrame:
        return SensorFrame(
            event_id=event_id,
            actor_ref="configured-member",
            capability=Capability.CONVERSATION,
            external=ExternalSenses(
                safety=0.8,
                connection=0.7,
                uncertainty=uncertainty,
                novelty=0.3,
                controllability=0.8,
            ),
        )

    def test_retry_precedes_live_sensor_and_reuses_coherent_packet(self) -> None:
        samples = iter(
            (
                OperationalInteroception(memory_pressure=0.1),
                OperationalInteroception(memory_pressure=1.0),
            )
        )
        sensor_calls = 0

        def sensor() -> OperationalInteroception:
            nonlocal sensor_calls
            sensor_calls += 1
            return next(samples)

        service = OrganismService(self.registry(), operational_sensor=sensor)
        frame = self.frame("stable-retry")
        first_packet, first_messages = asyncio.run(
            service.cortex_input("guild-a", frame, "first text")
        )
        second_packet, second_messages = asyncio.run(
            service.cortex_input("guild-a", frame, "replacement text")
        )

        self.assertEqual(1, sensor_calls)
        self.assertEqual(first_packet, second_packet)
        self.assertEqual(
            _experience_message(first_messages),
            _experience_message(second_messages),
        )
        self.assertEqual("replacement text", second_messages[-1]["content"])

    def test_older_replay_does_not_advance_or_replace_latest_experience(self) -> None:
        service = OrganismService(
            self.registry(),
            operational_sensor=OperationalInteroception,
        )
        first_packet, first_messages = asyncio.run(
            service.cortex_input("guild-a", self.frame("event-a"), "A")
        )
        self.clock.advance(seconds=1)
        asyncio.run(service.cortex_input("guild-a", self.frame("event-b"), "B"))
        entry_before = next(iter(service._experience_cache.values()))
        latest_before = entry_before.event_token
        self.clock.advance(seconds=1)
        replay_packet, replay_messages = asyncio.run(
            service.cortex_input("guild-a", self.frame("event-a"), "A replay")
        )
        entry_after = next(iter(service._experience_cache.values()))

        self.assertEqual(first_packet, replay_packet)
        self.assertEqual(latest_before, entry_after.event_token)
        self.assertEqual(2, len(entry_after.records))
        self.assertEqual(
            _experience_message(first_messages),
            _experience_message(replay_messages),
        )

    def test_replay_does_not_bypass_large_clock_rollback_guard(self) -> None:
        service = OrganismService(
            self.registry(),
            operational_sensor=OperationalInteroception,
        )
        frame = self.frame("clock-bound-retry")
        asyncio.run(service.cortex_input("guild-a", frame, "first"))
        self.clock.value -= timedelta(seconds=301)

        with self.assertRaisesRegex(StateStoreError, "cached experience"):
            asyncio.run(service.cortex_input("guild-a", frame, "retry"))

    def test_queued_sensing_cannot_recreate_data_after_forgetting(self) -> None:
        registry = self.registry()
        service = OrganismService(
            registry,
            operational_sensor=OperationalInteroception,
        )

        async def scenario() -> None:
            release = asyncio.Event()
            original = service._run_in_worker

            async def delayed(operation, *args, **kwargs):
                await release.wait()
                return await original(operation, *args, **kwargs)

            patcher = patch.object(service, "_run_in_worker", new=delayed)
            patcher.start()
            try:
                queued = asyncio.create_task(
                    service.cortex_input(
                        "guild-a",
                        self.frame("queued-before-forget"),
                        "queued",
                    )
                )
                await asyncio.sleep(0)
            finally:
                patcher.stop()

            await service.forget_member("guild-a", "configured-member")
            release.set()
            await queued

        with self.assertRaisesRegex(
            OrganismServiceStaleOperationError,
            "privacy deletion",
        ):
            asyncio.run(scenario())
        self.assertEqual(0, registry.sector("guild-a").inspect()["transient_state_count"])

    def test_cross_service_deletion_barrier_rejects_older_queued_work(self) -> None:
        registry_a = self.registry(state_name="shared-state")
        registry_b = self.registry(state_name="shared-state")
        service_a = OrganismService(
            registry_a,
            operational_sensor=OperationalInteroception,
        )
        service_b = OrganismService(
            registry_b,
            operational_sensor=OperationalInteroception,
        )

        async def scenario() -> None:
            release = asyncio.Event()
            original = service_a._run_in_worker

            async def delayed(operation, *args, **kwargs):
                await release.wait()
                return await original(operation, *args, **kwargs)

            patcher = patch.object(service_a, "_run_in_worker", new=delayed)
            patcher.start()
            try:
                queued = asyncio.create_task(
                    service_a.cortex_input(
                        "guild-a",
                        self.frame("queued-on-service-a"),
                        "queued",
                    )
                )
                await asyncio.sleep(0)
            finally:
                patcher.stop()

            await service_b.purge_guild("guild-a")
            release.set()
            await queued

        with self.assertRaisesRegex(
            OrganismServiceStaleOperationError,
            "privacy deletion",
        ):
            asyncio.run(scenario())
        self.assertEqual(
            0,
            registry_a.sector("guild-a").inspect()["transient_state_count"],
        )

        asyncio.run(
            service_a.cortex_input(
                "guild-a",
                self.frame("appropriate-retry"),
                "retry",
            )
        )
        self.assertEqual(
            1,
            registry_a.sector("guild-a").inspect()["transient_state_count"],
        )

    def test_cross_service_deletion_invalidates_process_local_replay(self) -> None:
        registry_a = self.registry(state_name="shared-replay-state")
        registry_b = self.registry(state_name="shared-replay-state")
        service_a = OrganismService(
            registry_a,
            operational_sensor=OperationalInteroception,
        )
        service_b = OrganismService(
            registry_b,
            operational_sensor=OperationalInteroception,
        )
        frame = self.frame("cached-before-cross-service-forget")
        before, _ = asyncio.run(
            service_a.cortex_input("guild-a", frame, "before")
        )

        asyncio.run(service_b.forget_member("guild-a", "configured-member"))
        with self.assertRaisesRegex(
            OrganismServiceStaleOperationError,
            "privacy deletion",
        ):
            asyncio.run(service_a.cortex_input("guild-a", frame, "stale retry"))

        after, _ = asyncio.run(
            service_a.cortex_input("guild-a", frame, "appropriate retry")
        )
        self.assertIsNot(before, after)
        self.assertEqual(
            1,
            registry_a.sector("guild-a").inspect()["transient_state_count"],
        )

    def test_global_stale_revision_evicts_replays_from_every_domain(self) -> None:
        registry_a = self.registry(state_name="shared-domain-replay-state")
        registry_b = self.registry(state_name="shared-domain-replay-state")
        service_a = OrganismService(
            registry_a,
            operational_sensor=OperationalInteroception,
        )
        service_b = OrganismService(
            registry_b,
            operational_sensor=OperationalInteroception,
        )
        frame_a = self.frame("cached-guild-a")
        frame_b = self.frame("cached-guild-b")
        asyncio.run(service_a.cortex_input("guild-a", frame_a, "guild a"))
        before_b, _ = asyncio.run(
            service_a.cortex_input("guild-b", frame_b, "guild b")
        )

        asyncio.run(service_b.forget_member("guild-b", "configured-member"))
        with self.assertRaises(OrganismServiceStaleOperationError):
            asyncio.run(
                service_a.cortex_input("guild-a", frame_a, "consume stale marker")
            )

        after_b, _ = asyncio.run(
            service_a.cortex_input("guild-b", frame_b, "appropriate retry")
        )
        self.assertIsNot(before_b, after_b)

    def test_privacy_barrier_keeps_independent_sectors_concurrent(self) -> None:
        registry = self.registry()
        revision = registry.privacy_deletion_revision
        first_started = threading.Event()
        second_started = threading.Event()
        release = threading.Event()

        def hold(guild_ref: str, started: threading.Event) -> None:
            with registry.privacy_operation(
                revision,
                sector_refs=(guild_ref,),
            ):
                started.set()
                if not release.wait(timeout=2):
                    raise TimeoutError("privacy-domain concurrency test timed out")

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(hold, "guild-a", first_started)
            second = executor.submit(hold, "guild-b", second_started)
            try:
                self.assertTrue(first_started.wait(timeout=1))
                self.assertTrue(second_started.wait(timeout=1))
            finally:
                release.set()
            first.result(timeout=2)
            second.result(timeout=2)

    def test_disjoint_deletions_atomically_advance_the_durable_revision(self) -> None:
        registry = self.registry()

        def erase(guild_ref: str) -> None:
            with registry.privacy_deletion(sector_refs=(guild_ref,)):
                pass

        guilds = tuple(f"guild-{index}" for index in range(16))
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = tuple(executor.submit(erase, guild_ref) for guild_ref in guilds)
            for future in futures:
                future.result(timeout=5)

        self.assertEqual(len(guilds), registry.privacy_deletion_revision)

    def test_explicit_key_loss_recovery_purges_every_known_state_domain(self) -> None:
        registry = self.registry(state_name="key-loss-state")
        registry.sector("guild-a").inspect()
        shared = registry.state_directory / "shared"
        shared.mkdir(parents=True)
        (shared / "sensory.json").write_text("old shared state", encoding="utf-8")
        (registry.state_directory / "discovery.json").write_text(
            "old discovery state",
            encoding="utf-8",
        )
        with registry.privacy_deletion(sector_refs=("guild-a",)):
            pass
        sector_path = next(
            (registry.state_directory / "sectors").glob("*.json")
        )
        temporary_paths = (
            sector_path.parent / f".{sector_path.name}.orphan.tmp",
            shared / ".sensory.json.orphan.tmp",
            registry.state_directory / ".discovery.json.orphan.tmp",
            registry.state_directory / ".privacy-lifecycle.json.orphan.tmp",
        )
        for path in temporary_paths:
            path.write_text("orphaned sensitive state", encoding="utf-8")
        sector_bytes = sector_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "at least 32 bytes"):
            MemorySectorRegistry.purge_all_state_after_key_loss(
                registry.state_directory,
                owner_key=b"too-short",
            )
        self.assertEqual(sector_bytes, sector_path.read_bytes())
        self.assertTrue(all(path.exists() for path in temporary_paths))

        replacement_owner = b"replacement-owner-key-material-00004"
        with self.assertRaisesRegex(StateStoreError, "lifecycle authentication"):
            MemorySectorRegistry(
                registry.state_directory,
                identity_key=b"hardening-identity-key-material-0001",
                learning_key=b"hardening-learning-key-material-0002",
                owner_key=replacement_owner,
                clock=self.clock,
            )

        removed = MemorySectorRegistry.purge_all_state_after_key_loss(
            registry.state_directory,
            owner_key=replacement_owner,
        )
        self.assertEqual(1, removed["sector_states_removed"])
        self.assertEqual(1, removed["shared_states_removed"])
        self.assertEqual(1, removed["discovery_states_removed"])
        self.assertEqual(4, removed["temporary_files_removed"])
        self.assertTrue(removed["privacy_marker_replaced"])
        recovered = MemorySectorRegistry(
            registry.state_directory,
            identity_key=b"hardening-identity-key-material-0001",
            learning_key=b"hardening-learning-key-material-0002",
            owner_key=replacement_owner,
            clock=self.clock,
        )
        self.assertEqual(0, recovered.privacy_deletion_revision)
        self.assertEqual([], list((registry.state_directory / "sectors").glob("*.json")))
        self.assertEqual([], list(shared.glob("*.json")))
        self.assertFalse((registry.state_directory / "discovery.json").exists())
        self.assertFalse(any(path.exists() for path in temporary_paths))

    def test_changed_frame_reusing_event_fails_before_sensor_or_state_write(self) -> None:
        sensor_calls = 0

        def sensor() -> OperationalInteroception:
            nonlocal sensor_calls
            sensor_calls += 1
            return OperationalInteroception()

        registry = self.registry()
        service = OrganismService(registry, operational_sensor=sensor)
        asyncio.run(
            service.cortex_input("guild-a", self.frame("bound-event"), "first")
        )
        state_path = (
            registry.state_directory
            / "sectors"
            / f"{registry.sector_id('guild-a')}.json"
        )
        before = state_path.read_bytes()

        with self.assertRaisesRegex(ValueError, "different organism inputs"):
            asyncio.run(
                service.cortex_input(
                    "guild-a",
                    self.frame("bound-event", uncertainty=0.9),
                    "changed",
                )
            )

        self.assertEqual(1, sensor_calls)
        self.assertEqual(before, state_path.read_bytes())

    def test_whitespace_is_rejected_before_creating_state(self) -> None:
        registry = self.registry()
        service = OrganismService(
            registry,
            operational_sensor=OperationalInteroception,
        )
        with self.assertRaisesRegex(ValueError, "non-whitespace"):
            asyncio.run(
                service.cortex_input("guild-a", self.frame("blank"), " \t\n")
            )
        self.assertFalse((registry.state_directory / "sectors").exists())

    def test_registry_pins_one_calibration_for_future_sectors(self) -> None:
        source = REPOSITORY_ROOT / "organism" / "defaults.json"
        config_path = self.root / "calibration.json"
        config_path.write_bytes(source.read_bytes())
        registry = self.registry(config_path=config_path)
        first = registry.sector("guild-a").config

        changed = json.loads(config_path.read_text(encoding="utf-8"))
        changed["baseline_state"]["activation"] = 0.99
        config_path.write_text(json.dumps(changed), encoding="utf-8")
        second = registry.sector("guild-b").config

        self.assertIs(first, second)
        self.assertNotEqual(0.99, second.baseline_state["activation"])

    def test_directly_injected_config_is_fully_revalidated(self) -> None:
        calibration = OrganismConfig.load()

        with self.assertRaisesRegex(
            ValueError,
            "prompt_ttl_seconds must be a positive integer",
        ):
            replace(calibration, prompt_ttl_seconds=-1)
        with self.assertRaisesRegex(
            ValueError,
            "strategy_baselines must contain every strategy",
        ):
            replace(calibration, strategy_baselines={})
        for name, value in (
            ("prompt_ttl_seconds", 10**20),
            ("max_future_skew_seconds", 10**20),
            ("max_observation_age_seconds", 10**20),
            ("severe_guard_hours", 1e300),
            ("recovery_half_life_hours", 1e300),
            ("transient_state_retention_hours", 1e300),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(
                ValueError,
                rf"{name} cannot exceed",
            ):
                replace(calibration, **{name: value})
        for name in ("recent_observation_limit", "max_transient_states"):
            with self.subTest(name=name), self.assertRaisesRegex(
                ValueError,
                rf"{name} cannot exceed 256",
            ):
                replace(calibration, **{name: 257})

    def test_existing_inspections_are_byte_for_byte_read_only_and_batched(self) -> None:
        engine = self.registry().sector("guild-a")
        engine.inspect()
        before = engine.store.path.read_bytes()
        with patch.object(engine, "_read", wraps=engine._read) as read:
            profiles = engine.inspect_profiles(
                scope="local",
                actor_refs=(f"actor-{index}" for index in range(8)),
            )
        self.assertEqual(8, len(profiles))
        self.assertEqual(1, read.call_count)
        self.assertEqual(before, engine.store.path.read_bytes())
        with self.assertRaisesRegex(ValueError, "capability is not supported"):
            engine.inspect_profiles(
                scope="local",
                actor_refs=(),
                capability="not-a-capability",  # type: ignore[arg-type]
            )

    def test_memory_pressure_is_quiet_then_increasingly_constraining(self) -> None:
        self.assertEqual(0.0, memory_constraint_signal(0.0))
        self.assertEqual(0.0, memory_constraint_signal(0.70))
        signals = tuple(memory_constraint_signal(value) for value in (0.8, 0.9, 1.0))
        self.assertLess(signals[0], signals[1])
        self.assertLess(signals[1], signals[2])
        self.assertGreater(signals[2] - signals[1], signals[1] - signals[0])

        with patch("organism.fate_service.psutil.virtual_memory") as memory:
            memory.return_value.percent = 50.0
            ordinary = sample_operational_interoception()
            memory.return_value.percent = 95.0
            constrained = sample_operational_interoception()
        self.assertEqual(1.0, ordinary.energy_reserve)
        self.assertLess(constrained.energy_reserve, ordinary.energy_reserve)

    def test_idle_per_path_locks_do_not_accumulate(self) -> None:
        baseline = len(_PROCESS_LOCKS)
        for index in range(256):
            JsonStateStore(
                self.root / f"lock-{index}.json",
                validator=lambda _payload: None,
            ).read(dict)
        gc.collect()
        self.assertLessEqual(len(_PROCESS_LOCKS), baseline + 1)

    def test_transient_replay_history_has_a_global_bound(self) -> None:
        engine = self.registry().sector("guild-a")
        payload = engine._fresh(NOW)
        baseline_state = engine._baseline_state(NOW).to_dict()
        for state_index in range(20):
            state_key = f"state-{state_index:02d}"
            payload["states"][state_key] = baseline_state
            payload["dominant_modes"][state_key] = "social_engagement"
            payload["state_access"][state_key] = timestamp_text(
                NOW + timedelta(seconds=state_index)
            )
            payload["recent_observations"][state_key] = [
                {"event_key": f"event-{state_index}-{event}", "fingerprint": None}
                for event in range(256)
            ]
        payload["last_state_key"] = "state-19"

        engine._prune_transient_states(payload, NOW, keep="state-19")

        total = sum(
            len(history) for history in payload["recent_observations"].values()
        )
        self.assertLessEqual(total, 4096)
        self.assertEqual(256, len(payload["recent_observations"]["state-19"]))


if __name__ == "__main__":
    unittest.main()
