"""Legacy command handlers preserve normal failures without swallowing shutdown."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.misc.global_chat import GlobalChat
from cogs.misc.nsfw import NSFW
from cogs.utility.embeds import EmbedCreatorView


class LegacyExceptionChecks(unittest.IsolatedAsyncioTestCase):
    async def test_global_queue_cancellation_reaches_task_owner(self):
        cog = GlobalChat.__new__(GlobalChat)
        message = SimpleNamespace(
            guild=SimpleNamespace(id=2), author=SimpleNamespace(id=2),
            channel=SimpleNamespace(send=AsyncMock(side_effect=asyncio.CancelledError())),
        )
        cog._queue = [[("message", []), False, message] for _ in range(4)]
        cog.config = {"blocked": []}

        with self.assertRaises(asyncio.CancelledError):
            await GlobalChat.handle_queue.coro(cog)

        message.channel.send.assert_awaited_once()

    async def test_image_query_cancellation_does_not_send_failure_reply(self):
        cog = NSFW(SimpleNamespace())
        cog.query = AsyncMock(side_effect=asyncio.CancelledError())
        ctx = SimpleNamespace(send=AsyncMock())

        with self.assertRaises(asyncio.CancelledError):
            await NSFW._gel.callback(cog, ctx, tag="test")

        ctx.send.assert_not_awaited()

    async def test_global_message_cancellation_reaches_listener_owner(self):
        cog = GlobalChat.__new__(GlobalChat)
        cog.active_channel_ids = {4}
        message = SimpleNamespace(content="Hello", author=SimpleNamespace(bot=False), channel=SimpleNamespace(id=4))

        with patch("cogs.misc.global_chat.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError())):
            with self.assertRaises(asyncio.CancelledError):
                await GlobalChat.on_message(cog, message)

    async def test_image_query_regular_error_still_sends_failure_reply(self):
        cog = NSFW(SimpleNamespace())
        cog.query = AsyncMock(side_effect=RuntimeError("Unavailable"))
        ctx = SimpleNamespace(send=AsyncMock())

        await NSFW._gel.callback(cog, ctx, tag="test")

        ctx.send.assert_awaited_once_with("error")

    async def edit_color(self, value, *, color_error=None):
        response = SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock())
        modal = SimpleNamespace(answer=SimpleNamespace(value=value), interaction=SimpleNamespace(response=response), wait=AsyncMock())
        view = EmbedCreatorView(SimpleNamespace(bot=SimpleNamespace(config={"theme_color": 0})))
        interaction = SimpleNamespace(data={"values": ["Set Color"]}, response=SimpleNamespace(send_modal=AsyncMock()))
        with patch("cogs.utility.embeds.Ask", return_value=modal):
            if color_error:
                with patch("cogs.utility.embeds.Color", side_effect=color_error):
                    await view.callback(interaction)
            else:
                await view.callback(interaction)
        return response, view

    async def test_invalid_hex_keeps_validation_feedback(self):
        response, view = await self.edit_color("#invalid")

        response.send_message.assert_awaited_once_with("That's not a valid HEX color", ephemeral=True)
        response.edit_message.assert_not_awaited()
        self.assertEqual(view.color, 0)

    async def test_valid_hex_updates_the_embed(self):
        response, view = await self.edit_color("#ff0000")

        response.send_message.assert_not_awaited()
        self.assertEqual(view.color.value, 0xFF0000)
        self.assertEqual(response.edit_message.call_args.kwargs["embed"].color.value, 0xFF0000)

    async def test_unexpected_color_failure_propagates(self):
        with self.assertRaisesRegex(RuntimeError, "Color construction failed"):
            await self.edit_color("#ff0000", color_error=RuntimeError("Color construction failed"))


if __name__ == "__main__":
    unittest.main()
