"""Global chat cooldowns release their member state on expiry, failure, or cancellation."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from cogs.misc.global_chat import GlobalChat


class GlobalChatIgnoreChecks(unittest.IsolatedAsyncioTestCase):
    def make_cooldown(self):
        cog = GlobalChat.__new__(GlobalChat)
        cog.active_channel_ids = {4}
        cog.ignore = []
        cog.cd = SimpleNamespace(check=Mock(return_value=True))
        message = SimpleNamespace(
            content="Hello", author=SimpleNamespace(id=2, bot=False),
            guild=SimpleNamespace(me=object()),
            channel=SimpleNamespace(id=4, permissions_for=lambda member: SimpleNamespace(add_reactions=True)),
            add_reaction=AsyncMock(),
        )
        return cog, message

    async def test_expired_cooldown_releases_member_and_keeps_wait_order(self):
        cog, message = self.make_cooldown()
        waits = []

        async def sleep(seconds):
            waits.append(seconds)
            if seconds == 25:
                self.assertEqual(cog.ignore, [2])

        with patch("cogs.misc.global_chat.asyncio.sleep", AsyncMock(side_effect=sleep)):
            await GlobalChat.on_message(cog, message)

        self.assertEqual(waits, [0.21, 25])
        self.assertEqual(cog.ignore, [])
        message.add_reaction.assert_awaited_once_with("⏳")

    async def test_cancelled_cooldown_releases_member(self):
        cog, message = self.make_cooldown()

        async def sleep(seconds):
            if seconds == 25:
                self.assertEqual(cog.ignore, [2])
                raise asyncio.CancelledError()

        with patch("cogs.misc.global_chat.asyncio.sleep", AsyncMock(side_effect=sleep)):
            with self.assertRaises(asyncio.CancelledError):
                await GlobalChat.on_message(cog, message)

        self.assertEqual(cog.ignore, [])

    async def test_failed_reaction_releases_member_and_keeps_existing_diagnostic(self):
        cog, message = self.make_cooldown()
        message.add_reaction.side_effect = RuntimeError("Reaction failed")

        with patch("cogs.misc.global_chat.asyncio.sleep", AsyncMock()), patch("builtins.print") as console:
            await GlobalChat.on_message(cog, message)

        self.assertEqual(cog.ignore, [])
        self.assertIn("Reaction failed", console.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
