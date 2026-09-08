"""Regression checks for ghost-typing logger filters."""

import unittest
from unittest.mock import AsyncMock, Mock

from discord import Guild, Thread

from cogs.moderation.logger import Logger


class LoggerGhostTypingTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def make_logger(*, ignored_channels, privacy=True):
        logger = object.__new__(Logger)
        logger.config = {
            "123": {"ignored_channels": list(ignored_channels)}
        }
        logger.typing = {}
        logger.bot = Mock()
        logger.bot.get_privacy = AsyncMock(return_value=privacy)
        logger.bot.wait_for = AsyncMock()
        return logger

    @staticmethod
    def make_thread(*, channel_id=456, parent_id=222):
        guild = Mock(spec=Guild)
        guild.id = 123
        channel = Mock(spec=Thread)
        channel.id = channel_id
        channel.parent_id = parent_id
        channel.guild = guild
        return channel

    async def test_ghost_typing_ignores_thread_under_ignored_channel(self):
        logger = self.make_logger(ignored_channels=[222])
        channel = self.make_thread()
        user = Mock(id=789)

        await logger.on_typing(channel, user, None)

        logger.bot.wait_for.assert_not_awaited()
        self.assertEqual(logger.typing, {})

    async def test_ghost_typing_respects_disabled_mod_log_privacy(self):
        logger = self.make_logger(ignored_channels=[], privacy=False)
        channel = self.make_thread()
        user = Mock(id=789)

        await logger.on_typing(channel, user, None)

        logger.bot.wait_for.assert_not_awaited()
        self.assertEqual(logger.typing, {})


if __name__ == "__main__":
    unittest.main()
