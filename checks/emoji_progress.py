import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from botutils.tools import update_msg
from cogs.utility.emojis import Emojis


class EmojiProgressTests(unittest.IsolatedAsyncioTestCase):
    def make_message(self, kind=discord.InteractionMessage, *, ephemeral=False):
        message = Mock(spec=kind)
        message.id = 123
        message.content = "Uploading emoji(s).."
        message.flags = discord.MessageFlags(ephemeral=ephemeral)
        message.edit = AsyncMock()
        message.channel = SimpleNamespace(
            fetch_message=AsyncMock(), send=AsyncMock()
        )
        return message

    def http_error(self, code=50027, status=401):
        return discord.HTTPException(
            SimpleNamespace(status=status, reason="Failed"),
            {"code": code, "message": "Invalid Webhook Token"},
        )

    async def test_valid_interaction_appends_without_fetching(self):
        message = self.make_message()
        result = await update_msg(message, "Added smile")
        self.assertIs(result, message.edit.return_value)
        message.edit.assert_awaited_once_with(
            content="Uploading emoji(s)..\nAdded smile"
        )
        message.channel.fetch_message.assert_not_awaited()

    async def test_invalid_token_preserves_progress_and_continues_with_bot_message(self):
        for kind in (discord.InteractionMessage, discord.WebhookMessage):
            with self.subTest(kind=kind):
                message = self.make_message(kind)
                message.edit.side_effect = self.http_error()
                fetched = self.make_message(discord.Message)
                first = self.make_message(discord.Message)
                first.content = "Uploading emoji(s)..\nAdded first"
                fetched.edit.return_value = first
                message.channel.fetch_message.return_value = fetched
                cog = object.__new__(Emojis)
                cog._upload_emoji = AsyncMock(side_effect=["Added first", "Added second"])
                ctx = SimpleNamespace(msg=message)

                await cog.upload_emoji(ctx)
                self.assertIs(ctx.msg, first)
                await cog.upload_emoji(ctx)

                message.channel.fetch_message.assert_awaited_once_with(123)
                fetched.edit.assert_awaited_once_with(content=first.content)
                first.edit.assert_awaited_once_with(
                    content="Uploading emoji(s)..\nAdded first\nAdded second"
                )
                message.channel.send.assert_not_awaited()

    async def test_other_http_errors_are_not_retried(self):
        message = self.make_message()
        error = self.http_error(50013, 403)
        message.edit.side_effect = error
        with self.assertRaises(discord.HTTPException) as caught:
            await update_msg(message, "Added smile")
        self.assertIs(caught.exception, error)
        message.channel.fetch_message.assert_not_awaited()

    async def test_ephemeral_response_does_not_fall_back_to_public_channel(self):
        message = self.make_message(ephemeral=True)
        message.edit.side_effect = self.http_error()
        with self.assertRaises(discord.HTTPException):
            await update_msg(message, "Added smile")
        message.channel.fetch_message.assert_not_awaited()
        message.channel.send.assert_not_awaited()

    async def test_regular_message_appends_normally(self):
        message = self.make_message(discord.Message)
        await update_msg(message, "Added smile")
        message.edit.assert_awaited_once_with(
            content="Uploading emoji(s)..\nAdded smile"
        )

    async def test_overflow_continues_in_new_message(self):
        message = self.make_message()
        message.content = "x" * 1990
        continuation = self.make_message(discord.Message)
        continuation.content = "Uploading emoji(s)"
        message.channel.send.return_value = continuation
        result = await update_msg(message, "Added smile")
        self.assertIs(result, continuation.edit.return_value)
        message.channel.send.assert_awaited_once_with("Uploading emoji(s)")
        continuation.edit.assert_awaited_once_with(
            content="Uploading emoji(s)\nAdded smile"
        )
        message.edit.assert_not_awaited()

    async def test_failed_bot_fetch_is_not_hidden(self):
        message = self.make_message()
        message.edit.side_effect = self.http_error()
        error = self.http_error(10008, 404)
        message.channel.fetch_message.side_effect = error
        with self.assertRaises(discord.HTTPException) as caught:
            await update_msg(message, "Added smile")
        self.assertIs(caught.exception, error)
        message.channel.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
