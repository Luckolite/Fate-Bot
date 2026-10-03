"""History cleanup uses bounded writes and preserves recent records."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from cogs.utility.utility import Utility


class UtilityDatabaseChecks(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_uses_age_cutoffs_without_selecting_rows(self):
        cog = object.__new__(Utility)
        cog.bot = SimpleNamespace(log=SimpleNamespace(info=Mock()))
        with patch("cogs.utility.utility.time", return_value=2_000_000_000), patch("cogs.utility.utility.delete_in_batches", AsyncMock(side_effect=[2, 3, 4, 5])) as delete:
            await cog._cleanup_history()
        calls = delete.call_args_list
        self.assertEqual(len(calls), 4)
        for call in calls:
            self.assertTrue(call.args[1].startswith("delete from "))
            self.assertTrue(call.args[1].endswith("limit 5000;"))
        month = 2_000_000_000 - 30 * 86400
        self.assertEqual([call.args[2] for call in calls[:3]], [(month,)] * 3)
        year = 2_000_000_000 - 365 * 86400
        self.assertEqual(calls[3].args[2], (year, year))
        cog.bot.log.info.assert_called_once_with("Removed 5 old invites")

    async def test_activity_inserts_and_updates_use_same_epoch_timestamp(self):
        cog = object.__new__(Utility)
        cog.bot = SimpleNamespace(get_privacy=AsyncMock(return_value=True), execute=AsyncMock())
        cog.afk = {}
        message = SimpleNamespace(author=SimpleNamespace(id=1, bot=False), raw_mentions=[], content="hello")
        before = SimpleNamespace(id=1, status=discord.Status.online)
        after = SimpleNamespace(status=discord.Status.offline)
        with patch("cogs.utility.utility.time", return_value=2_000_000_000):
            await cog.on_message(message)
            await cog.on_member_update(before, after)
        self.assertEqual([call.args[1] for call in cog.bot.execute.call_args_list], [(1, 2_000_000_000, 2_000_000_000)] * 2)


if __name__ == "__main__":
    unittest.main()
