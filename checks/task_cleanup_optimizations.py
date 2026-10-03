"""Checks for batched cooperative cleanup and live cooldown instances."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from discord.ext import commands

from botutils import Cooldown
from cogs.core.tasks import Tasks


class CleanupChecks(unittest.IsolatedAsyncioTestCase):
    async def test_cog_runtime_cleanup_uses_same_cutoff(self):
        cog = SimpleNamespace(cleanup_runtime_state=AsyncMock())
        bot = SimpleNamespace(filtered_messages={}, cogs={"Logger": cog})
        with patch("cogs.core.tasks.time.time", return_value=1000):
            await Tasks.cog_cleanup.coro(SimpleNamespace(bot=bot))
        cog.cleanup_runtime_state.assert_awaited_once_with(1000)

    async def test_builtin_command_buckets_expire_without_resetting_active_limits(self):
        mapping = commands.CooldownMapping.from_cooldown(1, 60, commands.BucketType.user)
        expired = commands.Cooldown(1, 60)
        expired.update_rate_limit(100)
        active = commands.Cooldown(1, 60)
        active.update_rate_limit(990)
        mapping._cache = {1: expired, 2: active}
        command = SimpleNamespace(_buckets=mapping)
        bot = SimpleNamespace(filtered_messages={}, cogs={},
                              walk_commands=lambda: iter((command, command)))
        with patch("cogs.core.tasks.time.time", return_value=1000), patch.object(
            mapping, "_verify_cache_integrity", wraps=mapping._verify_cache_integrity
        ) as cleanup:
            await Tasks.cog_cleanup.coro(SimpleNamespace(bot=bot))
        cleanup.assert_called_once_with(1000)
        self.assertEqual(mapping._cache, {2: active})
        self.assertEqual(active.get_retry_after(1000), 50)

    async def test_cog_cooldowns_are_cleaned_and_active_windows_survive(self):
        cooldown = Cooldown(2, 10)
        cooldown.index = {1: [1, 2], 2: [1000, 1]}
        cooldown._last_cleanup = 0
        cog = SimpleNamespace(cooldown=cooldown)
        bot = SimpleNamespace(filtered_messages={1: {1: 5000, 2: 10000}}, cogs={"Name": cog})
        with patch("cogs.core.tasks.time.time", return_value=10000):
            await Tasks.cog_cleanup.coro(SimpleNamespace(bot=bot))
        self.assertEqual(cooldown.index, {2: [1000, 1]})
        self.assertEqual(bot.filtered_messages, {1: {2: 10000}})

    async def test_large_cleanup_yields_in_batches_and_preserves_refreshed_entry(self):
        bot = SimpleNamespace(filtered_messages={1: {key: 0 for key in range(300)}}, cogs={})
        calls = 0

        async def yield_loop(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                bot.filtered_messages[1][129] = 10000
        with patch("cogs.core.tasks.time.time", return_value=10000), patch("cogs.core.tasks.asyncio.sleep", AsyncMock(side_effect=yield_loop)) as sleep:
            await Tasks.cog_cleanup.coro(SimpleNamespace(bot=bot))
        self.assertEqual(sleep.await_count, 3)
        self.assertEqual(bot.filtered_messages, {1: {129: 10000}})


if __name__ == "__main__":
    unittest.main()
