"""Dashboard pool creation and profile reads use bounded SQL work."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from apps.Dashboard.dashboard.ranking_store import MySQLRankingStore
from apps.Dashboard.dashboard.settings_store import BotSettingsStore
from botutils.cache_rewrite import Cache
from checks.mongo_cache_optimizations import Collection


class DashboardDatabaseChecks(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_first_reads_create_one_pool(self):
        store = MySQLRankingStore(host="127.0.0.1", pool_recycle=60, maxsize=2)
        pool = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())

        async def create(**kwargs):
            await asyncio.sleep(0)
            return pool
        with patch("apps.Dashboard.dashboard.ranking_store.aiomysql.create_pool", AsyncMock(side_effect=create)) as factory:
            pools = await asyncio.gather(*(store._pool() for _ in range(20)))
            self.assertTrue(all(value is pool for value in pools))
            factory.assert_awaited_once()
            self.assertEqual(factory.call_args.kwargs["pool_recycle"], 60)
            self.assertEqual(factory.call_args.kwargs["maxsize"], 2)
        await store.close()
        await store.close()
        pool.close.assert_called_once()
        pool.wait_closed.assert_awaited_once()

    async def test_close_waits_for_initializing_pool(self):
        store = MySQLRankingStore()
        pool = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
        started, proceed = asyncio.Event(), asyncio.Event()

        async def create(**kwargs):
            started.set()
            await proceed.wait()
            return pool
        with patch("apps.Dashboard.dashboard.ranking_store.aiomysql.create_pool", AsyncMock(side_effect=create)):
            load = asyncio.create_task(store._pool())
            await started.wait()
            close = asyncio.create_task(store.close())
            await asyncio.sleep(0)
            pool.close.assert_not_called()
            proceed.set()
            await asyncio.gather(load, close)
        self.assertIsNone(store.pool)
        pool.close.assert_called_once()

    async def test_profile_uses_one_query_and_preserves_absent_board_defaults(self):
        for rows in ([], [("server", 250, 2)], [("global", 500, 3)], [("server", 250, 2), ("global", 500, 3)]):
            store = MySQLRankingStore()
            store._fetchall = AsyncMock(return_value=rows)
            profile = await store.profile(42, 7, {"first_lvl_xp_req": 250})
            store._fetchall.assert_awaited_once()
            self.assertEqual(store._fetchall.call_args.args[1], (7, 42, 42))
            expected = {board: (xp, rank) for board, xp, rank in rows}
            for board in ("server", "global"):
                self.assertEqual((profile[board]["xp"], profile[board]["rank"]), expected.get(board, (0, None)))

    async def test_unchanged_bot_dashboard_save_skips_prefix_and_ranking_writes(self):
        collection = Collection()
        collection.find_one = AsyncMock(return_value={"_id": 1, "first_lvl_xp_req": 250})
        prefix_collection = SimpleNamespace(update_one=AsyncMock())
        bot = SimpleNamespace(
            loop=asyncio.get_running_loop(), aio_mongo={"ranking": collection, "GuildPrefixes": prefix_collection},
            guild_prefixes={1: {"prefix": "!", "override": False}},
            get_guild=lambda guild_id: SimpleNamespace(id=guild_id),
        )

        class Mapping(dict):
            async def flush(self):
                pass

        ranking = Cache(bot, "ranking")
        cogs = {
            "Settings": SimpleNamespace(config=Mapping()),
            "Ranking": SimpleNamespace(config=ranking),
            "Messages": SimpleNamespace(config=Mapping()),
            "Verification": object(),
        }
        bot.get_cog = cogs.get
        store = BotSettingsStore(bot)
        store._actor = AsyncMock()
        store._validate_channels = Mock()
        store._preflight_verification = AsyncMock()
        store._save_verification = AsyncMock()
        store.get = AsyncMock(return_value={})
        settings = dict(general={}, prefix={"value": "!", "allow_personal": True},
                        messages={}, ranking={"first_lvl_xp_req": 250}, verification={})
        await store.save(1, settings)
        prefix_collection.update_one.assert_not_awaited()
        self.assertEqual(collection.batches, [])
        settings["ranking"]["first_lvl_xp_req"] = 500
        await store.save(1, settings)
        self.assertEqual(collection.documents[1]["first_lvl_xp_req"], 500)


if __name__ == "__main__":
    unittest.main()
