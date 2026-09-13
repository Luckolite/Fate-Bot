"""Run with python -m unittest checks.global_xp_guard.

Optional real MySQL tests use an isolated localhost server named by
FATE_XP_TEST_PORT (root, empty password), creating/dropping a unique test database.
Never point this at Fate's application database.
"""

import asyncio
import json
import os
import unittest
from dataclasses import asdict
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from botutils.global_xp_guard import Activity, DAY, GlobalXPGuard
from cogs.core.ranking import Ranking


START = 1800000000  # Half-hour aligned, safely away from the epoch sentinel.


class ActivityChecks(unittest.TestCase):
    def test_short_repetition_is_not_a_flag(self):
        state = Activity()
        for i in range(100):
            self.assertEqual(state.observe(START + 10 * i, i + 1), (True, None))

    def test_sustained_machine_timing_flags_and_expires(self):
        state = Activity()
        for i in range(720):
            self.assertEqual(state.observe(START + 10 * i, i + 1), (True, None))
        award, flag = state.observe(START + 7200, 721)
        self.assertFalse(award)
        self.assertEqual(flag["reason"], "regular_cadence")
        end = state.blocked_until
        self.assertEqual(end, START + 7200 + DAY)
        self.assertEqual(state.observe(end - 1, 722), (False, None))
        self.assertEqual(state.blocked_until, end)
        self.assertEqual(state.observe(end, 723), (True, None))

    def test_repeat_penalties_and_decay(self):
        state = Activity()
        now = START
        message = 0
        for duration in (DAY, 3 * DAY, 7 * DAY):
            for i in range(721):
                message += 1
                award, flag = state.observe(now + i * 10, message)
            self.assertIsNotNone(flag)
            self.assertEqual(state.blocked_until - (now + 7200), duration)
            now = state.blocked_until
        now += 31 * DAY
        for i in range(721):
            message += 1
            award, flag = state.observe(now + i * 10, message)
        self.assertEqual(state.strikes, 1)

    def test_dense_randomized_activity_flags_after_36_hours(self):
        state = Activity()
        now, i = START, 0
        while now < START + 36 * 3600:
            i += 1
            self.assertIsNone(state.observe(now, i)[1])
            now += (67, 83, 101, 109)[i % 4]
        award, flag = state.observe(now, i + 1)
        self.assertFalse(award)
        self.assertEqual(flag["reason"], "sustained_activity")

    def test_normal_long_days_with_sleep_do_not_flag(self):
        state = Activity()
        i = 0
        for day in range(4):
            now = START + day * DAY
            while now < START + day * DAY + 20 * 3600:
                i += 1
                self.assertIsNone(state.observe(now, i)[1])
                now += (33, 45, 59, 81)[i % 4]

    def test_sparse_overnight_activity_does_not_flag(self):
        state = Activity()
        for i in range(600):
            self.assertIsNone(state.observe(START + i * 600, i + 1)[1])

    def test_checkpoint_preserves_cadence_and_cooldown(self):
        state = Activity()
        for i in range(720):
            state.observe(START + i * 10, i + 1)
        restored = Activity(**json.loads(json.dumps(asdict(state))))
        self.assertFalse(restored.observe(START + 7191, 800)[0])
        self.assertEqual(restored.observe(START + 7200, 801)[1]["reason"], "regular_cadence")

    def test_replayed_ids_and_clock_reversal_do_not_award(self):
        state = Activity()
        self.assertTrue(state.observe(START, 100)[0])
        self.assertFalse(state.observe(START + 20, 100)[0])
        self.assertFalse(state.observe(START - 20, 101)[0])
        self.assertTrue(state.observe(START + 20, 102)[0])


class ListenerChecks(unittest.IsolatedAsyncioTestCase):
    def ranking(self):
        ranking = object.__new__(Ranking)
        config = dict(Ranking.default_config)
        class Config:
            def __getitem__(self, key):
                async def get():
                    return config
                return get()
        ranking.config = Config()
        ranking.bot = SimpleNamespace(pool=True, log=Mock())
        ranking.global_cd = {}
        ranking.spam_cd = {}
        ranking.cd = {}
        ranking.global_guard = SimpleNamespace(observe=AsyncMock(return_value=False))
        ranking.get_cached_xp = AsyncMock(return_value=0)
        ranking.handle_level_roles = AsyncMock()
        ranking.xp_cache = {}
        ranking.xp_cache_access = {}
        ranking.pending_guild = {}
        ranking.pending_monthly = {}
        return ranking

    def message(self, age=0, webhook=None, bot=False):
        return SimpleNamespace(
            guild=SimpleNamespace(id=1), author=SimpleNamespace(id=2, bot=bot),
            channel=SimpleNamespace(id=3, parent_id=None), id=100, webhook_id=webhook,
            created_at=datetime.fromtimestamp(START - age, tz=timezone.utc),
        )

    async def test_global_block_does_not_stop_server_xp(self):
        ranking = self.ranking()
        with patch("cogs.core.ranking.time", return_value=START):
            await ranking.on_message(self.message())
        ranking.global_guard.observe.assert_awaited_once_with(2, 100, START)
        self.assertEqual(ranking.pending_guild[(1, 2)], 1)

    async def test_storage_failure_does_not_award_global_or_stop_server(self):
        ranking = self.ranking()
        ranking.global_guard.observe.side_effect = RuntimeError("database unavailable")
        with patch("cogs.core.ranking.time", return_value=START):
            await ranking.on_message(self.message())
        self.assertEqual(ranking.pending_guild[(1, 2)], 1)
        ranking.bot.log.assert_called_once()

    async def test_bots_webhooks_and_old_messages_do_not_reach_guard(self):
        for message in (self.message(age=121), self.message(webhook=42), self.message(bot=True)):
            ranking = self.ranking()
            with patch("cogs.core.ranking.time", return_value=START):
                await ranking.on_message(message)
            ranking.global_guard.observe.assert_not_awaited()

    async def test_global_only_flush_invalidates_leaderboard_cache(self):
        ranking = self.ranking()
        ranking.global_guard.flush = AsyncMock(return_value=True)
        ranking.pending_commands = {}
        ranking.flush_lock = asyncio.Lock()
        ranking.leaderboard_cache = {1: "old rankings"}
        await ranking.flush_pending()
        self.assertEqual(ranking.leaderboard_cache, {})

    async def test_disabled_channels_do_not_reach_guard(self):
        ranking = self.ranking()
        ranking.xp_disabled_in_channel = Mock(return_value=True)
        await ranking.on_message(self.message())
        ranking.global_guard.observe.assert_not_awaited()
        self.assertEqual(ranking.pending_guild, {})


@unittest.skipUnless(os.getenv("FATE_XP_TEST_PORT"), "isolated MySQL not requested")
class DatabaseChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiomysql
        self.database = "xp_guard_test_" + uuid4().hex
        config = dict(host="127.0.0.1", port=int(os.environ["FATE_XP_TEST_PORT"]), user="root", password="", autocommit=True)
        self.admin = await aiomysql.create_pool(**config)
        async with self.admin.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(f"CREATE DATABASE {self.database}")
        self.pool = await aiomysql.create_pool(**config, db=self.database)
        self.guard = GlobalXPGuard(SimpleNamespace(pool=self.pool))
        await self.sql("CREATE TABLE global_msg (user_id BIGINT UNSIGNED PRIMARY KEY, xp BIGINT NOT NULL) ENGINE=InnoDB")
        await self.sql("CREATE TABLE global_monthly (user_id BIGINT UNSIGNED, timeframe DOUBLE, xp BIGINT NOT NULL, PRIMARY KEY (user_id, timeframe)) ENGINE=InnoDB")
        await self.sql("CREATE TABLE msg (user_id BIGINT UNSIGNED PRIMARY KEY, xp BIGINT NOT NULL) ENGINE=InnoDB")
        await self.guard.ensure_schema()

    async def asyncTearDown(self):
        self.pool.close()
        await self.pool.wait_closed()
        async with self.admin.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(f"DROP DATABASE {self.database}")
        self.admin.close()
        await self.admin.wait_closed()

    async def sql(self, query, args=()):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, args)
                return await cur.fetchall()

    async def flag(self):
        for i in range(721):
            await self.guard.observe(1, i + 1, START + i * 10)

    async def test_atomic_rollback_includes_pending_and_preserves_older_and_other_xp(self):
        # Old calendar-day bucket overlaps the cutoff: preserve all of it.
        old = int((START - DAY) // DAY) * DAY
        await self.sql("INSERT INTO global_msg VALUES (1, 900), (2, 500)")
        await self.sql("INSERT INTO global_monthly VALUES (1, %s, 900), (2, %s, 500)", (old, START))
        await self.sql("INSERT INTO msg VALUES (1, 1234)")
        await self.flag()
        await self.guard.flush()
        self.assertEqual(await self.sql("SELECT * FROM global_msg ORDER BY user_id"), ((1, 900), (2, 500)))
        self.assertEqual(await self.sql("SELECT SUM(xp) FROM global_monthly WHERE user_id=1"), ((900,),))
        self.assertEqual(await self.sql("SELECT xp FROM msg WHERE user_id=1"), ((1234,),))
        self.assertEqual(await self.guard.latest_flag(1), 720)
        fresh = GlobalXPGuard(SimpleNamespace(pool=self.pool))
        self.assertFalse(await fresh.observe(1, 1000, START + 8000))
        self.assertEqual((await fresh.state(1)).blocked_until, START + 7200 + DAY)

    async def test_restart_restores_detection_and_global_cooldown(self):
        for i in range(720):
            await self.guard.observe(1, i + 1, START + i * 10)
        await self.guard.flush()
        fresh = GlobalXPGuard(SimpleNamespace(pool=self.pool))
        self.assertFalse(await fresh.observe(1, 721, START + 7191))
        self.assertFalse(await fresh.observe(1, 722, START + 7200))
        await fresh.flush()
        self.assertEqual(await fresh.latest_flag(1), 720)

    async def test_concurrent_guild_messages_award_once(self):
        results = await asyncio.gather(*(self.guard.observe(1, i, START) for i in range(1, 30)))
        self.assertEqual(sum(results), 1)
        await self.guard.flush()
        self.assertEqual(await self.sql("SELECT xp FROM global_msg WHERE user_id=1"), ((1,),))

    async def test_committed_batch_retry_never_double_awards_or_deducts(self):
        await self.flag()
        batch = self.guard.snapshot()
        await self.guard.write_batch(batch)
        # New legitimate XP after the original commit must survive its retry.
        await self.sql("UPDATE global_msg SET xp=50 WHERE user_id=1")
        await self.guard.write_batch(batch)
        self.assertEqual(await self.sql("SELECT xp FROM global_msg WHERE user_id=1"), ((50,),))
        self.assertEqual(await self.sql("SELECT COUNT(*) FROM global_xp_flags"), ((1,),))

    async def test_failure_rolls_back_every_table_then_retries_pending_batch(self):
        await self.flag()
        await self.sql("CREATE TRIGGER reject_checkpoint BEFORE INSERT ON global_xp_activity FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='injected failure'")
        with self.assertRaises(Exception):
            await self.guard.flush()
        for table in ("global_msg", "global_monthly", "global_xp_flags", "global_xp_batches", "global_xp_activity"):
            self.assertEqual(await self.sql(f"SELECT COUNT(*) FROM {table}"), ((0,),))
        self.assertIsNotNone(self.guard.batch)
        await self.sql("DROP TRIGGER reject_checkpoint")
        await self.guard.flush()
        self.assertEqual(await self.guard.latest_flag(1), 720)
        self.assertIsNone(self.guard.batch)

    async def test_partial_minute_is_preserved_and_totals_never_negative(self):
        now = START + 7201
        cutoff = now - DAY
        minute = int(cutoff // 60) * 60
        await self.sql("INSERT INTO global_msg VALUES (1, 10)")
        await self.sql("INSERT INTO global_monthly VALUES (1, %s, 5), (1, %s, 50)", (minute, minute + 60))
        self.guard.flags = [dict(user_id=1, flagged_at=now, reason="regular_cadence", blocked_until=now+DAY, cutoff=minute+60, end=int(now//60)*60)]
        await self.guard.flush()
        self.assertEqual(await self.sql("SELECT xp FROM global_msg"), ((0,),))
        self.assertEqual(await self.sql("SELECT xp FROM global_monthly"), ((5,),))
        self.assertEqual(await self.guard.latest_flag(1), 10)

    async def test_owner_release_is_persistent(self):
        await self.flag()
        await self.guard.flush()
        await self.guard.release(1)
        fresh = GlobalXPGuard(SimpleNamespace(pool=self.pool))
        self.assertTrue(await fresh.observe(1, 900, START + 8000))
        self.assertEqual((await fresh.state(1)).strikes, 0)

    async def test_lost_commit_response_retries_without_duplicating_xp(self):
        await self.guard.observe(1, 1, START)
        real_acquire = self.pool.acquire
        failed = False

        class Connection:
            def __init__(self, conn):
                self.conn = conn
            def __getattr__(self, name):
                return getattr(self.conn, name)
            async def commit(self):
                nonlocal failed
                await self.conn.commit()
                if not failed:
                    failed = True
                    raise ConnectionError("commit succeeded but response was lost")

        class Acquire:
            async def __aenter__(self):
                self.context = real_acquire()
                return Connection(await self.context.__aenter__())
            async def __aexit__(self, *args):
                return await self.context.__aexit__(*args)

        self.guard.bot = SimpleNamespace(pool=SimpleNamespace(acquire=Acquire))
        with self.assertRaises(ConnectionError):
            await self.guard.flush()
        # New activity arriving before retry must also be checkpointed once.
        await self.guard.observe(1, 2, START + 11)
        await self.guard.flush()
        self.assertEqual(await self.sql("SELECT xp FROM global_msg WHERE user_id=1"), ((2,),))
        self.assertEqual(await self.sql("SELECT SUM(xp) FROM global_monthly WHERE user_id=1"), ((2,),))
        fresh = GlobalXPGuard(SimpleNamespace(pool=self.pool))
        self.assertEqual((await fresh.state(1)).last_message, 2)

    async def test_cancellation_keeps_retryable_snapshot(self):
        await self.guard.observe(1, 1, START)
        original = self.guard.write_batch
        self.guard.write_batch = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await self.guard.flush()
        self.assertIsNotNone(self.guard.batch)
        self.guard.write_batch = original
        await self.guard.flush()
        self.assertEqual(await self.sql("SELECT xp FROM global_msg"), ((1,),))

    async def test_cleanup_preserves_unresolved_receipt(self):
        await self.guard.observe(1, 1, START)
        batch = self.guard.snapshot()
        await self.guard.write_batch(batch)
        self.guard.batch = batch
        await self.sql("UPDATE global_xp_batches SET created_at=%s", (START - 100 * DAY,))
        await self.guard.cleanup(START)
        self.assertEqual(await self.sql("SELECT COUNT(*) FROM global_xp_batches"), ((1,),))
        self.guard.batch = None
        await self.guard.cleanup(START)
        self.assertEqual(await self.sql("SELECT COUNT(*) FROM global_xp_batches"), ((0,),))

    async def test_non_transactional_legacy_tables_fail_closed(self):
        await self.sql("ALTER TABLE global_msg ENGINE=MyISAM")
        fresh = GlobalXPGuard(SimpleNamespace(pool=self.pool))
        with self.assertRaisesRegex(RuntimeError, "InnoDB"):
            await fresh.observe(1, 1, START)
        self.assertEqual(fresh.awards, {})


if __name__ == "__main__":
    unittest.main()
