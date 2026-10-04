"""Commands distinguish input errors, delivery failures, and cancellation."""

import asyncio
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from cogs.core.user import User
from cogs.utility.emojis import Emojis
from cogs.utility.utility import Utility


class CommandExceptionChecks(unittest.IsolatedAsyncioTestCase):
    def color_context(self):
        role = SimpleNamespace(
            name="Member", position=2, color=discord.Color.blue(), edit=AsyncMock(),
        )
        context = SimpleNamespace(
            author=SimpleNamespace(
                guild_permissions=SimpleNamespace(manage_roles=True),
                top_role=SimpleNamespace(position=10),
            ),
            guild=SimpleNamespace(get_role=Mock(return_value=role)),
            send=AsyncMock(),
        )
        return context, role

    def webhook_context(self):
        return SimpleNamespace(
            message=SimpleNamespace(attachments=[]),
            guild=SimpleNamespace(icon=None),
            author=SimpleNamespace(send=AsyncMock()),
            channel=SimpleNamespace(create_webhook=AsyncMock(return_value=SimpleNamespace(
                name="Relay", url="https://example.test/webhooks/1/private-token",
            ))),
            send=AsyncMock(),
        )

    async def test_invalid_role_color_reports_input_error_without_editing(self):
        context, role = self.color_context()

        await Utility.color.callback(None, context, "<@&2>", "invalid")

        context.send.assert_awaited_once_with("Invalid Hex")
        role.edit.assert_not_awaited()

    async def test_valid_role_color_keeps_edit_and_confirmation(self):
        context, role = self.color_context()

        await Utility.color.callback(None, context, "<@&2>", "#aabbcc")

        role.edit.assert_awaited_once_with(color=discord.Color(0xAABBCC))
        context.send.assert_awaited_once_with(
            "Changed Member's color from #3498db to #aabbcc",
        )

    async def test_color_delivery_error_is_not_relabelled_as_invalid_input(self):
        context, role = self.color_context()
        context.send.side_effect = [OSError("Connection lost"), None]

        with self.assertRaisesRegex(OSError, "Connection lost"):
            await Utility.color.callback(None, context, "<@&2>", "1ffffff")

        context.send.assert_awaited_once_with("That hex value is too large")
        role.edit.assert_not_awaited()

    async def test_color_cancellation_is_not_relabelled_as_invalid_input(self):
        context, _ = self.color_context()
        context.send.side_effect = [asyncio.CancelledError, None]

        with self.assertRaises(asyncio.CancelledError):
            await Utility.color.callback(None, context, "<@&2>", "1ffffff")

        self.assertEqual(context.send.await_count, 1)

    async def test_role_color_keeps_zero_digits_after_optional_prefix(self):
        for literal, expected in (
            ("#ff0000", 0xFF0000), ("#00ff00", 0x00FF00),
            ("0x000000", 0), ("0x0f0f00", 0x0F0F00),
        ):
            with self.subTest(literal=literal):
                context, role = self.color_context()
                await Utility.color.callback(None, context, "<@&2>", literal)
                role.edit.assert_awaited_once_with(color=discord.Color(expected))

    async def test_webhook_dm_success_keeps_confirmation(self):
        context = self.webhook_context()

        await Utility.create_webhook.callback(None, context, name="Relay")

        context.author.send.assert_awaited_once()
        context.send.assert_awaited_once_with("Sent the webhook url to dm 👍")

    async def test_webhook_dm_failure_keeps_existing_fallback(self):
        context = self.webhook_context()
        context.author.send.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "DM disabled",
        )

        await Utility.create_webhook.callback(None, context, name="Relay")

        call = context.send.await_args
        self.assertEqual(call.args, ("Failed to dm you the webhook url",))
        self.assertEqual(call.kwargs["embed"].description,
                         "https://example.test/webhooks/1/private-token")

    async def test_webhook_confirmation_failure_does_not_post_url_fallback(self):
        context = self.webhook_context()
        context.send.side_effect = [OSError("Connection lost"), None]

        with self.assertRaises(OSError):
            await Utility.create_webhook.callback(None, context, name="Relay")

        context.author.send.assert_awaited_once()
        context.send.assert_awaited_once_with("Sent the webhook url to dm 👍")

    async def test_webhook_dm_cancellation_does_not_post_url_fallback(self):
        context = self.webhook_context()
        context.author.send.side_effect = asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await Utility.create_webhook.callback(None, context, name="Relay")

        context.send.assert_not_awaited()

    def blocked_cog(self, fetch_error):
        cursor = SimpleNamespace(
            execute=AsyncMock(), fetchall=AsyncMock(return_value=[(1, "Saved name", "Reason")]),
        )

        @asynccontextmanager
        async def open_cursor():
            yield cursor

        cog = User.__new__(User)
        cog.bot = SimpleNamespace(
            user=SimpleNamespace(display_avatar=SimpleNamespace(url="https://example.test/bot.png")),
            fetch_user=AsyncMock(side_effect=fetch_error),
            utils=SimpleNamespace(cursor=open_cursor),
        )
        return cog

    async def test_blocked_list_fetch_error_keeps_saved_name(self):
        context = SimpleNamespace(send=AsyncMock())

        await User.blocked.callback(self.blocked_cog(OSError("Offline")), context)

        field = context.send.await_args.kwargs["embed"].fields[0]
        self.assertEqual((field.name, field.value), ("Saved name", "Reason"))

    async def test_blocked_list_cancellation_stops_before_sending(self):
        context = SimpleNamespace(send=AsyncMock())

        with self.assertRaises(asyncio.CancelledError):
            await User.blocked.callback(self.blocked_cog(asyncio.CancelledError), context)

        context.send.assert_not_awaited()

    def sticker_context(self):
        return SimpleNamespace(
            author="Uploader",
            message=SimpleNamespace(reference=None, stickers=[], attachments=[]),
            guild=SimpleNamespace(create_sticker=AsyncMock()),
            send=AsyncMock(),
        )

    async def test_sticker_download_error_keeps_processing_remaining_links(self):
        context = self.sticker_context()
        with patch("cogs.utility.emojis.download", AsyncMock(side_effect=[OSError("Offline"), b"image"])):
            await Emojis.add_sticker.callback(
                None, context, "https://example.test/a.png", "https://example.test/b.png",
            )

        self.assertEqual(context.send.await_args_list[0].args, ("Failed to fetch https://example.test/a.png",))
        self.assertEqual(context.guild.create_sticker.await_args.kwargs["name"], "b")
        self.assertEqual(context.guild.create_sticker.await_count, 1)

    async def test_sticker_download_cancellation_stops_upload(self):
        context = self.sticker_context()
        with patch("cogs.utility.emojis.download", AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await Emojis.add_sticker.callback(None, context, "https://example.test/a.png")

        context.send.assert_not_awaited()
        context.guild.create_sticker.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
