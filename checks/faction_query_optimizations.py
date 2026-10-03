"""Claim miss-cache bounds, expiry, and ownership invalidation checks."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from weakref import WeakValueDictionary

from cogs.fun.factions_rewrite import FactionsRewrite
from checks.custom_command_optimizations import Cursor


class ClaimChecks(unittest.IsolatedAsyncioTestCase):
    def make_cog(self, row=None):
        cog = object.__new__(FactionsRewrite)
        cog.repo = SimpleNamespace(one=AsyncMock(return_value=row))
        cog.unclaimed_channels = {}
        cog.claim_lookup_locks = WeakValueDictionary()
        cog.claim_revision = 0
        return cog

    async def test_repeated_unclaimed_channel_is_queried_once_then_expires(self):
        cog = self.make_cog()
        with patch("cogs.fun.factions_rewrite.monotonic", return_value=100):
            for _ in range(20):
                self.assertIsNone(await cog.find_channel_claim((1, 2)))
        cog.repo.one.assert_awaited_once()
        with patch("cogs.fun.factions_rewrite.monotonic", return_value=161):
            await cog.find_channel_claim((1, 2))
        self.assertEqual(cog.repo.one.await_count, 2)

    async def test_positive_claims_always_query_current_owner(self):
        cog = self.make_cog((5,))
        self.assertEqual(await cog.find_channel_claim((1, 2)), (5,))
        cog.repo.one.return_value = (6,)
        self.assertEqual(await cog.find_channel_claim((1, 2)), (6,))
        self.assertEqual(cog.unclaimed_channels, {})

    async def test_successful_claim_invalidates_miss_immediately(self):
        cog = self.make_cog()
        await cog.find_channel_claim((1, 2))
        cog.invalidate_claim((1, 2))
        cog.repo.one.return_value = (5,)
        self.assertEqual(await cog.find_channel_claim((1, 2)), (5,))
        cog.unclaimed_channels[(1, 3)] = 100
        cog.invalidate_claim()
        self.assertEqual(cog.unclaimed_channels, {})

    async def test_claim_command_invalidates_only_after_successful_write(self):
        for rowcount in (0, 1):
            cog = self.make_cog()
            cursor = Cursor()
            cursor.rowcount = rowcount
            cog.repo.transaction = lambda: cursor
            cog.require_manager = AsyncMock(return_value=[SimpleNamespace(id=5, name="Team")])
            cog.unclaimed_channels[(1, 2)] = 9999999999
            ctx = SimpleNamespace(
                guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=2, mention="#channel"),
                send=AsyncMock(),
            )
            await FactionsRewrite.claim.callback(cog, ctx)
            self.assertEqual((1, 2) in cog.unclaimed_channels, rowcount == 0)

    async def test_inflight_miss_does_not_undo_claim_invalidation(self):
        cog = self.make_cog()

        async def query(*args):
            cog.invalidate_claim((1, 2))
            return None
        cog.repo.one.side_effect = query
        await cog.find_channel_claim((1, 2))
        self.assertEqual(cog.unclaimed_channels, {})

    async def test_simultaneous_misses_share_database_read(self):
        cog = self.make_cog()

        async def query(*args):
            await asyncio.sleep(0)
            return None
        cog.repo.one.side_effect = query
        await asyncio.gather(*(cog.find_channel_claim((1, 2)) for _ in range(20)))
        cog.repo.one.assert_awaited_once()

    async def test_cache_is_bounded_and_failed_queries_are_not_cached(self):
        cog = self.make_cog()
        with patch("cogs.fun.factions_rewrite.MAX_PENDING_CLAIM_CHANNELS", 2):
            for key in ((1, 2), (1, 3), (1, 4)):
                await cog.find_channel_claim(key)
        self.assertEqual(set(cog.unclaimed_channels), {(1, 3), (1, 4)})
        cog.repo.one.side_effect = RuntimeError("database unavailable")
        with self.assertRaises(RuntimeError):
            await cog.find_channel_claim((1, 5))
        self.assertNotIn((1, 5), cog.unclaimed_channels)


if __name__ == "__main__":
    unittest.main()
