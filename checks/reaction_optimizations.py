"""Reaction webhook requests reuse Discord's existing transport."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from cogs.fun.reactions import Reactions


class ReactionChecks(unittest.IsolatedAsyncioTestCase):
    async def test_cached_and_fetched_webhooks_send_directly(self):
        for cached in (True, False):
            webhook = SimpleNamespace(name="Reaction", send=AsyncMock(), delete=AsyncMock())
            ctx = SimpleNamespace(
                guild=SimpleNamespace(id=1),
                channel=SimpleNamespace(id=2, webhooks=AsyncMock(return_value=[webhook])),
                author=SimpleNamespace(name="Clyde", display_avatar=SimpleNamespace(url="avatar")),
                message=SimpleNamespace(mentions=[], delete=AsyncMock()),
            )

            cog = Reactions(SimpleNamespace(loop=SimpleNamespace(call_later=Mock())))
            if cached:
                cog.webhook[2] = webhook
            with patch("cogs.fun.reactions.os.listdir", return_value=["a.gif"]), patch("cogs.fun.reactions.discord.File", return_value="file"), patch("cogs.fun.reactions.asyncio.sleep", AsyncMock()):
                await cog.send_webhook(ctx, "hug", "hello")
            webhook.send.assert_awaited_once_with(content="hello", username="🚫", avatar_url="avatar", file="file")
            cog.bot.loop.call_later.assert_called_once_with(
                300, cog.expire_selection, 1, "hug", "a.gif"
            )
            ctx.message.delete.assert_awaited_once()
            if cached:
                ctx.channel.webhooks.assert_not_awaited()
                webhook.delete.assert_not_awaited()
            else:
                ctx.channel.webhooks.assert_awaited_once()
                webhook.delete.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
