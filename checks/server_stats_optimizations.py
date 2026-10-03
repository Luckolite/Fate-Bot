"""Server-stat counts and boost-only updates without walking the member cache."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs.utility.server_stats import ServerStatistics


class ServerStatChecks(unittest.IsolatedAsyncioTestCase):
    async def test_counts_preserve_humans_bots_and_boosts(self):
        channels = {key: SimpleNamespace(name="old", edit=AsyncMock()) for key in (10, 20, 30)}
        cog = object.__new__(ServerStatistics)
        cog.config = {1: {
            stat: {"channel_id": channel_id, "format": stat + " {count}"}
            for stat, channel_id in (("members", 10), ("bots", 20), ("boosts", 30))
        }}
        cog.bot = SimpleNamespace(get_channel=channels.get)
        guild = SimpleNamespace(id=1, premium_subscription_count=4, members=[
            SimpleNamespace(bot=False), SimpleNamespace(bot=True), SimpleNamespace(bot=False),
        ])
        await cog.update_channels(guild)
        channels[10].edit.assert_awaited_once_with(name="members 2")
        channels[20].edit.assert_awaited_once_with(name="bots 1")
        channels[30].edit.assert_awaited_once_with(name="boosts 4")

    async def test_boost_only_config_never_walks_members_and_unchanged_name_skips_edit(self):
        guild = SimpleNamespace(id=1, premium_subscription_count=4)
        channel = SimpleNamespace(name="boosts 4", edit=AsyncMock())
        cog = object.__new__(ServerStatistics)
        cog.config = {1: {"boosts": {"channel_id": 30, "format": "boosts {count}"}}}
        cog.bot = SimpleNamespace(get_channel=lambda channel_id: channel)
        await cog.update_channels(guild)
        channel.edit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
