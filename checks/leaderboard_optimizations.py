"""Global rankings are shared without extending their freshness window."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from weakref import WeakValueDictionary

from cogs.core.ranking import Ranking
from checks.custom_command_optimizations import Cursor


class LeaderboardChecks(unittest.IsolatedAsyncioTestCase):
    def make_cog(self):
        cog = object.__new__(Ranking)
        cog.leaderboard_cache = {}
        cog.leaderboard_locks = WeakValueDictionary()

        async def fetch(guild_id, *, include_global):
            await asyncio.sleep(0)
            data = {"Msg Leaderboard": [(guild_id, 5)], "Monthly Msg Leaderboard": [(guild_id, 3)]}
            if include_global:
                data.update({"Global Msg Leaderboard": [(100, 10)], "Global Monthly Leaderboard": [(101, 6)]})
            return data

        cog._fetch_leaderboards = AsyncMock(side_effect=fetch)
        return cog

    async def test_concurrent_guilds_fetch_global_boards_once(self):
        cog = self.make_cog()
        results = await asyncio.gather(*(cog.get_leaderboard_data(guild_id) for guild_id in range(1, 21)))
        self.assertEqual(sum(call.kwargs["include_global"] for call in cog._fetch_leaderboards.call_args_list), 1)
        self.assertEqual(cog._fetch_leaderboards.await_count, 20)
        for guild_id, result in enumerate(results, 1):
            self.assertEqual(result["Msg Leaderboard"], [(guild_id, 5)])
            self.assertEqual(result["Global Msg Leaderboard"], [(100, 10)])
        await cog.get_leaderboard_data(1)
        self.assertEqual(cog._fetch_leaderboards.await_count, 20)

    async def test_reuse_does_not_extend_global_ttl_and_clear_forces_refresh(self):
        cog = self.make_cog()
        with patch("cogs.core.ranking.monotonic", return_value=100):
            await cog.get_leaderboard_data(1)
        with patch("cogs.core.ranking.monotonic", return_value=129):
            await cog.get_leaderboard_data(2)
        self.assertEqual(cog.leaderboard_cache[2][0], 130)
        with patch("cogs.core.ranking.monotonic", return_value=131):
            await cog.get_leaderboard_data(2)
        self.assertTrue(cog._fetch_leaderboards.call_args.kwargs["include_global"])
        cog.leaderboard_cache.clear()
        await cog.get_leaderboard_data(1)
        self.assertTrue(cog._fetch_leaderboards.call_args.kwargs["include_global"])

    async def test_guild_only_query_omits_global_tables_and_decodes_rows(self):
        cursor = Cursor(rows=[(b"guild", 1, 10), ("monthly", 2, 5)])
        cog = object.__new__(Ranking)
        cog.bot = SimpleNamespace(utils=SimpleNamespace(cursor=lambda: cursor), log=SimpleNamespace(debug=Mock()))
        result = await cog._fetch_leaderboards(7, include_global=False)
        query, args = cursor.execute.call_args.args
        self.assertNotIn("global_msg", query)
        self.assertNotIn("global_monthly", query)
        self.assertEqual(args[:2], (7, 7))
        self.assertEqual(len(args), 3)
        self.assertEqual(result["Msg Leaderboard"], [(1, 10)])
        self.assertEqual(result["Monthly Msg Leaderboard"], [(2, 5)])
        await cog._fetch_leaderboards(7, include_global=True)
        query, args = cursor.execute.call_args.args
        self.assertIn("global_msg", query)
        self.assertIn("global_monthly", query)
        self.assertEqual(len(args), 4)


if __name__ == "__main__":
    unittest.main()
